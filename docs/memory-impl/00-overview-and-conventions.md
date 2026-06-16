# Memory Implementation — Overview and Conventions

Read this file first. It applies to every workstream document in this
directory.

## What is being built

Full implementation of `docs/agent-memory-adaptive-rag-spec.md` (the spec):
per-agent durable memory (pinned / episodic / semantic / procedural) with
adaptive hybrid retrieval (Qdrant BM25 + BGE-M3 dense + RRF), deterministic
write pipeline, approval-gated candidates, and full UI.

This ships as ONE complete implementation. The workstream split below exists
for context-window manageability and technical dependency order — not as
releases. Do not ship a temporary design that a later workstream rewrites.

## Document index and build order

| Doc | Workstream | Depends on |
|---|---|---|
| `01-identity.md` | Agent identity: unique names, role→agent_name | nothing (BLOCKING for all others) |
| `02-schema-and-foundation.md` | Tables, contracts, settings, flags | 01 |
| `03-indexing-pipeline.md` | Outbox, chunker, embeddings, Qdrant | 02 |
| `04-retrieval.md` | Router, hybrid search, budgets, hooks, MCP tools | 03 |
| `05-memory-writes.md` | Candidates, curator, MemoryService, email capture | 02 (parallel with 03/04) |
| `06-ui.md` | Memory tab, evidence drawer, settings, SSE | 04, 05 |
| `07-tests-and-acceptance.md` | Test inventory, eval harness, acceptance | all |

## Non-negotiable rules (from spec + CLAUDE.md)

1. **SQLite is the source of truth.** Qdrant is a rebuildable index. Losing
   Qdrant must never lose memory.
2. **LLMs never write to memory tables.** Only `MemoryService` persists.
   LLMs propose candidates; deterministic code validates and writes.
3. **Owner identity is never LLM-supplied.** The backend derives the owner
   agent from the spawn-time tool binding (doc 04). Any identity-like tool
   argument is ignored.
4. **One authoritative state per decision.** `memory_candidates.status` owns
   candidate state; approval rows are input events. No "try X, fall back to
   Y" for the same fact — probe the authoritative source upfront.
5. **No polling.** All live updates flow through existing SSE
   (`ActivityStreamManager` for global events, progress stream for
   request-scoped chat feedback).
6. **No silent fallbacks.** Degraded modes (Qdrant down, embedder down) are
   logged AND surfaced (`retrieval_degraded` event); mismatches between LLM
   input and DB ground truth `raise`.
7. **English-only code/comments/docs.** UI copy is bilingual via `useLang()`
   (`lang === 'vi' ? … : …`). Trigger lexicons live in one dedicated module
   (pattern: `backend/helpers/crawl_markers.py`), never inline.
8. **Memory indexing stays off the synchronous chat path.** Within
   `before_llm_call`: compaction check first, retrieval second. Indexing is
   async via outbox.

## Existing integration points (verified, do not duplicate)

| Concern | Where | Notes |
|---|---|---|
| Hook attach pattern | `backend/services/context_compaction.py:1271-1330` | `create_context_compaction_hooks()` returns `ToolRunnerHooks(before_llm_call=..., on_context_overflow=...)` — TWO hooks since PR #89. `attach_compaction_hooks_to_all()` (~:1330) merges them. Hooks compose via `merge_hooks()` in `services/sse_progress.py` (it OR-combines `before_llm_call` AND `on_context_overflow`). Attach call sites: `backend/server.py:491,517` and `backend/services/dynamic_agents.py:159-162`. |
| Compaction plan contract | `backend/services/context_compaction.py` (`plan_compaction_llm`, ~:730; skeleton `_plan_skeleton` ~:464) | Compaction is now LLM-driven (PR #89, "LLM-only compaction"). The plan skeleton still reserves `promote_to_memory: []` (~line 474, comment "reserved — no memory subsystem yet"). Extend, don't replace. Because the compactor LLM already analyzes the content, memory-candidate extraction rides on that same pass — no separate rule scan. |
| Raw snapshots (append-only) | `backend/services/context_persistence.py` (`agent_context_snapshots`, ~46-61) | Audit trail only; NOT a required lookup path for memory (spec §4.3). |
| Session persistence | `backend/services/session_service.py` | Per-agent `history_{agent_name}.json` files — MUTABLE (compaction rewrites). Never use message indexes into these files as stable addresses. |
| Canonical message stream | `backend/services/agent_message_stream.py` | Normalized agent name keying; per-agent cursor; ring buffer of recent turns. Episodic projection taps this. |
| Background jobs | `backend/services/background_jobs.py` | `BackgroundJobRunner` ABC + `BackgroundJobScheduler` with DB-persisted state and restart recovery. The index worker is a new runner here (NOT the idle-only TTS pattern). |
| Global SSE | `backend/services/activity_stream.py` | `ActivityStreamManager`, event format `{agent_name, event_type, message, timestamp, data}`. SSE endpoint `/api/agents/activity-stream`. |
| Approvals | `backend/services/approval_service.py` | Generic: `approval_type` + `metadata_json` (~:491,521). Add `memory_candidate` type; cron-specific logic is isolated and untouched. |
| Skills | `backend/services/skill_service.py` | Disk-first `SKILL.md`, runtime refresh, mtime optimistic locking. Procedure publication reuses this. |
| DB conventions | `backend/core/database.py` | Single `data/jarvis.db`, WAL, SQLAlchemy models, `String(100)` ids, `Float` timestamps, `metadata.create_all()` + try/except `ALTER TABLE ADD COLUMN` migrations. |
| Settings storage | `SystemConfig` table + `backend/routes/settings.py` | Category-based; per-feature route pattern exists (`routes/context_compaction.py`). |
| Auth | `core/auth.py` `verify_api_key` | Every new route uses `dependencies=[Depends(verify_api_key)]`. |
| Frontend API | `frontend/src/api.js` (`apiFetch`) | Single wrapper, CSRF on mutating methods. |
| Agent identity normalization | `backend/services/sse_progress.py:95` (`normalize_agent_name`) | Strips `[1]`-style instance suffixes. Becomes the canonical identity function (doc 01). |

## New module layout (spec §25)

```text
backend/services/memory/        # write side: models, memory_service,
                                # candidate_service, provenance, versions,
                                # authorization, sensitivity
backend/services/retrieval/     # read side: contracts, orchestrator,
                                # intent_router, budget, evidence_builder,
                                # quality_gate, cache, providers/
backend/services/indexing/      # outbox_service, memory_index_worker,
                                # projector, chunker, embedding providers,
                                # qdrant_indexer, consistency_service
backend/routes/memory.py
backend/routes/memory_settings.py
backend/tools/memory_server.py  # per-agent MCP tool server
backend/helpers/memory_triggers.py  # bilingual trigger lexicon
```

`providers/graph_provider.py` is an interface only — no implementation
(GraphRAG deferred; entity extraction is backfilled later from stored
content, so NO entity columns ship now).

## Conventions for every workstream

- Match existing code style; route handlers thin; service logic in services.
- Every non-obvious decision gets a **why** comment.
- New SQLAlchemy models go in `core/database.py` next to existing ones.
- Timestamps: `Float` epoch seconds (`datetime.now().timestamp()`).
- IDs: `String(100)` UUIDs generated server-side.
- Tests: backend `backend/.venv/bin/python -m pytest`; frontend `node --test`
  (NOT vitest; extensionless imports break node:test).
- Dev ports: backend 8001, frontend 3001 ONLY. Never touch 3000/8000.
- Backend run: `uv run uvicorn server:app --port 8001` (NEVER `--reload`).
- Feature flag: everything behind `memory.enabled` setting (default `false`)
  until acceptance criteria pass; flag removal is part of acceptance, not an
  afterthought.
