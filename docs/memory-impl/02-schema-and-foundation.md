# Workstream 02 — Schema, Contracts, Settings

Goal: all canonical tables, typed domain contracts, settings surface, and
feature flag. Everything downstream (indexing, retrieval, writes, UI) builds
on this. Spec sections: 12 (data model), 3.5/3.6 (tech scope), 18.3
(settings), 25 (module boundaries).

## 1. Tables (SQLAlchemy models in `backend/core/database.py`)

Follow existing conventions: `String(100)` PK UUIDs, `Float` epoch
timestamps, JSON-as-TEXT columns named `*_json`, lightweight migration via
`metadata.create_all()` (+ try/except `ALTER TABLE` for later columns).
Single-user DB — no user_id columns (intentional, see header comment in
`database.py`).

Create exactly the spec §12 schemas:

- `memory_records` — `owner_agent_name` (normalized, from doc 01), type/
  subtype, `subject_scope`, content + normalized_content, status, importance,
  confidence, authority, sensitivity, pinned flag, validity window,
  current_version. **No entity columns** (GraphRAG deferred).
- `memory_versions` — immutable version rows, `UNIQUE(memory_id, version)`.
- `memory_sources` — provenance per (memory, version): source_type/id,
  `source_agent_name`, excerpt, hash, authority.
- `memory_candidates` — `status` is the ONE authoritative candidate state
  (`pending`, `auto_approved`, `approved`, `rejected`, `expired`);
  `requires_curator`, `requires_approval`, `dedupe_key`, `resolution_json`.
- `episodic_documents` — immutable, hash-verified, SELF-CONTAINED copies of
  what the agent experienced. `content` is the durable record served to
  retrieval; `metadata_json` may carry session/snapshot refs as debug info
  only (never a required lookup). `indexed_revision` drives outbox sync.
- `memory_index_outbox` — `UNIQUE(event_type, aggregate_id,
  aggregate_revision)` for idempotency; status
  `pending|in_progress|done|dead`; `attempt_count`, `next_attempt_at`,
  `last_error`.
- `retrieval_runs` — telemetry per retrieval (route, filters, result ids,
  stage timings, token counts, cache_hit, status).
- `communication_records` — email persistence (spec §14):
  `id, channel, sender, recipients_json, subject, body, source_ref,
  created_at`. Written by the email tool-boundary hook (doc 05). Sender and
  recipients are agent names where the party is an agent.

Indexes: every column used in mandatory filters — `owner_agent_name`,
`memory_type`, `status`, `subject_scope`, `created_at` on `memory_records`;
`owner_agent_name`, `session_id`, `content_hash`, `indexed_revision` on
`episodic_documents`; `status`, `next_attempt_at` on the outbox.

Plus an FTS5 virtual table over `episodic_documents.content` +
`memory_records.normalized_content` (degraded fallback / admin search /
consistency reference — spec §8.2). Maintain it with triggers or in the same
write transaction; it must never be the production search path.

## 2. Enums and validation (single module)

`backend/services/memory/models.py` — Python enums/constants used by BOTH
write and read sides (one definition, no string literals scattered):

- `MemoryType`: `pinned | episodic | semantic | procedural` (working context
  is NOT a stored type).
- `MemoryStatus`: `active | superseded | archived | expired | deleted |
  pending_approval`.
- `Authority`: `tool_verified | user_confirmed | agent_observed |
  reported_by_agent | external_document | inferred`. Rule: `inferred` can
  never be pinned nor auto-promoted to a Skill (enforced in MemoryService).
- `SubjectScope` validation: controlled taxonomy `user`, `project:<name>`,
  `agent:<name>`, `system`. Free-form scopes are REJECTED (raise, no silent
  normalization).

## 3. Domain contracts

`backend/services/retrieval/contracts.py` — dataclasses (typed, no dicts
across module boundaries):

- `Evidence` — exactly spec §9: evidence_id, record_id, owner_agent_name,
  memory_type, excerpt, source{type,id,timestamp,uri}, scores{bm25_rank,
  dense_rank, rrf, reranker, final}, authority, confidence, validity.
- `RetrievalBudget` — spec §7.4 fields; constructed only from settings.
- `RetrievalRequest` / `RetrievalResult` (route taken, evidence list,
  degraded flags, timings).
- `MemoryCandidate` payload schema (used by router, compactor, curator).
- `RetrievalProvider` ABC: `search(request) -> list[Evidence]` — implemented
  by qdrant_provider, sqlite_fts_provider, communication_provider; plus
  `GraphProvider` ABC as interface ONLY (no implementation, no callers).

## 4. Settings

Storage: `SystemConfig` rows, category `memory` (pattern:
`routes/settings.py`, `routes/context_compaction.py`).

Routes (`backend/routes/memory_settings.py`, `verify_api_key`):

```text
GET   /api/memory/settings
PATCH /api/memory/settings      # partial update, changed keys only
```

Keys (defaults in parentheses):

```text
enabled (false)                     mode (balanced)            # economical|balanced|deep
auto_capture_preferences (true)     approval_policy (manual)   # manual|auto_low_risk
pinned_token_budget (1500)          evidence_token_budget (2500)
curator_model (low-cost default)    curator_provider / curator_base_url / curator_api_key
                                    # same UX as existing LLM provider selection;
                                    # api key stored is_secret=true
embedding_model (BAAI/bge-m3)       embedding_revision (pinned)
reranker_enabled (true)             qdrant_url (http://localhost:6333)
retention_episodic_days (90)        retention_retrieval_runs_days (30)
trigger_lexicon_overrides ({})      quality_gate_thresholds ({})
```

Budgets per mode = spec §7.4 JSON; store only overrides, derive the rest in
code from one `DEFAULT_BUDGETS` constant.

## 5. Feature flag behavior

`memory.enabled=false` (default) means: no hooks attached, no outbox worker
scheduled, no MCP memory server wired into agents, routes return
`{enabled: false}` (UI shows setup state, doc 06). Flipping to `true` at
runtime takes effect for new sessions/spawns; a restart note in settings UI
is acceptable — do NOT build hot-rewiring of live agents.

## 6. Qdrant dev profile

- Add a pinned-version Qdrant service to the local dev compose / run docs
  (port 6333, volume for persistence). Pin the image tag, never `latest`
  (spec §23).
- Collection (created by the indexer, doc 03): `jarvis_memory_bge_m3_v1`,
  named vectors `dense` (BGE-M3 dim) + `bm25` (sparse), payload indexes per
  spec §8.1. One collection per embedding schema/version — agent isolation is
  payload filters, NOT per-agent collections.
- Backend must start and serve chat normally when Qdrant is unreachable
  (degraded mode — doc 03/04).

## 7. Licensing / OSS hygiene (spec §23)

- Pin model revisions (not just names) for BGE-M3 and the reranker.
- Third-party notices + model attribution; document model size and RAM needs
  before download (BGE-M3 ~2.3 GB, reranker ~1.1 GB disk).
- New deps (`qdrant-client`, embedding runtime) go in `backend/pyproject`
  extras so core install stays lean; GitNexus stays dev-only.

## Verification

- Unit: scope taxonomy validation (rejects free-form), enum round-trips,
  budget derivation per mode, settings PATCH partial-update semantics.
- Integration: fresh DB boot creates all tables + FTS5; second boot is a
  no-op (migration idempotency); settings persist across restart.
- `memory.enabled=false` → zero memory side effects in a full chat turn
  (assert no outbox rows, no hook registration).
