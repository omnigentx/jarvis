# Workstream 03 — Indexing Pipeline (Outbox → Qdrant)

Goal: durable, idempotent, restart-safe pipeline that projects SQLite truth
into the Qdrant search index, fully off the chat critical path. Spec
sections: 5 (write path), 8 (hybrid index), 13 (chunking), 19 (background
indexing), 20 (degraded modes), 12.8 (retention).

## Flow

```text
SQLite commit (MemoryService / episodic projector)
  -> memory_index_outbox row (same transaction)
  -> memory_index_worker picks up pending rows
  -> projector/chunker -> embedding batch -> Qdrant upsert/delete
  -> mark aggregate indexed_revision; outbox row done
  -> SSE memory_indexed / memory_index_failed
```

## 1. `outbox_service.py`

- `enqueue(event_type, aggregate_id, aggregate_revision, payload)` — called
  INSIDE the same SQLite transaction as the domain write (this is the whole
  point of the outbox: commit atomicity between truth and index intent).
- Idempotency via `UNIQUE(event_type, aggregate_id, aggregate_revision)`;
  re-enqueue of the same revision is a silent no-op.
- Event types: `memory_upsert`, `memory_delete`, `episodic_upsert`,
  `episodic_prune`, `rebuild_all`.

## 2. `memory_index_worker.py`

DEVIATION (verified): the existing `BackgroundJobScheduler`
(`backend/services/background_jobs.py`) is HARD idle-gated — its `start()`
loop calls `_is_idle()` (no on-demand TTS + 30s cooldown) before running any
job. Registering the index worker there would make indexing idle-only, which
contradicts "continuous low priority". So the worker runs its OWN lightweight
asyncio loop started in the FastAPI lifespan, independent of the TTS
scheduler. It still exposes a clean, directly-callable `process_pending()` for
tests (no loop needed). The durable queue is `memory_index_outbox` itself, so
there is no in-memory state to restore — restart recovery = reclaim expired
leases on the outbox.

- Poll cadence: claim batches of pending rows ordered by `next_attempt_at`
  (the scheduler loop owns sleeping; keep claims short).
- Retry: exponential backoff via `next_attempt_at`, bounded attempts
  (e.g. 8), then status `dead` + `memory_index_failed` SSE + visible in
  index-status route. Dead letters are re-queueable from the UI/route.
- Idempotent execution: Qdrant point IDs are deterministic
  (`chunk_id` UUID5 of record_id + chunk ordinal + index_revision), upserts
  are safe to repeat. Per-record `indexed_revision` check skips stale work.
- Crash mid-batch: rows stay `in_progress` with a lease timestamp; on
  startup, rows with expired leases revert to `pending` (restart recovery).
- Retention pruning runs here too (one task type): delete `retrieval_runs`
  older than the window; archive/prune `episodic_documents` older than the
  window UNLESS referenced by an active `memory_sources` row; emit audit
  events. Windows from settings (doc 02).

## 3. `projector.py` + `chunker.py`

Projection sources → `episodic_documents` (immutable copies):

- Completed logical turns from `agent_message_stream` (canonical stream,
  normalized agent names, per-agent cursor — extend its cursor pattern with
  a memory-projection cursor so content is projected exactly once).
- Meeting transcripts (`meeting_transcripts` table) for participants listed
  in the meeting `config_json`.
- Prompt injections received by an agent.
- `communication_records` (emails — written by doc 05's hook).

Chunk policies (spec §13 table): one message/logical turn per chunk; tool
trace = goal + tool + key args + outcome; meeting = topic/speaker block;
email = subject + normalized body; decision/fact = one record; skill/runbook
= heading-aware; prose 400-800 tokens with 50-100 overlap; oversized tool
output = short projection + artifact reference.

Never embed: secrets (run sensitivity scan from doc 05's `sensitivity.py`
BEFORE persistence and embedding), raw binary, duplicate snapshots
(content_hash dedupe), unbounded tool output, transient progress events,
content the agent is not authorized to read.

## 4. `embedding_provider.py` + `bge_embedding_provider.py`

- Interface: `embed_documents(texts) -> vectors`, `embed_query(text) ->
  vector`, `revision() -> str`.
- BGE-M3 with PINNED revision (settings). Lazy singleton loaded in the MAIN
  backend process only — agent subprocesses always go through the backend
  API/tools and never load a model (spec §8.3). Guard with an env check so
  accidental import in a spawned subprocess raises loudly.
- Batch document embedding (worker path); on-demand query embedding
  (retrieval path). Document footprint: BGE-M3 ~2.3 GB disk; verify RAM
  headroom before enabling dense retrieval on the deploy server.
- Never silently substitute a different model into the same collection
  (spec §20): collection name carries the schema version
  (`jarvis_memory_bge_m3_v1`); a model/revision change = new collection +
  rebuild.

## 5. `qdrant_indexer.py`

- Creates collection + payload indexes on first use (idempotent):
  named vectors `dense` + `bm25` (Qdrant sparse/BM25); payload fields and
  indexes exactly per spec §8.1 (`owner_agent_name`, `memory_type`,
  `subject_scope`, `source_type`, `status`, `authority`, `created_at`,
  `embedding_revision`).
- Upsert points with full payload; delete by `record_id` filter on memory
  deletion/archival (spec §21: deletion removes index projections through
  the outbox).
- Partial-index state: if the embedder is down, BM25-only upsert proceeds
  and the point/record is marked dense-pending for later completion (spec
  §20 — no all-or-nothing failure).

## 6. `consistency_service.py`

- `rebuild()`: drop/recreate collection, re-enqueue everything from SQLite
  (`rebuild_all` outbox event fan-out). Exposed via
  `POST /api/memory/reindex` (doc 04 routes) and used after Qdrant data
  loss — acceptance criterion 6.
- `status()`: counts SQLite-side aggregates vs indexed revisions vs Qdrant
  point counts; detects staleness; exposed via `GET /api/memory/index-status`.
- FTS5 doubles as a consistency reference (row counts / hash spot-checks).

## 7. Degraded modes (spec §20 — all surfaced, never silent)

| Failure | Behavior |
|---|---|
| Qdrant down | SQLite writes succeed; outbox stays pending; retrieval falls back to FTS5 (BM25-ish, no dense); emit `retrieval_degraded`; chat continues. |
| Embedder down | BM25 path unaffected; dense indexing retried; partial-index state visible in index-status. |
| Worker crash | Lease expiry returns rows to pending on restart; no lost events (outbox is durable). |

## Verification

- Unit: outbox idempotency (same revision twice = one row), backoff math,
  deterministic point IDs, chunker policies per source type, lease recovery.
- Integration (real SQLite + real Qdrant in dev profile): write memory →
  outbox → point visible in Qdrant with correct payload; kill Qdrant →
  writes still succeed, pending accumulates, recovery drains; `rebuild()`
  after wiping Qdrant restores identical point counts; retention prune
  respects active-source references; embedder-down → BM25-only partial
  state then completion.
- No memory indexing work occurs inside `before_llm_call` (assert via hook
  instrumentation in tests).
