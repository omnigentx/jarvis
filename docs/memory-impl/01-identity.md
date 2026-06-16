# Workstream 01 — Agent Identity (BLOCKING)

Goal: one trusted identity per agent = the **normalized agent name**. Role
labels are display metadata, never identity. Duplicate agent names cannot be
created. Everything else in this feature keys memory on `owner_agent_name`,
so this lands first.

## Background (verified facts)

- Agents are identified by name strings across all existing tables
  (`agent_activities`, `agent_context_snapshots`, `spawn_records`, ...).
  There is no agent UUID. We standardize on names — do NOT introduce a
  second identity system.
- `normalize_agent_name()` in `backend/services/sse_progress.py:95` strips
  `[1]`-style instance suffixes. It is already used by all 4 progress hooks
  and `humanize_agent_name` (impact: MEDIUM, all callers inside
  `sse_progress.py`).
- Team spawns already generate unique names:
  `_generate_unique_agent_name()` in
  `backend/fast-agent/src/fast_agent/spawn/team_spawner.py:65` produces
  `'{RandomName} [{Role}]'` and checks the live registry + stored sessions.
- Persistent spawns already reject duplicates:
  `_persist_dynamic_agent_to_db()` in
  `backend/fast-agent/src/fast_agent/spawn/servers/agent_spawner_server.py:829-877`
  raises `ValueError` on duplicate — but only EXACT match, not normalized.
- **The bug**: isolated spawns have no name parameter at all.
  `spawn_and_run_isolated(task, ..., role="")` (same file, :351-406) passes
  `role` down, and `backend/fast-agent/src/fast_agent/spawn/isolated_runner.py:508`
  does `agent_name = os.environ.get("TEAM_MY_NAME", "") or role` — the role
  label BECOMES the identity. Two spawns with the same role = duplicate
  agent_name in `spawn_records`, activities, and SSE.
- Resume paths carry no name input: `restart_spawn(run_id)` (:524) and
  `resume_spawn(run_id, follow_up_task)` (:597) load the record from
  `_registry.get(run_id)`; `resume_with_inject` (`backend/services/inject_resume.py`)
  receives a `spawn_record` dict from the DB. Auto-wake
  (`_check_and_resume_on_inbox` in `isolated_spawner.py`) likewise respawns
  from an existing record. **No uniqueness check applies there by
  construction — they cannot introduce a new name.**

## Tasks

### 1. Promote the normalizer to a shared helper

Create `backend/helpers/agent_identity.py`:

```python
def normalize_agent_name(name: str) -> str: ...   # moved logic
```

- Move the implementation from `sse_progress.py:95`; keep a re-export in
  `sse_progress.py` so its 5 internal callers and any external importers are
  untouched (`from helpers.agent_identity import normalize_agent_name`).
- Behavior must not change — every SSE event flows through this function.
  Add unit tests pinning current behavior (suffix `[1]`/`[2]` stripped,
  team role suffix `[SA]` KEPT — `"Khoi [SA]"` is a full identity, instance
  suffixes are not).

### 2. Shared uniqueness check

Add to `backend/fast-agent/src/fast_agent/spawn/` (importable by all three
creation entry points) — conceptually:

```python
def ensure_unique_agent_name(name: str, *, registry, db_path: str) -> None:
    """Raise ValueError if normalize(name) collides with:
    - a live agent in the spawn registry (any status considered live);
    - a spawn_records row with status running/idle;
    - an agent_definitions row (persistent dynamic agents);
    - a static agent defined in agent.py (compare against the runtime
      agent list when available).
    Comparison is on normalized names, case-sensitive.
    """
```

Why one function: a fourth creation entry added later must not re-implement
the check. The audit test (task 5) enforces that.

### 3. Apply at the three creation entry points (and only there)

| Entry point | File | Change |
|---|---|---|
| `spawn_agent` | `agent_spawner_server.py:902` | Call `ensure_unique_agent_name(name, ...)` before `_persist_dynamic_agent_to_db`. Keep the existing exact-match check inside `_persist...` as a DB-level backstop. |
| `spawn_and_run_isolated` | `agent_spawner_server.py:351` | See task 4 (signature change), then check the resolved `agent_name`. |
| `spawn_team_members` / team path | `team_spawner.py` (`_generate_unique_agent_name` at :65, used at :484,562) | Route its collision check through `ensure_unique_agent_name` so all paths share one definition of "taken". Generated-name retry loop stays. |

Resume/restart/auto-wake paths (`restart_spawn`, `resume_spawn`,
`resume_with_inject`, `_check_and_resume_on_inbox`, `spawn_and_run_background`
when resuming) are NOT modified — they take `run_id`/`spawn_record` and have
no name input. Add a comment at the check site documenting this asymmetry.

### 4. `spawn_and_run_isolated`: `role` → `agent_name`

- Add `agent_name: str = ""` parameter. `role` stays as a display label only.
- Resolution order for identity:
  1. explicit `agent_name` argument (validated unique);
  2. else generate a unique name from `role` using the team pattern
     (`'{RandomName} [{role}]'` via the shared generator) — never use the
     bare role string as identity.
- Fix `isolated_runner.py:508`: `agent_name = TEAM_MY_NAME or <resolved
  unique name>` — the bare `role` fallback is removed.
- Update the tool docstring so LLM callers know `agent_name` is optional and
  auto-generated; `role` is for humans.
- `run_isolated_agent` upstream impact is HIGH (11 symbols): d=1 callers
  `spawn_and_run_isolated`, `_bg_task`, example `single_spawn.py`; d=2
  `run_isolated_agent_background`; d=3 includes team `_spawn_single_agent`,
  auto-wake `_check_and_resume_on_inbox`, `resume_with_inject`,
  `restart_spawn`, `resume_spawn`. Thread the resolved name through without
  changing resume semantics; run the existing spawn/resume test suites.

### 5. Audit test

Pattern already exists:
`backend/tests/test_services/test_inject_resume.py::test_audit_all_run_isolated_agent_background_callers_forward_overrides`.
Add an equivalent test that statically scans the spawn server module(s) and
asserts every creation entry point (tool functions that accept a NEW name or
role) calls `ensure_unique_agent_name`. The test must fail if a new creation
tool is added without the check.

### 6. Standardize role-vs-name fallback chains

These read `role` where identity is meant. After task 4, `agent_name` is
always present on new records; the fallbacks remain ONLY for pre-existing DB
rows — annotate them as legacy-read shims, do not delete silently:

| Site | Current code |
|---|---|
| `backend/routes/agents.py:162` | `record.get("agent_name") or record.get("role") or ""` |
| `backend/routes/agents.py:712,785,791` | same pattern |
| `agent_spawner_server.py:179` | `getattr(event, "agent_name", "") or getattr(event, "role", "")` |
| `backend/services/spawn_progress_bridge.py:307` | `"role": agent_name  # backward compat` — KEEP the field (frontend may read it) but it is display-only |
| `spawn_progress_bridge.py:342,416,559` | parameters literally named `role` that carry agent_name — rename params to `agent_name` (internal-only; verify with grep that no kwargs callers use `role=`) |

Rule: new writes always set `agent_name`; `role` is never written as a
substitute for it.

### 7. Rename = explicit migration only

No automatic rename support. Document in code (where agent definitions are
updated) that renaming requires a migration that rewrites
`owner_agent_name` across memory tables. Out of scope to build the tool now.

## Verification

- Unit: normalizer behavior pinned; `ensure_unique_agent_name` collision
  matrix (live registry / spawn_records / definitions / static, normalized
  vs exact); generated-name fallback for isolated spawns.
- Audit test (task 5) green and demonstrably fails on an unchecked entry.
- Integration: spawn duplicate via each of the 3 entries → explicit error
  (not silent suffix); resume/restart/auto-wake of an existing agent still
  works (run existing `test_inject_resume.py` and spawn test suites).
- E2E: spawn agent named X, ask Jarvis to spawn X again → user-visible
  error; resume X after idle → succeeds.
