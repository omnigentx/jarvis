# Spawn Event Pipeline — UDS Socket Architecture

## Architecture Overview

```
Child process (spawn_events.py)
  → UDS socket (SPAWN_EVENT_SOCKET)
  → SpawnEventSocketServer (spawn_event_socket.py, limit=4MB)
  → SpawnProgressBridge (spawn_progress_bridge.py)
    → ActivityStreamManager (global SSE broadcast)
    → ProgressEventManager (chat SSE)
    → SQLite agent_activities table
    → SQLite spawn_records table (via AgentRegistryDB)
```

## Key Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `SPAWN_EVENT_SOCKET` | UDS socket path for event IPC | `.runtime/state/spawn_events.sock` |
| `SPAWN_PROJECT_DIR` | Project root for registry, sessions, workspaces | Set by `isolated_spawner.py` |
| `SPAWN_REGISTRY_DB` | SQLite DB path for spawn records | `data/jarvis.db` |
| `TEAM_WORKSPACE` | Team workspace root (set by `team_spawner.py`) | Not set for non-team agents |
| `TEAM_MESSAGES_DIR` | Inter-agent message bus directory | Derived from session |

## Event Types

| Event | Source | Purpose |
|-------|--------|---------|
| `started` | Child process | Agent subprocess launched |
| `agent_ready` | Child process | MCP servers loaded, ready for tasks |
| `runtime_config` | Child process | Full instruction + skills + tools (LARGE, can exceed 64KB) |
| `mcp_status` | Child process | MCP server connection health |
| `thinking` | Child process | Waiting for LLM response |
| `response` | Child process | LLM response received |
| `tool_call` / `tool_result` | Child process | Tool execution lifecycle |
| `result` | Child process | Agent task completed |
| `idle` / `resumed` | Child process | Keep-alive state transitions |
| `agent_paused` / `agent_resumed` | Child process | Pause signal handler |
| `token_usage` | Child process | Token consumption metrics |
| `lifecycle_*` | Parent hooks | Spawn lifecycle phases |

## Pause/Resume Signal Flow

```
Dashboard → POST /api/agents/{name}/pause → PauseManager.pause()
  → SIGUSR1 → PauseSignalHandler (child) → blocks at pause_checkpoint()
  → Emits agent_paused via UDS → Bridge → Dashboard SSE

Dashboard → POST /api/agents/{name}/resume → PauseManager.resume()  
  → SIGUSR2 → PauseSignalHandler (child) → unblocks
  → Emits agent_resumed via UDS → Bridge → Dashboard SSE
```

**PID lookup**: `pause_manager._find_pid()` uses `registry_db.find_by_name()`.
Must search ALL records regardless of status — using `list_running()` breaks resume.

## Process Lifecycle

- Child processes created via `asyncio.create_subprocess_exec()` in `_run_subprocess()` (isolated_spawner.py)
- **NO `start_new_session=True`** → child is in same process group as parent
- **Backend restart kills ALL child agents** — asyncio transport sends SIGTERM on event loop close
- DB status may show `idle` but process is dead — always verify PID with `kill -0 <pid>`
- `run_isolated_agent_background()` wraps the child in an asyncio task (`_bg_task`) that runs in-process

## Messages Directory Resolution

For inject prompt / message bus:
1. **Priority**: spawn record `workspace` field → derive from env
2. **Derivation**: `SPAWN_PROJECT_DIR / .runtime/state/messages/{session_id}/`
3. **Team agents**: `team_spawner.py` sets `TEAM_MESSAGES_DIR` for children

## Socket Buffer Sizing

- Default `asyncio.StreamReader` buffer: **64KB**
- `runtime_config` events contain: instructions + skills + tool definitions → can be **100KB+**
- Current limit: **4MB** set in `spawn_event_socket.py`
- Buffer overflow causes `LimitOverrunError` → socket handler crashes silently
