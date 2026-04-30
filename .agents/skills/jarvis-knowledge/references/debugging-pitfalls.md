# Debugging Pitfalls — Spawn, Events, Approval

> Read this before debugging spawn/event/approval issues.

## 1. UDS Socket Buffer Overflow (Silent Event Loss)

**Symptom**: Events stop reaching dashboard after first few events. No errors in logs.

**Root cause**: `runtime_config` event contains full agent instructions + skills + tool definitions → can exceed 64KB default `asyncio.StreamReader` buffer → `LimitOverrunError` crashes socket handler silently.

**Fix location**: `spawn_event_socket.py` → `asyncio.start_unix_server(limit=4MB)`

**How to detect**: 
- Check if `runtime_config` event size > 64KB (it's emitted ONCE per agent spawn)
- Look for missing `runtime_config` and all subsequent events in activity stream
- Socket handler silently stops — no error log because the exception kills the handler coroutine

---

## 2. PID Lookup Filtering Breaks Resume

**Symptom**: Agent shows "Paused" but never resumes after approval. No SIGUSR2 sent.

**Root cause**: PID lookup functions that use `list_running()` or filter `status in (running, pending)` will NOT find paused agents. PID is never found → signal never sent.

**Rule**: Any function that needs to find a PID must use `find_by_name()` (searches ALL records regardless of status).

**Affected files**:
- `pause_manager.py:_find_pid()` — must use `find_by_name()`, NOT `list_running()`
- `routes/inject.py` status filter — must include `idle` and `paused`

---

## 3. Inject Prompt — Workspace Not in Spawn Record

**Symptom**: Inject fails with "has no workspace" even though agent is alive.

**Root cause**: Spawn records don't have a `workspace` field. The workspace is a team-level concept set via `TEAM_WORKSPACE` env var in child process.

**Fix**: Derive messages directory from `SPAWN_PROJECT_DIR + session_id`:
```python
messages_dir = Path(SPAWN_PROJECT_DIR) / ".runtime" / "state" / "messages" / session_id
```

**Pattern**: `team_spawner.py` sets `TEAM_MESSAGES_DIR` for children. The inject route must replicate this path derivation when `workspace` is missing.

---

## 4. Agent Status Mismatch (DB vs Reality)

**Symptom**: Dashboard shows agent as "idle" or "running" but agent process is dead.

**Root cause**: Backend restart kills child processes (same process group, no `start_new_session=True`), but DB record is NOT updated because no cleanup event fires.

**Verification**: Always check PID liveness before trusting DB:
```bash
kill -0 <pid>  # Returns 0 if alive, error if dead
```

**Query to inspect**:
```sql
SELECT data_json FROM spawn_registry WHERE run_id = ?
-- Check "pid" and "status" fields in the JSON
```

---

## 5. Inject Status Filter Must Include Idle/Paused

**Symptom**: Inject rejected with "agent not active" even though agent is alive in keep-alive loop.

**Root cause**: `routes/inject.py` only accepted `status in (running, pending)`. Agents in keep-alive have `status=idle`.

**Rule**: Valid statuses for inject are: `running`, `pending`, `idle`, `paused`.

---

## 6. Dashboard Broadcast Guards

**Symptom**: Dashboard shows agent "Starting..." even though agent was already running.

**Rule**: Only broadcast `started` event for `idle` agents during inject. Broadcasting for `running`/`paused` agents overwrites the current action on dashboard with misleading status info.

**Pattern in inject.py**:
```python
if active.get("status") == "idle":
    # Only broadcast started for idle → running transition
    activity_stream_manager.broadcast({...})
```
