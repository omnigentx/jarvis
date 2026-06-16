# Workstream 05 — Memory Writes (Router → Candidates → MemoryService)

Goal: deterministic write pipeline. LLMs propose; `MemoryService` alone
persists. Spec sections: 11 (write pipeline), 14 (communications), 3.2/3.3
(boundaries), 4.2-4.5 (memory types).

## 1. Memory Router (deterministic, event-driven)

One module (`backend/services/memory/candidate_service.py` entry) consuming
EXISTING event sources — do not create a parallel event system:

| Trigger | Source | Output |
|---|---|---|
| completed logical turn | `agent_message_stream` cursor (same tap as episodic projection, doc 03) | episodic index job (no LLM) |
| completed user message | turn content + matching pinned memories | preference candidate (lexicon: "from now on", "từ giờ", "remember", "nhớ là" — reuse `memory_triggers.py`) |
| context compaction | compactor `promote_to_memory` output (task 2) | decision/fact candidates |
| task completion / idle | spawn lifecycle events (`spawn_progress_bridge` already observes these) → reduced task trace | procedural candidate |
| user correction | corrected answer + correction | superseding candidate |
| explicit "remember this" | user message + minimal context | immediate candidate (deterministic path) |
| repeated workflow evidence | clustering over stored task traces | Skill/MCP proposal candidate |

Every stream keeps a review cursor (pattern: `agent_message_stream`'s
per-agent cursor) so content is never re-sent to a curator.

## 2. Compactor integration

`backend/services/context_compaction.py` — compaction is LLM-driven since
PR #89 (`plan_compaction_llm`, ~:730). The plan skeleton `_plan_skeleton`
(~:464) still returns `promote_to_memory: []` (reserved, ~line 474). Because
the compactor LLM already analyzes the content to build the summary, extend
its structured output to also emit memory candidates in the SAME pass — do
not add a second analysis. Extend the plan contract per spec §11.2:

```json
{"memory_candidates": [{"type": "semantic", "subtype": "...",
  "content": "...", "subject_scope": "project:jarvis",
  "source_message_indexes": [28, 29], "confidence": 0.97, "explicit": true}]}
```

Hard rules: candidate extraction must not weaken summary quality; compaction
succeeds/fails independently of memory processing (wrap candidate handoff in
its own try/except + log, AFTER the compaction result is safely persisted);
candidates are queued (outbox/candidate row), never processed inline in
`before_llm_call`; `promote_to_memory` is a proposal, not a write command.

## 3. Deterministic path vs curator path

No curator LLM for: episodic indexing, schema validation, exact-duplicate
rejection (`dedupe_key` = hash of normalized content + scope + type), source
authorization, version creation, explicit unambiguous "remember this",
compactor candidates passing strict policy.

Curator LLM ONLY for: conflicts with active memory, ambiguous scope/type,
merge decisions, sensitivity classification, generalizing a task trace into
a procedure, Skill/MCP proposal justification.

Curator implementation:
- Configured from settings exactly like the existing LLM provider selection
  (model + provider + optional custom OpenAI-compatible base URL + API key —
  doc 02). Low-cost default. Token usage recorded per run.
- Input: candidate + conflicting/related memories. Output: STRUCTURED
  decision (`create | merge | supersede | reject | needs_approval`) validated
  against a schema. The curator has NO database tools — it returns a
  decision; `MemoryService` executes it after re-validating.
- Curator down → candidate stays `pending`; nothing blocks chat or
  compaction (spec §20).

## 4. `MemoryService` — the only write authority

`backend/services/memory/memory_service.py`. Responsibilities (spec §11.5):

- Derive `owner_agent_name` from trusted runtime/DB state (tool binding /
  event source) — NEVER from candidate payload. On mismatch between an
  LLM-supplied hint and ground truth: `raise` (no silent fallback).
- Validate type, scope taxonomy, source authorization (section 6 below).
- Sensitivity scan (`sensitivity.py`): secret patterns (keys, tokens,
  passwords) → reject or require approval per policy; never persisted into
  embeddable content. Sensitive data is never auto-pinned.
- Dedupe: exact via `dedupe_key`; semantic duplicates → curator.
- Conflict/supersession: never overwrite active memory silently — create a
  conflict candidate; on approval, old record → `superseded` + new version
  chain; both source histories preserved.
- Pinned: enforce per-agent token budget at write time (reject or require
  user to unpin something — surfaced in UI); `inferred` authority can never
  be pinned.
- Create immutable `memory_versions` row per change; `memory_sources` rows
  with authority per spec §14 list; outbox event in the same transaction;
  emit `ActivityStreamManager` events (`memory_created`, `memory_updated`,
  `memory_superseded`, ...).
- `memory_forget` requests: archive (default, reversible) or hard delete
  per policy; deletion propagates to the index via outbox; audit kept per
  retention policy.

## 5. Candidate state machine + approval integration

`memory_candidates.status` is the ONE authoritative state:

```text
pending -> auto_approved   (deterministic path passed strict policy)
pending -> approved        (user approved, or curator decision per policy)
pending -> rejected
pending -> expired         (retention)
```

When `requires_approval=1`: create an approval via the existing
`approval_service` with `approval_type="memory_candidate"`,
`metadata_json={"candidate_id": ...}` (the service is generic — verified
~`approval_service.py:491,521`; cron-specific logic stays isolated).
Approval resolution is an INPUT EVENT: MemoryService consumes it and updates
the candidate row. UI and all readers read ONLY `memory_candidates`.

## 6. Communication capture and authorization

- **Email**: capture at the email tool boundary with an after-tool-call hook
  (same `ToolRunnerHooks` composition as doc 04) into
  `communication_records`. FIRST implementation step: verify which email
  path agents actually use (internal tool vs MCP provider) and hook that
  one; record the decision in code comments. Sender + recipients may index;
  rechecked at query AND fetch time.
- **Meetings**: participants from meeting `config_json` may index
  transcripts (`meeting_transcripts` table).
- **Prompt injections**: the receiving agent may index what it received.
- Promotion to own semantic memory keeps the provenance chain; authority
  reflects the source (`reported_by_agent`, `external_document`, ...);
  `inferred` can never be pinned or auto-promoted to a Skill.

## 7. Procedural learning

- Reduced task traces (goal, key tool calls, outcome — bounded, not raw
  context) stored on task completion.
- Clustering of repeated successful workflows is plain code (similarity on
  trace signatures), not an LLM loop; threshold-crossing clusters become
  procedural candidates via the curator.
- Approved procedures publish as versioned Skills through the EXISTING
  `skill_service` + approval flow. Never auto-publish; never silently
  injected as hidden instructions (spec §4.4). MCP proposals are
  records/documents only — never auto-created servers.

## Verification

- Unit: dedupe_key, scope/authority validation, pinned budget enforcement,
  conflict→supersession chain, sensitivity rejection, candidate state
  transitions (incl. approval input event), curator decision schema
  validation, owner mismatch raises.
- Integration: compaction run produces candidates without affecting the
  compaction result on candidate failure (fault injection); approval
  round-trip via real `approval_service`; rollback restores prior version;
  email hook writes `communication_records` and the projector indexes it;
  Skill proposal reaches `skill_service` draft state.
- E2E: "from now on answer in Vietnamese" → pinned memory after approval →
  next session honors it; user correction supersedes the wrong fact with
  both histories visible; received email becomes private memory with
  provenance; "remember this" works in vi and en.
