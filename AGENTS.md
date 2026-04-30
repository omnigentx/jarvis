# Jarvis AI Assistant — Repository Guidelines

- Repo: Private monorepo (`jarvis_v3`)
- Core runtime: [fast-agent](https://fast-agent.ai/) (Python, git submodule at `backend/fast-agent`)
- Always reference files repo-root relative (e.g. `backend/routes/chat.py:187`); never absolute paths.

## Core Principles

1. **Realtime monitoring** — SSE, WebSocket, or equivalent push protocol. **Absolutely no polling.** All agent status, tool execution progress, and activity events must stream to clients in real-time via `services/activity_stream.py` (ActivityStreamManager) and `services/sse_progress.py` (ProgressManager).

2. **Production-ready code** — Every feature must handle edge cases: connection drops, auth failures, SSE reconnection with exponential backoff, timeout handling, graceful degradation. No TODO placeholders or "happy path only" implementations.

3. **Clarification before implementation** — When requirements are ambiguous, always present specific questions with options to the user before writing code. Prefer numbered questions that are actionable.

4. **Clean architecture** — Code must be modular, well-separated, and easy to extend:
   - Keep files under ~500 LOC; split when exceeding.
   - Extract reusable composables/services; avoid copy-paste.
   - Use clear naming that reflects domain concepts (not generic names).
   - Every component should have a single responsibility.

5. **fast-agent best practices** — This project is built on [fast-agent](https://fast-agent.ai/). Always:
   - Reference official docs before implementing agent features: [Tool Runner](https://fast-agent.ai/agents/tool_runner/), [Prompting](https://fast-agent.ai/agents/prompting/), [Instructions](https://fast-agent.ai/agents/instructions/)
   - Use `ToolRunnerHooks` for monitoring and progress tracking (see `services/spawn_progress_bridge.py` for the pattern).
   - Leverage agent cards (`.fast-agent/agent_cards/`) for dynamic agent configuration.
   - Understand the session/history model via `services/session_service.py`.
   - Never bypass fast-agent's built-in capabilities; extend through hooks, not monkey-patching.

6. **Evidence-based debugging** — Never guess or assume root causes. Always:
   - Read source code of the relevant module before concluding.
   - Provide file paths and line numbers when identifying issues.
   - Show logs/stack traces as evidence.
   - If the bug is in a dependency (e.g. `fast-agent` core), read the submodule source at `backend/fast-agent/`.

## Project Structure

```
jarvis_v3/
├── backend/                    # FastAPI + fast-agent runtime (Python)
│   ├── server.py               # FastAPI application entry point
│   ├── agent.py                # fast-agent agent definitions (@fast.agent decorators)
│   ├── routes/                 # API endpoints
│   │   ├── agents.py           # /api/agents — list, detail, pause/resume, team, skills, context
│   │   ├── chat.py             # /api/chat, /api/chat-stream (SSE), /api/chat-audio
│   │   ├── inject.py           # /api/agents/{name}/inject — prompt injection (MessageBus + resume)
│   │   ├── agent_timeline.py   # /api/agents/{name}/timeline (SSE)
│   │   ├── approvals.py        # /api/approvals — human-in-the-loop approval system
│   │   ├── scheduler.py        # /api/scheduler — cron jobs CRUD + SSE stream
│   │   ├── notifications.py    # /api/notifications — push notification management
│   │   ├── tts.py              # /api/tts/* — text-to-speech streaming
│   │   ├── stories.py          # /api/stories/* — audiobook reader + crawl
│   │   ├── library.py          # /api/library — book library management
│   │   ├── sessions.py         # /api/sessions — conversation history
│   │   ├── token_usage.py      # /api/tokens — LLM token usage metrics
│   │   └── auth.py             # /api/auth — login, API key check
│   ├── services/               # Business logic & shared state
│   │   ├── shared_state.py     # Singleton refs (agent_app, spawn_bridge, registry_db, etc.)
│   │   ├── session_service.py  # Session management (resume_and_send)
│   │   ├── sse_progress.py     # SSE progress manager + create_progress_hooks()
│   │   ├── activity_stream.py  # ActivityStreamManager (realtime agent status SSE)
│   │   ├── spawn_progress_bridge.py  # Subprocess events → SSE bridge + team completion
│   │   ├── spawn_event_socket.py     # Unix socket server for MCP subprocess events
│   │   ├── inject_resume.py    # Resume non-running agents with context from DB
│   │   ├── context_persistence.py    # Save/load agent context windows to/from SQLite
│   │   ├── pause_manager.py    # Agent pause/resume state management
│   │   ├── approval_service.py # Human-in-the-loop approval workflow
│   │   ├── cron_scheduler.py   # Cron job execution engine (APScheduler)
│   │   ├── background_jobs.py  # Background task management
│   │   ├── meeting_events.py   # Meeting event stream manager
│   │   ├── meeting_hooks_bridge.py   # Meeting hooks → SSE bridge
│   │   ├── dynamic_agents.py   # Dynamic agent loading from agent_cards/
│   │   ├── crawl_poller.py     # Story crawl polling/monitoring
│   │   ├── tts.py              # TTS provider (Edge / ElevenLabs)
│   │   ├── tts_pregen_job.py   # TTS pre-generation background job
│   │   ├── pregen_stream.py    # TTS pre-gen SSE stream helper
│   │   ├── library_manager.py  # Book library CRUD + progress tracking
│   │   ├── pricing.py          # LLM token pricing calculations
│   │   └── history.py          # TTS cache management
│   ├── core/                   # Infrastructure
│   │   ├── auth.py             # API key authentication (Bearer + query param)
│   │   ├── database.py         # SQLite database schema + migrations
│   │   ├── agent_registry_db.py # Agent spawn registry (SQLite, single source of truth)
│   │   └── logging_config.py   # Structured logging setup
│   ├── tools/                  # Custom MCP servers (Python)
│   │   ├── approval_server.py  # Human approval MCP tool
│   │   ├── calendar_server.py  # Google Calendar integration
│   │   ├── gmail_server.py     # Gmail read/send integration
│   │   ├── cron_server.py      # Cron management MCP tool
│   │   ├── iot_server.py       # IoT device control (Tuya)
│   │   ├── story_server.py     # Story crawl/TTS MCP tools
│   │   ├── library_server.py   # Library management MCP tool
│   │   ├── media_server.py     # Media processing utilities
│   │   ├── crawl_resume.py     # Resume crawl jobs
│   │   └── time_server.py      # Date/time with timezone
│   ├── .fast-agent/            # fast-agent runtime data
│   │   ├── agent_cards/        # Dynamic agent card YAML files
│   │   ├── skills/             # Agent skills (markdown + resources)
│   │   └── sessions/           # Session history files
│   ├── data/                   # Runtime data (gitignored)
│   │   ├── jarvis.db           # SQLite database (all tables)
│   │   ├── audio_cache/        # TTS audio cache files
│   │   └── stories/            # Story content files
│   ├── config/                 # Credentials & runtime config
│   ├── fastagent.config.yaml   # fast-agent main config (model, MCP servers, logging)
│   ├── fastagent.secrets.yaml  # Local dev secrets (API keys, base_url overrides)
│   └── fast-agent/             # Git submodule → fast-agent core
│
├── dashboard/                  # Ops Dashboard (Vue 3 + Vite + Tailwind v4)
│   └── src/
│       ├── views/              # Page components
│       │   ├── TeamMonitor.vue     # Multi-agent team monitoring + inject
│       │   ├── ChatView.vue        # Chat interface
│       │   ├── AgentsList.vue      # Agent list overview
│       │   ├── AgentDetail.vue     # Agent detail + context snapshots
│       │   ├── ApprovalsView.vue   # Human approval queue
│       │   ├── SchedulerDashboard.vue  # Cron job management
│       │   ├── NotificationList.vue    # Notification inbox
│       │   ├── NotificationDetail.vue  # Notification detail + TTS
│       │   ├── TokenUsage.vue      # Token usage analytics
│       │   ├── StoriesView.vue     # Story library browser
│       │   └── StoryReaderView.vue # Audiobook player
│       ├── components/         # Reusable UI components
│       ├── composables/        # Vue composables
│       │   ├── useActivityStream.js   # Agent activity SSE
│       │   ├── useChatStream.js       # Chat SSE stream
│       │   ├── useRealtimeStream.js   # Generic SSE helper
│       │   ├── useMeetingStream.js    # Meeting events SSE
│       │   ├── useMeetingList.js      # Meeting data fetcher
│       │   ├── useAudioPlayer.js      # Audio player state
│       │   ├── usePregenStream.js     # TTS pre-gen SSE
│       │   ├── useBreakpoint.js       # Responsive breakpoints
│       │   └── useToast.js            # Toast notification
│       ├── stores/             # Pinia stores
│       │   ├── agents.js           # Agent state + team data
│       │   ├── chat.js             # Chat state + conversations
│       │   ├── approvals.js        # Approval queue state
│       │   └── audioPlayer.js      # Audio player state
│       ├── api.js              # REST client with Bearer auth
│       └── router.js           # Vue Router setup
│
├── build/                      # CI/CD scripts
├── .github/workflows/          # GitHub Actions
├── .agents/skills/             # AI agent skills (for agents working ON this repo)
├── docker-compose.yaml         # Production deployment (backend + web)
└── Makefile                    # Deploy shortcuts
```

## Technology Stack

| Layer | Technology | Notes |
|-------|-----------|-------|
| Backend runtime | Python 3.11+ / FastAPI / Uvicorn | `uv` for dependency management |
| Agent framework | [fast-agent](https://fast-agent.ai/) | Git submodule at `backend/fast-agent` |
| LLM provider | OpenAI-compatible via CLIProxyAPI | Key rotation + load balancing on port 8317 |
| Dashboard | Vue 3 + Vite + Tailwind CSS v4 | SPA with Pinia state management |
| Database | SQLite | `backend/data/jarvis.db` (all tables in one file) |
| Deployment | Docker Compose (host networking) | Persistent volumes for sessions/data/logs |
| CI/CD | GitHub Actions | Auto-deploy on tag push |

## Build, Test, and Development Commands

### Backend
```bash
cd backend
uv sync                           # Install dependencies
uv run uvicorn server:app --host 0.0.0.0 --port 8000  # Run backend (dev)
uv run pytest tests/              # Run tests
```

### Dashboard
```bash
cd dashboard
npm install                       # Install deps
npm run dev -- --port 3000        # Dev server (proxies /api → backend:8000)
npm run build                     # Production build
```

### Docker (Production)
```bash
make deploy                       # Update submodule + build + run
make build                        # Build all services
docker compose logs -f jarvis-backend  # Tail logs
```

## Coding Style & Conventions

### Python (Backend)
- Follow PEP 8. Use type hints everywhere.
- Async/await for all route handlers and service calls.
- Structured logging via `logging.getLogger(__name__)` with `[PREFIX]` format:
  ```python
  logger.info(f'[REQUEST] POST /chat-stream cid={cid} msg="{preview}"')
  logger.info(f'[RESPONSE] POST /chat-stream cid={cid} duration={dur:.1f}s')
  ```
- Error handling: always catch, log with `exc_info=True`, and return structured error responses.
- Keep route handlers thin — delegate to services.

### Vue/JavaScript (Dashboard)
- Composition API with `<script setup>`.
- Pinia stores for shared state; composables for reusable logic.
- Tailwind v4 for styling — use design tokens from Figma (see color palette below).
- All API calls go through `api.js` (`apiFetch()` for REST, `buildSSEUrl()` for SSE).

### Design System (from Figma)
```
Background:       #0a0d14 (main), #0c0e15 (cards/sidebar), #111318 (inputs/bubbles)
Borders:          #1a1d2e (primary), #1e2030 (inputs), #2a3556 (active)
Text:             #f0f2f5 (primary), #c4c8d4 (secondary), #8b8fa3 (muted), #555872 (subtle)
Accent:           #3b82f6 (blue), #00d4aa (teal/active nav), #6366f1 (indigo/user avatar)
Status:           #10b981 (success/green), #f59e0b (warning), #ef4444 (error)
Tool call:        #0d1a12 (bg), #1a3328 (border) — green tones
Surfaces:         #1e2233 (buttons), #1e3a5f (agent/user bubbles), #111830 (active states)
Font:             Inter (all weights: Regular, Medium, Semi Bold, Bold)
```

## Realtime Architecture

### SSE Endpoints
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/agents/activity-stream` | GET | Global agent status stream (`ActivityStreamManager`) |
| `/api/chat-stream` | POST | Chat with streaming progress (`ProgressManager` + `ToolRunnerHooks`) |
| `/api/agents/{name}/timeline` | GET | Per-agent timeline events SSE |
| `/api/scheduler/stream` | GET | Cron job execution events SSE |

### SSE Event Flow (Chat)
```
Client POST /api/chat-stream
  ← SSE: {type: "start", data: {message: "Processing..."}}
  ← SSE: {type: "tool_call", data: {tool: "brave-search", args: ...}}
  ← SSE: {type: "tool_result", data: {tool: "brave-search", result: ...}}
  ← SSE: {type: "thinking", data: {message: "Analyzing..."}}
  ← SSE: {type: "done", data: {response: "...", conversation_id: "...", total_tokens: {...}}}
```

### Multi-Agent Event Pipeline
```
MCP Subprocess (agent)
  → stderr JSON events
  → SpawnEventSocketServer (Unix socket bridge)
  → SpawnProgressBridge (event processor)
    → ActivityStreamManager (SSE → Dashboard Team Monitor)
    → AgentRegistryDB (status updates → DB)
    → Team completion check → Notification (when all members finish)
```

### Rules
- **No polling.** If you need data updates, use SSE or WebSocket.
- SSE connections must implement exponential backoff (1s → 30s max) on disconnect.
- Dashboard SSE reconnects on tab visibility change (`visibilitychange` event).
- Auth for GET SSE: `?api_key=<key>` query param. Auth for POST SSE: Bearer token in header.

## Multi-Agent Architecture

### Agent Lifecycle States
```
pending → running → idle ←→ running (auto-resume on inbox)
                  → completed
                  → error
                  → cancelled
                  → timeout
```

### Prompt Injection (Node Model)
Agents are nodes in a team tree. Inject = send context/direction to a node WITHOUT disrupting team flow.

Three code paths in `routes/inject.py`:
| Agent State | Method | Team Flow After? |
|---|---|---|
| `running` / `pending` / `paused` | MessageBus (inline queue) | ✅ Agent reads during work |
| `idle` / `completed` / `error` / `cancelled` / `timeout` | Resume from DB context (`services/inject_resume.py`) | ✅ `_check_and_resume_on_inbox` fires |
| Not in registry (static agent) | `agent.generate()` | N/A |

### Context Persistence
Agent conversation history is saved to SQLite (`agent_context_snapshots` table) at key lifecycle points. This enables:
- Resume from any state without disk files
- Context inspection via dashboard (`AgentDetail.vue`)
- Agent inject with full conversation history

Key module: `services/context_persistence.py` — uses `SPAWN_REGISTRY_DB` env var for DB path.

### Team Completion Notifications
`SpawnProgressBridge._check_team_completion()` monitors team status using `AgentRegistryDB` as single source of truth. Notifications trigger only when ALL team members reach terminal state, using the orchestrator's result as notification content.

## Environment Variables

### Backend (`backend/.env`)
| Variable | Description |
|----------|-------------|
| `JARVIS_API_KEY` | API authentication key |
| `TTS_PROVIDER` | TTS engine. Currently only `edge` is supported (free, no API key). |
| `LOG_CONSOLE_LEVEL` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `SPAWN_REGISTRY_DB` | Absolute path to SQLite DB (auto-set by server.py startup) |
| `SPAWN_PROJECT_DIR` | Absolute path to project dir for MCP subprocess env |
| `SPAWN_EVENT_SOCKET` | Unix socket path for subprocess event bridge |

### Dashboard (`dashboard/.env`)
| Variable | Description |
|----------|-------------|
| `VITE_JARVIS_API_KEY` | Auto-injected API key for dev convenience |

## fast-agent Integration Patterns

### Adding a New Agent
1. Create agent card YAML in `backend/.fast-agent/agent_cards/`
2. Define skills in `backend/.fast-agent/skills/`
3. Register MCP servers in `backend/fastagent.config.yaml` if needed
4. See `backend/ADDING_AGENT_GUIDE.md` for full walkthrough.

### Hooking into Agent Lifecycle
Use `ToolRunnerHooks` to observe agent activity without modifying core behavior:
```python
from services.sse_progress import create_progress_hooks, merge_hooks

hooks = create_progress_hooks(request_id)
agent.tool_runner_hooks = merge_hooks(existing_hooks, hooks)
```
Reference: [fast-agent Tool Runner docs](https://fast-agent.ai/agents/tool_runner/)

### Session Management
- Sessions are managed by `services/session_service.py` via `resume_and_send()`.
- Session files live in `backend/.fast-agent/sessions/`.
- The `session_history_window` config (default 200) controls auto-pruning.

## Security & Configuration

- **Never commit secrets:** `fastagent.secrets.yaml`, `.env` files, and `config/credentials/` are gitignored.
- Use `fastagent.secrets.yaml.example` as template for new deployments.
- `fastagent.secrets.docker.yaml` contains Docker-specific overrides (no local paths).
- Self-hosted deploy restores Docker secrets from `~/jarvis-data/fastagent.secrets.docker.yaml` on the server before `docker compose build`.
- If prod starts sending `Authorization: Bearer sk-...` to CLIProxyAPI, check that persistent file first; `openai.api_key` should normally stay `jarvis-proxy-key` for the proxy flow.
- CLIProxyAPI runs on port `8317` as a separate host service for key rotation; Jarvis Docker reaches it via `host.docker.internal:8317`.

## After Code Changes

- After modifying backend code: restart the backend process so changes take effect.
- After modifying dashboard code: Vite HMR handles most changes automatically.
- After modifying `fastagent.config.yaml`: full backend restart required.
- After modifying agent cards or skills: agents reload dynamically (no restart needed).

## Git & Deployment

- `fast-agent` is a git submodule at `backend/fast-agent`. Update with:
  ```bash
  git submodule update --remote backend/fast-agent
  ```
- Deploy: `make deploy` (updates submodule + Docker build + restart).
- Tags trigger CI/CD: use `vX.Y.Z` format.
- Commit messages: concise, action-oriented (e.g. `chat: add SSE streaming to chat view`).
- Docker uses `host.docker.internal` (via `extra_hosts: host-gateway`) to reach the separately deployed CLIProxyAPI service on the Ubuntu host.

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **jarvis_v3** (27104 symbols, 78065 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## When Debugging

1. `gitnexus_query({query: "<error or symptom>"})` — find execution flows related to the issue
2. `gitnexus_context({name: "<suspect function>"})` — see all callers, callees, and process participation
3. `READ gitnexus://repo/jarvis_v3/process/{processName}` — trace the full execution flow step by step
4. For regressions: `gitnexus_detect_changes({scope: "compare", base_ref: "main"})` — see what your branch changed

## When Refactoring

- **Renaming**: MUST use `gitnexus_rename({symbol_name: "old", new_name: "new", dry_run: true})` first. Review the preview — graph edits are safe, text_search edits need manual review. Then run with `dry_run: false`.
- **Extracting/Splitting**: MUST run `gitnexus_context({name: "target"})` to see all incoming/outgoing refs, then `gitnexus_impact({target: "target", direction: "upstream"})` to find all external callers before moving code.
- After any refactor: run `gitnexus_detect_changes({scope: "all"})` to verify only expected files changed.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

## Tools Quick Reference

| Tool | When to use | Command |
|------|-------------|---------|
| `query` | Find code by concept | `gitnexus_query({query: "auth validation"})` |
| `context` | 360-degree view of one symbol | `gitnexus_context({name: "validateUser"})` |
| `impact` | Blast radius before editing | `gitnexus_impact({target: "X", direction: "upstream"})` |
| `detect_changes` | Pre-commit scope check | `gitnexus_detect_changes({scope: "staged"})` |
| `rename` | Safe multi-file rename | `gitnexus_rename({symbol_name: "old", new_name: "new", dry_run: true})` |
| `cypher` | Custom graph queries | `gitnexus_cypher({query: "MATCH ..."})` |

## Impact Risk Levels

| Depth | Meaning | Action |
|-------|---------|--------|
| d=1 | WILL BREAK — direct callers/importers | MUST update these |
| d=2 | LIKELY AFFECTED — indirect deps | Should test |
| d=3 | MAY NEED TESTING — transitive | Test if critical path |

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/jarvis_v3/context` | Codebase overview, check index freshness |
| `gitnexus://repo/jarvis_v3/clusters` | All functional areas |
| `gitnexus://repo/jarvis_v3/processes` | All execution flows |
| `gitnexus://repo/jarvis_v3/process/{name}` | Step-by-step execution trace |

## Self-Check Before Finishing

Before completing any code modification task, verify:
1. `gitnexus_impact` was run for all modified symbols
2. No HIGH/CRITICAL risk warnings were ignored
3. `gitnexus_detect_changes()` confirms changes match expected scope
4. All d=1 (WILL BREAK) dependents were updated

## Keeping the Index Fresh

After committing code changes, the GitNexus index becomes stale. Re-run analyze to update it:

```bash
npx gitnexus analyze
```

If the index previously included embeddings, preserve them by adding `--embeddings`:

```bash
npx gitnexus analyze --embeddings
```

To check whether embeddings exist, inspect `.gitnexus/meta.json` — the `stats.embeddings` field shows the count (0 means no embeddings). **Running analyze without `--embeddings` will delete any previously generated embeddings.**

> Claude Code users: A PostToolUse hook handles this automatically after `git commit` and `git merge`.

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
