# Context Compaction With Versioned Visibility

## 1. Objective

Implement backend-managed context compaction for multi-agent runs using a dedicated external compactor agent/service.

The user experience must be seamless: when an agent reaches the configured context threshold during chat, the system compacts context automatically, reloads the compacted working context, and lets the original agent continue the task without user interruption.

Users must be able to inspect compacted versions, raw snapshots before compaction, token savings, and rollback/debug information from the dashboard.

## 2. Non-Negotiable Requirements

- Raw context must never be physically deleted during compaction.
- Compaction must produce a new working snapshot, not mutate raw snapshot history.
- The original agent must continue the current task naturally after compaction.
- Compaction must be performed by a separate compactor component/agent, but backend must validate and apply the result.
- No polling. Any live compaction status must use existing SSE infrastructure.
- System/developer instructions, latest user request, active goal, unresolved errors, and recent tool result needed for reasoning must not be removed.
- Existing `agent.message_history` and fast-agent serialization must remain compatible with `prompt_serialization.to_json()` and `from_json()`.

## 3. Architecture Decision

Use this flow:

```text
Original Agent
-> backend threshold detector
-> raw snapshot saved
-> external Context Compactor creates plan + summary
-> backend validates plan
-> backend creates working snapshot
-> backend reloads working context into original live agent
-> original agent continues task
```

Do not let the original agent directly delete or rewrite its own context.

The compactor may be an internal rule-based service first, then an LLM/agent later. The MVP should be rule-based with the same public interface expected from the future LLM compactor.

## 4. Existing Code Integration Points

- Primary runtime context: `backend/fast-agent/src/fast_agent/agents/llm_decorator.py:923`
- Current raw snapshot persistence: `backend/services/context_persistence.py:64`
- Current resume path: `backend/services/inject_resume.py:81`
- Current chat snapshot save: `backend/routes/chat.py:474`
- Progress hooks: `backend/services/sse_progress.py:394`
- Streaming monitor source: `backend/services/agent_message_stream.py:123`

`agent.message_history` is the runtime context source. `save_agent_context()` currently serializes it into `agent_context_snapshots`. Resume/inject currently loads the latest raw context JSON and writes it to a temporary history file for subprocess restore.

## 5. Backend Data Model

Keep the existing table:

```text
agent_context_snapshots
```

Meaning: raw, append-only, audit/replay source of truth.

Add:

```text
agent_working_context_snapshots
```

Fields:

```text
id
raw_snapshot_id
parent_working_snapshot_id
run_id
agent_name
session_id
team_name
context_json
message_count
estimated_tokens_before
estimated_tokens_after
saved_tokens
reduction_ratio
compaction_version
policy_version
created_at
```

Add:

```text
context_compaction_events
```

Fields:

```text
id
agent_name
run_id
session_id
raw_snapshot_id
working_snapshot_id
reason
trigger_type
threshold_ratio
max_context_tokens
keep_recent_messages
summary_message
plan_json
validation_json
estimated_tokens_before
estimated_tokens_after
saved_tokens
status
error_message
created_at
```

Optional later:

```text
tool_result_artifacts
```

Purpose: DB-backed raw references for oversized tool outputs instead of relying only on `.tool-outputs`.

## 6. Compaction Config

Add backend config with UI-editable values:

```json
{
  "enabled": true,
  "max_context_tokens": 120000,
  "compact_at_ratio": 0.7,
  "keep_recent_messages": 10,
  "max_tool_result_tokens_in_context": 1500,
  "snapshot_versions_visible": 3,
  "emit_live_status": true
}
```

Default `snapshot_versions_visible` is `3`.

Settings should persist through the existing config DB pattern if available. Avoid env-only config because users explicitly need UI control.

## 7. Compaction Flow

Before an LLM call, estimate total context tokens.

If below threshold:

```text
continue normally
```

If above threshold:

```text
1. save raw snapshot
2. create compaction request
3. run external compactor
4. validate plan
5. apply plan to produce working context
6. save working snapshot
7. save compaction event
8. reload original agent history with working context
9. continue current LLM call
```

Important: do not compact the current pending delta in a way that breaks tool-call/tool-result pairing. Current user message and pending tool result must remain verbatim.

## 8. Compactor Output Contract

The compactor must output strict JSON:

```json
{
  "summary_message": "[COMPACTED_CONTEXT_SUMMARY]\n...",
  "keep_verbatim": [],
  "summarize": [],
  "delete_from_working_context": [],
  "promote_to_memory": [],
  "raw_references": [],
  "risks": [],
  "confidence": 0.0
}
```

Backend validation must reject plans that:

- remove system/template messages;
- remove the latest user request;
- remove a currently pending tool result;
- remove pinned messages;
- produce invalid `PromptMessageExtended` JSON;
- omit required summary sections;
- produce token savings below a configurable minimum unless reason is manual.

## 9. Working Snapshot Format

Working context should contain:

```text
system/template/developer messages
[COMPACTED_CONTEXT_SUMMARY]
important pinned decisions/findings
recent N messages
latest user request/current delta
pending tool result if any
```

Summary sections:

```text
[COMPACTED_CONTEXT_SUMMARY]

Current goal:
User constraints:
Architecture facts:
Important decisions:
Tool findings:
Errors / unresolved issues:
Recent state:
Next actions:
Raw references:
```

## 10. Live UX

During chat, the user should not be interrupted.

If `emit_live_status = true`, send SSE lifecycle events through the existing activity stream:

```text
context_compaction_started
context_compaction_completed
context_compaction_failed
```

These should be lightweight status events, not replacements for `message_turn`.

If compaction fails:

- continue with raw context if still safe;
- if context is too large to continue, return a structured error with a clear message;
- record a failed `context_compaction_events` row.

## 11. Dashboard UX

Add a "Context Versions" area in the existing agent detail/context UI.

Must match the current design system:

- Background: `#0a0d14`
- Card/sidebar surface: `#0c0e15`
- Borders: `#1a1d2e`
- Primary text: `#f0f2f5`
- Accent blue: `#3b82f6`
- Success green: `#10b981`
- Warning: `#f59e0b`
- Error: `#ef4444`
- Font: Inter

UI should show:

- compacted version timeline;
- raw snapshot before compaction;
- working snapshot after compaction;
- token before/after;
- saved tokens;
- percentage reduction;
- reason/trigger;
- compactor confidence;
- risks;
- "Show summary";
- "Show raw refs";
- "Compare before/after".

Settings UI:

- toggle compaction enabled;
- compact threshold ratio;
- max context tokens;
- keep recent messages;
- visible pre-compaction snapshots, default `3`.

UX goal: users can see agents are still reliable, understand what was summarized, and see concrete token savings.

## 12. API Endpoints

Suggested endpoints:

```text
GET /api/agents/{name}/context/versions
GET /api/agents/{name}/context/versions/{version_id}
GET /api/agents/{name}/context/versions/{version_id}/diff
GET /api/settings/context-compaction
PATCH /api/settings/context-compaction
```

All API reads should use existing auth patterns.

Do not expose huge raw context by default. Return metadata first, then lazy-load full snapshot/diff.

## 13. Tests

Backend tests:

- raw snapshot remains unchanged after compaction;
- working snapshot deserializes via `from_json()`;
- system/template/latest user are preserved;
- recent N messages are preserved;
- long tool result is summarized with raw ref;
- failed compaction does not corrupt live agent history;
- version limit defaults to `3`;
- settings update changes visible snapshot count;
- compaction event records token savings.

Frontend tests or manual QA:

- versions list renders empty state;
- compacted version shows token savings;
- before/after diff lazy-loads;
- settings default is `3`;
- changing visible snapshot count updates API query/display;
- SSE status appears without breaking message stream.

## 14. Implementation Order

1. Add data models/tables and migration helpers.
2. Add token estimator and `ContextCompactionConfig`.
3. Add `ContextCompactionManager` with rule-based compactor.
4. Add backend validation and working snapshot persistence.
5. Integrate threshold check in `ToolRunnerHooks.before_llm_call`.
6. Update resume path to prefer latest working snapshot, fallback raw.
7. Add API endpoints for versions/settings.
8. Add dashboard Context Versions UI.
9. Add tests.
10. Later: replace rule-based planner with LLM compactor agent using the same output contract.

## 15. Main Risk

The biggest risk is compacting while a tool-call/tool-result pair is mid-flight. Implementation must be especially careful around `ToolRunnerHooks.before_llm_call`, `runner.delta_messages`, and `agent.message_history`.

The safe rule is: compact historical background context only; preserve the current delta verbatim.

## 16. Implementation addendum (2026-06-10)

Shipped in `backend/services/context_compaction.py` + `backend/routes/context_compaction.py` + the AgentDetail "Context Versions" tab. The spec above is kept verbatim as the original design record; the implementation deviates where the codebase proved the spec wrong or redundant. Deviations and why:

1. **Resume rule changed to newest-wins** (spec §14.6 said "prefer latest working snapshot, fallback raw"). Raw snapshots keep being written on every `chat_complete` *after* a compaction, so an older working snapshot must never shadow a newer raw one — that would resume the agent into the past and silently drop turns. `load_latest_context_json_any()` picks the newest of either kind by `created_at`.

2. **One table instead of two** (spec §5 proposed `agent_working_context_snapshots` + `context_compaction_events`). Every working snapshot maps 1:1 to exactly one event, and ~8 columns were duplicated. A single `context_compaction_events` table (holding `working_context_json`) is the single source of truth. Derived values (`saved_tokens`, `reduction_ratio`) are computed at the API layer, never stored.

3. **Mid-loop safety is stronger than "preserve the delta"** (spec §15). `LlmDecorator._persist_history` appends after *every* LLM call inside a tool loop, so mid-loop `message_history` can already end with an `assistant(tool_calls)` whose results still ride in `runner.delta_messages`. The planner therefore (a) keeps the tail verbatim with a **pair-extended** cut (`_pair_safe_tail_start`), (b) always preserves the final message byte-identical (validated), and (c) force-keeps the latest user text request even when a long tool loop pushed it outside the keep-recent window.

4. **Token numbers**: the *trigger* uses provider-reported context tokens from `usage_accumulator` when available (ground truth), falling back to a chars/4 estimate. Before/after *savings* always use the same chars/4 estimator so the two numbers are comparable — mixing a real "before" with an estimated "after" would fabricate savings.

5. **`max_context_tokens` defaults to 0 = auto** (spec said 120000 static): auto resolves the model's real context window via `usage_accumulator.context_window_size` (ModelDatabase) and survives model swaps; 120000 remains the last-resort fallback.

6. **Loop/concurrency guards** (not in spec): a per-agent asyncio lock prevents stacked compactions, and a per-history-length memo prevents re-planning on every LLM call when a compaction attempt failed or saved too little — it retries only after the history grows.

7. **Settings live at `GET/PATCH /api/context-compaction/settings`** (spec §12 said `/api/settings/context-compaction`, which collides with the generic `/api/settings/{category}` route). Values persist through `config_service` (category `context_compaction`), so audit history and export/import come for free. Added `min_savings_ratio` (spec §8 required a configurable savings floor but §6 omitted the knob).

8. **Design tokens, not hex literals** (spec §11 lists raw hex values that don't match the actual design system). The UI uses `assets/tokens.css` variables (`--bg-*`, `--success`, `--warning`, `--danger`).

9. **Scope**: the `before_llm_call` hook is attached to **in-process agents** (chat path), mirroring the always-on token-persistence hook. Subprocess team agents are not hooked in v1 — they still benefit via the resume path (newest raw/working). `promote_to_memory` stays in the plan contract but is always empty, and §8's "remove pinned messages" validation rule is dropped — no memory or pinning subsystem exists anywhere in the repo to source either from. `tool_result_artifacts` (spec §5 "optional later") is deferred — `raw_references` point at (raw_snapshot_id, message index range).

10. **Failed compactions are recorded, never disruptive**: any planner/validator/apply error writes a `status='failed'` event row, emits `context_compaction_failed`, and leaves the live history untouched (the working context is built from deep copies and applied in one `load_message_history()` call only after validation passes).
