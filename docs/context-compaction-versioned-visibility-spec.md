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

## 17. Overflow recovery + serving-model window detection (2026-06-11)

Motivated by a field incident: the default model is a 9router **combo alias**
(`openai.coding-agent`) unknown to ModelDatabase, so the auto window detection
silently fell back to 120000 → threshold 84000 → repeated premature
compactions at a real context of ~85–88K while the dashboard showed only the
~24K history estimate. Combos also **rotate** between real models with
different windows (observed: `claude-sonnet-4.5` 200K vs `gpt-5.5` 272K),
which the static window assumption cannot survive.

1. **Serving-model window detection (Tier 1).** OpenAI-compatible responses
   carry the real serving model in `response.model` even when the request
   used an alias. After every call the OpenAI provider resolves that name
   through ModelDatabase (normalizing gateway dot-versions,
   `claude-sonnet-4.5` → `claude-sonnet-4-5`) and updates
   `usage_accumulator.set_context_window_size()` — unless an explicit
   `context=1m`-style override is active. The threshold side
   (`_context_token_limit`) tracks the **minimum window seen per agent per
   session**: after a rotation to a smaller model the threshold never climbs
   back, because the next call may land on the small model again. An UNKNOWN
   window **fails loud instead of falling back**: auto-compaction is skipped
   and the misconfiguration is surfaced once per agent per session (ERROR
   log + a `status='failed'` event row on the Context Versions tab + the
   SSE failure toast) — thresholding against an invented 120000 either
   compacts prematurely or never, and the operator needs to know either
   way. The 120000 constant survives only as a chunk-sizing default inside
   the LLM compactor, where it affects efficiency, not policy.

2. **Context-overflow recovery (reactive backstop).** Rotation can still
   overflow mid-conversation (context built at 272K, next call lands on a
   200K model). Overflow errors are classified **fatal** in the provider
   retry loop (`is_context_overflow_error` — retrying an identical payload is
   futile) and propagate to the tool runner, where a new
   `ToolRunnerHooks.on_context_overflow` hook runs
   `maybe_compact_agent(reason="context_overflow")` — threshold and
   grow-memo bypassed, `enabled=false` respected — and the LLM call is
   reissued **once** on the rebuilt payload. A second consecutive overflow
   propagates (compaction could not shrink below the window).

3. **LLM compaction ONLY — the rule-based planner was removed entirely
   (2026-06-11 final revision).** Earlier addenda kept a rule-based
   compactor as the MVP/fallback (spec §3 "rule-based first"). The
   product decision is that semantic preservation matters more than
   speed/cost: a blind cut-and-paste summary silently loses the decisions
   a resumed agent needs. So `plan_compaction`/`_build_summary` are gone;
   `plan_compaction_llm` is the only planner. The structural helpers
   (`_plan_zones`, `_tail_truncations`, `_plan_skeleton`,
   `build_working_context`, `validate_working_context`) remain — they are
   compactor-agnostic plumbing, not "rule-based compaction".

   The compactor is a **dedicated LLM agent** built from the
   `compactor_model` setting (Settings → Context Compaction; reuses the
   origin agent's provider config/keys) — choose a larger-window model so
   one call can feed more history at once. Empty setting = degraded
   default: the origin agent's own LLM via an isolated side-channel call.

   When the middle zone exceeds the compactor input budget
   (`compactor_input_ratio ×` the COMPACTOR's window — UI-configurable,
   default 0.7), it is split into **pair-safe chunks** (a cut never
   separates a tool call from its result) summarized concurrently by
   independent calls, then merged deterministically into the single
   required summary message — no second LLM merge pass (extra cost, new
   failure mode, no structural gain). Stream listeners are detached around
   the whole batch so compactor tokens never leak into the live chat
   stream.

   **No fallback — fail loud everywhere** (addendum item 1's philosophy,
   now applied without exception): if the compactor fails (no LLM, timeout,
   unparseable JSON) the compaction is a `status='failed'` event + SSE
   toast, and the agent continues on raw context. For overflow recovery
   the `on_context_overflow` hook then returns False so the original
   overflow error propagates — an honest failure beats a silently
   degraded summary. Events record `compactor: "llm"` and
   `compactor_model` in `plan_json`.

4. **`File references:` summary section (new required section).** Paths the
   agent read/edited must survive compaction so the resumed agent can
   re-open its working set. Populated from BOTH sources, unioned: the
   compactor LLM's `file_references` extraction AND a deterministic scan
   of tool-call arguments (`_file_references`) — a path the model
   overlooks still survives.

5. **`compactor_input_ratio` config (UI-editable, range 0.1–0.9, default
   0.7).** Fraction of the compactor's window one summary call may read.
   Larger = fewer chunks (faster, cheaper) but riskier of overflowing the
   compactor. Lives in the config DB like every other compaction setting.

6. **Compactor is a FAITHFUL compressor, never a decision-maker
   (2026-06-11).** Field incident: during a long "read many files" turn
   the LLM compactor wrote `Recent state: read phase complete, awaiting
   user's questions` / `Next actions: answer the user` — a verdict the
   conversation did not support (the user had said "keep going"). The
   resumed agent, trusting the summary, ended its turn with empty output.
   Root cause: the prompt invited the model to JUDGE task state and
   prescribe next steps. First fix attempt hardcoded a "you are mid-task,
   do not stop" header — rejected as scenario-overfitting (it would force
   the agent to fabricate work when a task is genuinely done). General
   fix: the compactor prompt now forbids interpretation — record only what
   the excerpt explicitly establishes, never decide whether the task is
   complete or what to do next; "next_actions" stays empty unless the
   conversation explicitly states pending steps. The summary header is
   purely descriptive ("a faithful, compressed summary of earlier turns"),
   with NO steering in either direction. Whether to continue, stop, or ask
   the user remains the agent's decision, driven by the preserved goal +
   verbatim recent messages exactly as with full history — so it adapts to
   any task state instead of being hardcoded to one outcome.
