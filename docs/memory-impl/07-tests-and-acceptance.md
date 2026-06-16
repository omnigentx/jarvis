# Workstream 07 — Tests, Evaluation Harness, Acceptance

Goal: prove the whole feature against spec §22 (observability/eval), §26
(tests), §27 (acceptance). Workstream docs 01-06 each carry their own
verification lists; this doc adds the cross-cutting harness and the final
gate.

Test runners: backend `backend/.venv/bin/python -m pytest`; frontend
`node --test` (NOT vitest; extensionless imports break node:test). Dev
ports 3001/8001 only.

## 1. Happy-path-first rule (CLAUDE.md §6)

Every user-facing flow ships with ≥1 e2e test through PRODUCTION code — no
mocking the memory subsystem itself. Refuse `autouse=True` fixtures that
stub memory/retrieval; integration fixtures (real SQLite, real Qdrant dev
container) are opt-in per test module. A mock-only test for a cross-layer
invariant counts as NO coverage.

## 2. Offline evaluation harness (spec §22)

Build fixtures + a pytest-marked eval suite (`-m memory_eval`, excluded
from default runs, used before flipping `memory.enabled` default):

Fixture set (seeded into a throwaway DB + Qdrant collection):
- exact identifier queries (error strings, file paths, ticket IDs);
- Vietnamese/English paraphrase pairs of the same fact;
- old decisions vs superseding corrections (staleness);
- user preferences (pinned);
- conflicting memories;
- unauthorized cross-agent queries (MUST return nothing);
- insufficient-evidence cases (MUST say so, not hallucinate matches);
- workflow/procedure retrieval.

Metrics: Recall@K, MRR/nDCG, source correctness, authorization correctness
(zero cross-agent leaks — hard fail), answer groundedness on the e2e
samples. Gate: hybrid (BM25+dense+RRF) must beat BM25-only on the fixture
set before hybrid is the default (acceptance #13).

Operating targets (assert via `retrieval_runs` telemetry over the e2e
suite):

```text
Level 0: 60-75% of turns      Level 2: <10% of turns
balanced evidence <= 2500 tokens; corrective rounds <= 1
fast retrieval p95 <= 500 ms (warm)
```

## 3. Cross-cutting integration tests (beyond per-doc lists)

- **Rebuild**: write memories → wipe Qdrant volume → `POST /api/memory/
  reindex` → identical retrieval results (acceptance #6).
- **Crash recovery**: kill the backend mid-outbox-batch → restart → all
  pending events drain exactly once (lease recovery + idempotent upserts).
- **Compaction independence**: force candidate-pipeline failure during a
  compaction → compaction result unaffected (acceptance #8); force
  compaction failure → no candidate rows leak.
- **Model revision migration**: bump embedding revision in settings → new
  collection created, old one untouched, rebuild path works, no silent
  mixed-model collection (spec §20).
- **SSE lifecycle**: each §17 event observed end-to-end
  (ActivityStreamManager → SSE endpoint → store) for the 4 frontend-handled
  types; remaining types asserted at the stream level only.
- **Restart recovery for approvals**: pending memory_candidate approval
  survives restart and resolves correctly afterwards.

## 4. E2E flows (spec §26 — full list, each a named spec)

1. Short session: explicit "remember this" (vi AND en) → candidate →
   approve → pinned → honored in a NEW session.
2. Long session compacts; candidates emitted; zero user-visible
   interruption; summary quality unchanged.
3. Agent recalls a previous decision (hybrid retrieval) with evidence chip
   + drawer + correct provenance.
4. Exact error text recalled via BM25; Vietnamese paraphrase recalled via
   dense.
5. Cross-agent memory query denied (tool AND REST level).
6. Received email → `communication_records` → indexed → recalled with
   provenance chain.
7. Low-confidence retrieval escalates exactly once, then stops or returns
   "insufficient evidence".
8. Qdrant outage mid-session: chat continues, `retrieval_degraded` chip,
   writes preserved, recovery drains.
9. User inspects, edits (new version), rejects a candidate, rolls back a
   memory — all from the Memory tab, all reflected via SSE without reload.
10. Duplicate agent-name spawn rejected with explicit error; resume/
    auto-wake of same-named agent unaffected (doc 01).

## 5. Acceptance checklist (spec §27 — final gate, all must hold)

1. Every memory record: one trusted owner (unique normalized agent name) +
   provenance.
2. No cross-agent private-memory retrieval (eval suite: zero leaks).
3. Most turns: no planner/curator LLM call (telemetry targets above).
4. Fast retrieval = BM25 + dense via RRF.
5. Hard budgets enforced (calls, time, results, tokens).
6. SQLite fully rebuilds Qdrant.
7. Qdrant failure degrades safely; no lost writes.
8. Compaction and memory creation fail independently.
9. User can inspect all memories, candidates, versions, sources, retrieval
   evidence.
10. Procedural memory cannot silently publish/modify a Skill/MCP.
11. All live updates via SSE; zero polling introduced.
12. No graph DB required.
13. Offline eval: hybrid beats BM25-only before hybrid is default.
14. Telemetry proves Level 2 is exceptional, not the default.

Plus repo-level gates: `gitnexus_impact` run for modified symbols (HIGH/
CRITICAL warnings addressed, not ignored); `gitnexus_detect_changes()`
before each commit; `npx gitnexus analyze --embeddings` after commits (the
PostToolUse hook handles this — embeddings exist, so the flag matters);
bilingual UI sweep; CLAUDE.md post-change checklist on every PR.
