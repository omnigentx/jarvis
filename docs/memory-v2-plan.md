# Memory v2 — LLM-driven recall/capture + GraphRAG on LadybugDB

Status: **locked design, pending implementation.** Replaces (a) the embedding
prototype intent gate, (b) write-time curator conflict-resolution, and (c)
Qdrant as the vector store.

## 0. Decisions locked

1. **Intent gate removed.** LLM decides capture/recall; cheap signals only gate
   COST (asymmetric error: a false-positive wastes one cheap call; a
   false-negative loses a memory → tune loose / use a frequency gate, not a
   content classifier).
2. **Dedup-net = ADD-only + temporal read-ranking** (was: curator supersede).
   Following mem0's 2026 algorithm: nothing is overwritten; changed facts are
   kept with temporal context; conflicts resolve at READ time via recency.
3. **Store = LadybugDB (embedded graph + HNSW vector), Qdrant dropped.**
   SQLite stays the source of truth + FTS5 the keyword/degraded lane.
4. **Graph scope added** (entity linking → GraphRAG), native in LadybugDB.

## 1. Why (root cause of the v1 failures)

`intent_gate.py` classifies messages by cosine to ~15 English prototype
sentences at a 0.67 threshold. **Embedding similarity is TOPIC, not INTENT** —
"What's my job?" (recall) and "I am a software engineer" (capture) are near
identical in embedding space. A prototype list cannot separate them; every new
phrasing is a gap. Symptoms: a self-profile question stored as memory + no
recall ("agent didn't know"); raw messages captured → 20+ near-dup candidates.

## 2. Storage architecture

```
            write authority                  rebuildable index (projection)
   SQLite (source of truth)  ──outbox──▶  LadybugDB (graph + HNSW vector)
     MemoryRecord, Candidate,                Memory/Entity nodes, edges,
     versions, audit, FTS5                   vector index on Memory.embedding
                  │
                  └── FTS5 (BM25 / degraded keyword lane stays here)
```

- **SQLite** unchanged as durable transactional SoT (write authority, versions,
  audit, exact-dedup). The transactional **outbox** + lease/idempotency worker
  stays — only the index TARGET changes from Qdrant to LadybugDB.
- **LadybugDB** (embedded, in-process, Cypher) holds the **rebuildable** graph +
  vector projection. If it underperforms, rebuild into another engine from
  SQLite — the index is disposable, which de-risks adopting an early-stage DB.
- **FTS5** stays for BM25 keyword + the Qdrant-down degraded path (now
  "LadybugDB-down").

### Graph schema (property graph, predefined)
Node tables:
- `Memory(id, owner_agent_name, memory_type, subject_scope, content,
  normalized_content, embedding FLOAT[1024], authority, confidence,
  created_at, valid_from, status)`
- `Entity(id, name, type, normalized_name, created_at)` — person/org/place/
  attribute/topic.

Relationship tables:
- `(Memory)-[:MENTIONS]->(Entity)` — entity linking (retrieval boost, multi-hop).
- `(Memory)-[:ABOUT]->(Entity)` — the memory's subject.
- `(Entity)-[:RELATES {type, valid_from, source_memory}]->(Entity)` — e.g.
  `User -[:WORKS_AT {valid_from}]-> Org`. **ADD-only:** multiple dated edges
  coexist (AcmeCorp@t1, NovaCorp@t2); read-time recency picks current.

Vector index: HNSW on `Memory.embedding` (cosine), via `CREATE_VECTOR_INDEX`.

## 3. Recall — always-retrieve, tail-injected, change-gated, multi-signal

Drop the recall intent gate. Every user turn:

1. **Multi-signal retrieval** over LadybugDB + FTS5, fused (RRF):
   - dense: `QUERY_VECTOR_INDEX` on `Memory.embedding` (HNSW),
   - sparse: FTS5 BM25,
   - **graph/entity signal**: from the dense hits, `MATCH` linked entities and
     pull entity-co-mentioned memories (multi-hop boost) — one Cypher query
     (vector results → graph traversal, native in LadybugDB).
   - **temporal**: recency-weight the fusion so "current state" queries rank the
     newest dated instance (the read-side of ADD-only).
2. **Inject at the TAIL** (append-only), right before the new user turn. Never
   strip/move earlier messages — mid-history mutation breaks the KV prefix
   cache; tail-append costs a miss only on the appended tokens.
3. **Change-gate**: inject a new block only when the retrieved set DIFFERS from
   the last injected set. Stable profile → no new block → prefix stays warm, no
   accumulation. Old blocks are cleaned by compaction; the SoT stays correct.

### Provenance (agent + code can distinguish injected memory; self-contained, no DB lookup)
```
⟦memory:recalled⟧   ← RESERVED sentinel; code detects THIS (startswith/contains)
[System memory recall — not user input] Based on the user's question, these
stored memories may be relevant (reference only): …
```
- Code filters by the reserved sentinel (stable; prose after it is free to
  reword / i18n). Optional belt-and-suspenders: `channels["jarvis:provenance"]
  = "memory_recall"` (persists through save/load history).
- Role: `system` if fast-agent maps mid-conversation system cleanly across
  providers; else framed `user`. Never a plain unmarked `user` (current bug).

## 4. Capture — two-speed LLM extraction (replaces heuristic capture)

LLM extracts; cheap signals only control how OFTEN. Context scales with
complexity. The extractor ALSO emits entities + relations (for the graph) in
the same call — no extra LLM call for entity linking.

| | **Fast lane** | **Slow lane** |
|---|---|---|
| Types | preference, personal fact, standing instruction | workflow/procedural, episodic synthesis, multi-turn decisions |
| Context | recent turns since cursor | the conversation segment being compacted |
| Model | cheap (Haiku / curator model) | capable (main / curator model) |
| When | **debounce** every N turns / ~45s idle (frequency gate) | **composite trigger** (below), piggybacks compaction |
| Output | candidates + entities/relations | candidates + entities/relations |

### Fast-lane cost gate = FREQUENCY, not content
- Run every `N` turns (default N≈4, tunable — the cost lever) OR on idle, over
  turns since the cursor. A frequency gate has **no per-turn content
  classification** → cannot reintroduce the topic≠intent brittleness.
- Optional **negative exclusion** (skip-only-when-CONFIDENTLY-empty: pure
  greetings/acks/very-short). Safe: only skips obvious-nothing; the LLM is the
  precise decider. False-positive = one ~$0.001 call returning "nothing".

### Slow-lane composite trigger
Fire when BOTH (a) enough messages since last extraction AND (b) accrued tokens
> 50% context window — which coincides with the **compaction** threshold. The
slow lane **piggybacks compaction**: it already reads the conversation to
summarize the older segment; extract durable memories from that same segment
before its detail is summarized away. A separate token-tagged extraction call
over the compacted segment (clean attribution); merging into the compaction
call is a future cost optimization.

### Idempotency
- **Extraction cursor** (per session, by message seq): process only
  `(cursor, now]`; advance after each run → linear, not quadratic, cost.
- Slow lane reuses compaction's summarized-segment boundary as a free cursor
  (always moves forward → disjoint windows, no re-capture across compactions).
- **Type-disjoint lanes** (fast=self-contained, slow=synthesis) → no overlap.

### Dedup-net = ADD-only + temporal (NOT curator)
- **No write-time curator call.** On persist: cheap **exact-dedup** (skip if
  `normalized_content + subject_scope` already exists). Otherwise ADD a dated
  node/edge.
- Changed facts (AcmeCorp→NovaCorp) → BOTH kept, dated. **Resolve at READ** via
  recency-weighted ranking; "where did I used to work" still answerable
  (lossless), "where do I work now" ranks NovaCorp.
- Near-dup accumulation is bounded by: cleaner LLM-extracted facts +
  exact-dedup + entity linking collapsing references + **retention pruning**
  (already exists). Kills the v1 near-dup-candidate spam at the source.
- Bonus: removes the per-conflict curator LLM cost and the lossy overwrite that
  made conflicts finicky ("never saw conflict fire").

## 5. Token-usage attribution (user requirement)

Memory's "silent" LLM calls (fast extractor, slow extractor) must be **visible
and filterable** separately from agent usage.
- Tag every memory LLM call with usage `category = "memory"` (+ lane sub-tag
  `memory:fast` / `memory:slow`).
- Wire through the existing token-persistence hook (`current_run_id` /
  token_usage rows): add a `category` column or a memory-namespaced run_id.
- **Token usage** view: a "Memory vs Agents" filter/group with per-lane
  breakdown. (Note: ADD-only removes the curator call, so memory token spend
  drops vs the v1 plan.)

## 6. What changes (file-level)

- `services/retrieval/intent_gate.py` — removed (recall + capture
  classification gone). v1 patches (self-profile prototypes, "?" guard) deleted.
- `helpers/memory_triggers.py` — RECALL/CAPTURE prototypes removed; keep only
  identifier/lexicon fast-paths if still used by routing.
- `services/memory/retrieval_hook.py` — always-retrieve + multi-signal + tail
  append + change-gate + reserved-sentinel provenance + role fix.
- `services/indexing/qdrant_indexer.py` → **`ladybug_indexer.py`**: outbox
  worker upserts Memory/Entity nodes + edges + vector index into LadybugDB.
- `services/retrieval/*` — retrieval reads LadybugDB (vector+graph) + FTS5
  (BM25), RRF fuse, recency rank.
- `services/memory/extractors/` (NEW) — `fast_extractor.py`, `slow_extractor.py`;
  shared structured-output schema emitting facts **+ entities + relations**.
- `services/memory/candidate_service.py` — ADD-only persist (exact-dedup, no
  curator); `conflict.py` curator path removed/demoted.
- `services/context_compaction/*` — hook the slow-lane trigger + segment cursor.
- `services/memory/settings.py` — LadybugDB path/config; drop `qdrant_url`;
  fast/slow extractor model settings; debounce N / idle.
- token persistence + Token usage view — category attribution.
- `fastagent.config.yaml` / deploy — drop the Qdrant container; LadybugDB is
  embedded (a data file), no separate service.

## 7. Migration

- Stand up LadybugDB schema; run `consistency.rebuild` to project all SQLite
  memories → Ladybug nodes + vectors (the index is rebuildable by design).
- Entity nodes/edges for EXISTING memories: a one-time backfill extraction pass
  (or lazily, as memories are re-touched).
- Remove Qdrant container + config after parity is verified.
- Old heuristic-spawned pending candidates: bulk-reject; v2 produces far fewer.

## 8. Testing

- Recall: always-retrieve injects tail block w/ sentinel; change-gate skips
  unchanged; multi-signal fuse (dense+BM25+entity) ranks correctly; provenance
  filterable by sentinel + channel.
- ADD-only/temporal: changed fact keeps both dated nodes; read ranks newest for
  "now", older retrievable for "past"; exact-dedup blocks literal dup.
- Extractors: PassthroughLLM scripts fast/slow output (facts+entities) → real
  candidate + graph write, isolated test DB/Ladybug temp, cleanup.
- Idempotency: two compactions → disjoint windows, no double-store.
- Graph: entity linking edges created; vector→graph traversal returns
  co-mentioned memories; multi-hop query works in one Cypher statement.
- Token attribution: memory calls recorded under category="memory", filterable.
- Eval (gated, real models): self-profile question → recall hit, no capture.

## 9. Phasing (each phase ships tests, independently verifiable)

1. **Recall** (store-agnostic): always-retrieve + tail-append + change-gate +
   provenance. Fixes "agent didn't know" immediately, low blast radius.
2. **Fast-lane extractor + ADD-only** (exact-dedup, drop curator from write).
   Kills the spam. Add recency ranking to retrieval (temporal read-side).
3. **Token-usage attribution** (category + UI filter).
4. **LadybugDB foundation**: schema, `ladybug_indexer`, migrate index from
   Qdrant, retrieval reads Ladybug-vector + FTS5; verify parity; drop Qdrant.
5. **Graph / GraphRAG**: extractor emits entities+relations; entity nodes/edges;
   vector→graph multi-hop retrieval + entity-linking boost. (mem0's +23.1
   multi-hop, +29.6 temporal class.)
6. **Slow-lane** (compaction piggyback) + cursor; cleanup dead intent_gate /
   prototypes.

Order rationale: 1–3 deliver correctness + cost wins on the CURRENT store
(fast value, reversible); 4–5 land the LadybugDB+graph substrate; 6 completes
the synthesis path. Steps 4/5 can move earlier if graph is the priority.
