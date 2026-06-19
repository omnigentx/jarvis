# Workstream 04 — Adaptive Retrieval (Read Path)

Goal: Level 0/1/2 retrieval with hard budgets, hybrid BM25+dense+RRF,
evidence contract, working-context integration, and the per-agent MCP tool
surface. Spec sections: 7 (policy), 8 (hybrid), 9 (evidence), 10 (ledger/
cache), 15 (tools), 16 (API), 22 (telemetry).

## 1. Trigger lexicon — `backend/helpers/memory_triggers.py`

Bilingual (vi/en + code-switched) phrase → retrieval-target mapping,
following the `backend/helpers/crawl_markers.py` pattern (one module, per
locale, never inline at call sites):

```text
"last time" / "lần trước" / "previously" / "hồi trước"  -> episodic
"remember" / "nhớ là" / "from now on" / "từ giờ"        -> pinned
"usual workflow" / "như mọi khi" / "thường làm"         -> procedures
"email from" / "email của" / "meeting yesterday"        -> communications
exact identifiers (regex: file paths, error text, IDs)   -> BM25-first
```

Settings key `trigger_lexicon_overrides` merges on top (doc 02). Unit-test
both languages and code-switched phrasing.

## 2. `intent_router.py` — deterministic level routing

All plain code, ONE module, data-driven (no LLM, no scattered ifs):

- **Level 0 (default, target 60-75% of turns)**: skip retrieval unless a
  lexicon/regex signal fires, the agent explicitly called a memory tool, or
  the evidence ledger says required evidence is missing. Continuing a tool
  loop, social/trivial turns → always Level 0.
- **Level 1**: normalization → mandatory owner/permission filters → BM25
  prefetch (top 30) ∥ dense prefetch (top 30) → RRF → policy scoring →
  evidence selection. No LLM call.
- **Level 1 → 2 escalation**: decided by `quality_gate.py` — numeric
  thresholds (top final score, BM25/dense rank disagreement, result count),
  configurable via `quality_gate_thresholds` setting. Plus explicit
  requests (deep mode, agent asks). Level 2 target <10% of turns.
- **Level 2**: bounded planner LLM — ≤3 subqueries, ≤1 corrective round
  (balanced) / ≤2 (deep), may switch strategy, may fetch full sources, may
  return "insufficient evidence". Forbidden: unbounded reflection, source
  expansion beyond owner scope, silent web access.

## 3. `budget.py`

Hard enforcement of spec §7.4 (per mode economical/balanced/deep): max
retrievals, rounds, subqueries, candidates, evidence items/tokens, stage
timeouts. Budgets are checked by the orchestrator, not trusted to callers.
Timeout → return best-so-far + degraded flag, never hang the turn
(fast p95 target ≤500ms warm).

## 4. Hybrid search — `providers/qdrant_provider.py`

- Mandatory payload filter on every query: `owner_agent_name` = derived
  owner (NEVER from arguments), `status=active`, plus requested
  type/scope filters. There is no code path that queries without the owner
  filter (enforce by constructing filters inside the provider, test it).
- BM25 (sparse) and dense run in parallel; RRF fusion (rank-based, never raw
  score addition); then bounded policy multipliers (owner_scope, authority,
  freshness, confidence, status) — clamp so a low-relevance result can
  never outrank all retrieval evidence (spec §8.4).
- Conditional reranker `BAAI/bge-reranker-v2-m3` (lazy, main process only,
  same rules as embedder): only on rank disagreement / close scores /
  multi-intent / high-risk / deep mode; ≤20 candidates in, ≤5 evidence out.
  Reranker down → RRF results + degraded status (never fail retrieval).
- `providers/sqlite_fts_provider.py`: same `RetrievalProvider` interface,
  used when Qdrant is unreachable (BM25-ish only) — emit
  `retrieval_degraded`.
- `providers/communication_provider.py`: searches authorized communication
  sources (participant check at query time AND fetch time).

## 5. Evidence — `evidence_builder.py`, ledger, cache

- Build `Evidence` exactly per contract (doc 02 / spec §9); compact excerpt
  blocks injected into working context; full source content only via
  progressive disclosure (`memory_fetch` → full `episodic_documents` row —
  one table, no cross-store anchor).
- Evidence ledger lives with the working context: evidence_id,
  content_hash, introduced/last-used turn, source_revision. Router consults
  it BEFORE searching (sufficient evidence present → Level 0). Never inject
  duplicates. The ledger survives compaction; evidence dropped by
  compaction is simply re-retrieved on demand (usually a cache hit) — no
  special preservation machinery.
- Cache key: `owner_agent_name + normalized_query + filters +
  memory_index_revision + retrieval_policy_version`. Invalidate on revision
  change, not TTL-only. Never shared across agents.

## 6. Working-context integration (hooks)

Follow the compaction hook pattern exactly
(`backend/services/context_compaction.py:1271-1330`,
`create_context_compaction_hooks()` / `attach_compaction_hooks_to_all()`):

- New `create_memory_hooks()` merged into the SAME `ToolRunnerHooks`
  composition at both attach sites: `backend/server.py:491,517` and
  `backend/services/dynamic_agents.py:159-162`, combined via
  `merge_hooks()` (`services/sse_progress.py`).
- Since PR #89, compaction registers BOTH `before_llm_call` and
  `on_context_overflow`. The memory retrieval hook is a `before_llm_call`
  hook only; it must NOT interfere with the overflow-recovery retry path
  (emergency compaction must remain able to reissue the call).
- Ordering INSIDE `before_llm_call`: compaction check first, retrieval
  second — fresh evidence is never summarized away in the same turn.
- Evidence blocks carry lightweight markers (identification/dedup only).
- Hook does nothing when `memory.enabled=false` or router says Level 0
  (the dominant path must cost ~zero).

## 7. Pinned memory loading

At session start (and when pinned revision changes), append one clearly
delimited pinned-memory block through the existing session load path
(`backend/services/session_service.py`). Enforce the per-agent pinned token
budget at WRITE time (MemoryService, doc 05) so load time is a plain read.
Do not duplicate content already in the agent instruction or Skills.

## 8. MCP tool surface — `backend/tools/memory_server.py`

Per-agent server wired at spawn with the agent's normalized name in its
config/env — the same pattern as other per-agent server wiring (e.g.
`TEAM_MY_NAME` style env binding in the spawn path). Tool args NEVER carry
identity; any identity-like argument is ignored.

Tools (stable domain contracts, no Qdrant internals):

- `memory_search(query, types?, mode?, limit?)` → evidence list.
- `memory_fetch(evidence_ids)` → full authorized source content.
- `memory_remember(content, type?, scope?)` → creates a CANDIDATE (doc 05),
  never an active memory.
- `memory_forget(record_id, reason)` → archive/delete REQUEST (policy +
  audit, doc 05).
- `procedure_propose(...)` → procedural candidate / Skill proposal.
- `memory_feedback` — DEFERRED, do not implement (user-facing feedback in
  the drawer ships instead).

## 9. REST API — `backend/routes/memory.py`

Spec §16 routes, all `verify_api_key`, all validating the agent exists and
is accessible:

```text
GET  /api/agents/{name}/memories            (+ filters, pagination)
GET  /api/agents/{name}/memories/{id}       (+ /versions)
POST /api/agents/{name}/memories/{id}/rollback | /archive
DELETE /api/agents/{name}/memories/{id}
GET  /api/agents/{name}/memory-candidates
POST /api/agents/{name}/memory-candidates/{id}/approve | /reject
PATCH /api/agents/{name}/memory-candidates/{id}
GET  /api/agents/{name}/retrieval-runs (+ /{id})
POST /api/agents/{name}/memory-search       # manual search from UI
POST /api/memory/reindex
GET  /api/memory/index-status
```

## 10. Telemetry

Every retrieval writes a `retrieval_runs` row (route, stage timings, token
counts, cache_hit, degraded status). Level distribution, evidence tokens,
reranker activation, cache-hit rate are derivable by SQL — no extra
telemetry system.

## Verification

- Unit: router level decisions per signal matrix (vi + en), quality-gate
  thresholds, budget enforcement (incl. timeout best-effort), RRF math,
  bounded policy multipliers, cache key/invalidations, ledger dedup,
  owner-filter-always-present (construct provider, attempt query without
  owner → impossible by construction).
- Integration: hybrid search against real Qdrant returns expected ranks for
  exact-identifier (BM25-first) vs paraphrase (dense-first) fixtures;
  Qdrant down → FTS5 fallback + `retrieval_degraded`; reranker off → RRF
  order.
- E2E: agent recalls a previous decision via hybrid retrieval; exact error
  text found via BM25; Vietnamese paraphrase found via dense; cross-agent
  memory query denied; low-confidence retrieval escalates ONCE then stops;
  evidence chip appears in chat (doc 06).
