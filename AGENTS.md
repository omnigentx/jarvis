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
│   │   ├── chat.py             # /api/chat (legacy single-shot, used by Xiaozhi), /api/chat-stream (SSE)
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
│   │   ├── tts.py              # Edge TTS provider (legacy, still the chat default)
│   │   ├── tts_realtime.py     # RealtimeTTS adapter — registry-driven engines + factories
│   │   ├── tts_pregen_job.py   # Story TTS pre-gen (always Edge)
│   │   ├── pregen_stream.py    # TTS pre-gen SSE stream helper
│   │   ├── stt_realtime.py     # RealtimeSTT adapter (WS-fed, swappable hook)
│   │   ├── voice_engine_registry.py  # Single source of truth — TTS/STT engine specs
│   │   ├── voice_config.py     # JSON-shaped DB persistence for voice.tts.* / voice.stt
│   │   ├── library_manager.py  # Book library CRUD + progress tracking
│   │   ├── pricing.py          # LLM token pricing calculations
│   │   ├── history.py          # TTS cache management
│   │   └── repo_config.py      # Resolve project repo URL (config_service DB only)
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
│   ├── fast-agent/             # Git submodule → fast-agent core
│   ├── realtimestt_src/        # Git submodule → omnigentx/RealtimeSTT (hands-free STT)
│   └── realtimetts_src/        # Git submodule → omnigentx/RealtimeTTS (streaming TTS)
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
│       │   ├── StoryReaderView.vue # Audiobook player
│       │   └── settings/
│       │       └── SettingsVoice.vue   # Voice tab — engines, secrets, STT, wake-word
│       ├── components/         # Reusable UI components (incl. chat/VoiceBar.vue)
│       ├── composables/        # Vue composables
│       │   ├── useActivityStream.js   # Agent activity SSE
│       │   ├── useChatStream.js       # Chat SSE stream
│       │   ├── useRealtimeStream.js   # Generic SSE helper
│       │   ├── useMeetingStream.js    # Meeting events SSE
│       │   ├── useMeetingList.js      # Meeting data fetcher
│       │   ├── useAudioPlayer.js      # Audio player state
│       │   ├── usePregenStream.js     # TTS pre-gen SSE
│       │   ├── useVoiceSession.js     # Hands-free /ws/voice client + AudioWorklet
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
| `/api/agents/activity-stream` | GET | Global agent status + `message_turn` stream (`ActivityStreamManager`) |
| `/api/agents/{name}/messages` | GET | Initial / paginated `agent.message_history` for the v2 monitor (`?since=N`, `?limit=L`) |
| `/api/agents/{name}/turns/{idx}/full` | GET | Untruncated PromptMessageExtended for one turn (used by "Show full") |
| `/api/chat-stream` | POST | Chat with streaming progress (`ProgressManager` + `ToolRunnerHooks`) |
| `/api/agents/{name}/timeline` | GET | Per-agent timeline events SSE (legacy AgentDetail "Activity" tab) |
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

### Team Monitor v2 — message_history-driven (canonical)
The monitor UI reads turns directly from `agent.message_history` rather
than from synthesized `tool_call`/`tool_result`/`response` events. One
event channel, one shape, no duplicates.

```
agent.message_history  ←  source of truth (fast-agent llm_decorator._persist_history)
        │
        ├─ in-process: ToolRunnerHooks.after_llm_call / after_tool_call
        │     → services.agent_message_stream.emit_message_history_delta
        │     → activity_stream broadcast: {event_type: "message_turn", data: {turn_idx, message}}
        │
        └─ subprocess: isolated_runner._install_tool_hooks
              → emit_event("message_turn", run_id, role, turn_idx, message)
              → SpawnProgressBridge._forward_message_turn (applies trim_message_for_stream)
              → activity_stream broadcast (same shape)

Frontend: useAgentTurns composable dedups by (agent, turn_idx) and bridges
recentEvents → per-agent buffer. AgentTerminal.vue renders the buffer.
```

`trim_message_for_stream` caps any text block above 16 KiB, marking
`_truncated=true` + `_full_size`. The UI fetches uncapped content from
`/api/agents/{name}/turns/{idx}/full` when the user clicks "Show full".

`tool_call`, `tool_result`, and `response` event_types are **no longer
broadcast** on the activity-stream — they're contained within
`message_turn`. Lifecycle events (`started`/`idle`/`error`/`thinking`/
`agent_paused`/`agent_resumed`/`token_usage`/`agent_added`/
`agent_removed`) stay as a separate channel.

The terminal-style grid is the ONLY monitor UI — the legacy v1 card
view was removed (see commit "feat(dashboard): TeamMonitor terminal UI
+ meeting transcript + voice"). No feature flag, no fallback.

### Rules
- **No polling.** If you need data updates, use SSE or WebSocket.
- SSE connections must implement exponential backoff (1s → 30s max) on disconnect.
- Dashboard SSE reconnects on tab visibility change (`visibilitychange` event).
- New monitor work should plug into `useAgentTurns` / `<AgentTerminal>`,
  not synthesize parallel event types.

## Voice Architecture

### Provider split (single-source-of-truth invariant)
| Provider | Where it's used | Engine |
|----------|-----------------|--------|
| `tts_chat_provider` (registry-driven) | `/chat`, `/chat-stream`, `/ws/voice`, cron notifications | Edge default; ElevenLabs / OpenAI / Azure / System opt-in via Settings → Voice |
| `tts_stories_provider` (locked Edge) | Story chapters, library books, `tts_pregen_job` | Always Edge — protects long-form quota at code level |

Dispatch lives in `routes/tts.py`: `_state.tts_chat_provider if is_notification else _state.tts_stories_provider`. The legacy `tts_provider` attribute aliases the chat provider for back-compat callers; never alias to stories.

### Voice transports
| Path | Direction | Purpose |
|------|-----------|---------|
| `GET /api/tts/{request_id}` | server → browser MP3 stream | Listen-back of chat replies, story chapters, notifications. Existing browser `<audio>` plays it. |
| `WS /ws/voice` | bidirectional binary + JSON | Hands-free conversational loop. Browser sends 16 kHz mono int16 PCM; server emits partial transcripts, VAD events, wake word, and TTS audio (PCM or MP3 depending on engine). Barge-in cancels in-flight TTS in-process — single-socket avoids round-trip latency. |

### Registry & hot-reload
- `services/voice_engine_registry.py` is the **single source of truth** for which engines exist, their params, secrets, and requirements. Settings UI form is generated entirely from this dict — adding a new engine is purely additive.
- Storage (DB-backed JSON, hot-reloadable):
  - `voice.tts.chat` = `{engine, params}`
  - `voice.tts.stories` = `{voice, rate}` (locked schema — no `engine` field accepted)
  - `voice.stt` = `{backend, params, wake_word: {backend, params}}`
  - `voice.secrets.{engine}.{slot}` (encrypted via secrets_crypto)
- `services/runtime_config.py::apply_voice_chat_config / apply_voice_stories_config / apply_voice_stt_config` are listener-driven: any UI write to `voice.*` rebuilds **only** the affected provider; chat changes never touch stories.

### Submodules (omnigentx forks)
- `backend/realtimestt_src` — fork of `KoljaB/RealtimeSTT`, faster-whisper based STT with VAD, optional wake word (Porcupine / OpenWakeWord). WS-fed via `use_microphone=False` + `feed_audio()`. Submodule path differs from the Python package name (`RealtimeSTT`) so the source tree at backend cwd doesn't shadow the editable install via PEP 420 namespace package resolution.
- `backend/realtimetts_src` — fork of `KoljaB/RealtimeTTS`, multi-engine streaming TTS. Edge bypasses the library for the legacy MP3 path; other engines run through `RealtimeTTSProvider` with PCM↔MP3 transcode for HTTP and raw PCM for WS. Same path-vs-package-name rule as STT.
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

## MCP Atlassian — known incident pitfalls (read before debugging)

These are pitfalls recovered from production incidents. Each one cost
~10-30 minutes of agent retry-loops before being diagnosed. They live
here so the next agent (LLM or human) doesn't re-discover them by
trial and error.

- **`jira_create_project.project_template_key` is REQUIRED on Cloud.**
  Older descriptions said it was optional; modern Jira Cloud rejects
  POST `/rest/api/3/project` with bare `HTTP 400: Invalid request payload`
  (no structured fields) when omitted. As of 2026-05-16 the MCP tool
  fills a per-type default (`software`→Scrum agility, `business`→process
  control, `service_desk`→IT). Override only when needed. Evidence:
  `jarvis.log:3322-3330`, 2026-05-16 00:34 ICT — agent burned 4
  retries before fix landed.

- **Confluence returns 403 (not 404) for pages in non-existent spaces.**
  `confluence_create_page(space_key="X")` against a missing space `X`
  comes back as `"The calling user does not have permission to view
  the content"`. Always call `confluence_create_space("X", ...)`
  BEFORE `create_page` when targeting a new key. The MCP tool now
  rewrites the 403 with an actionable hint, but the underlying
  Atlassian wording can change — if the hint disappears, this is
  still the right diagnosis. Evidence: `jarvis.log:3296`, 2026-05-16
  00:32 ICT.

- **`lead_account_id` accepts `"me"` / email / display-name.** Agents
  don't need to know their 24-char Cloud accountId. Pass `"me"` and
  the server resolves via `/rest/api/3/myself`. Email/display-name
  flow through the user-lookup chain. As of 2026-05-15.

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **jarvis** (35826 symbols, 100473 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

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
3. `READ gitnexus://repo/jarvis/process/{processName}` — trace the full execution flow step by step
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
| `gitnexus://repo/jarvis/context` | Codebase overview, check index freshness |
| `gitnexus://repo/jarvis/clusters` | All functional areas |
| `gitnexus://repo/jarvis/processes` | All execution flows |
| `gitnexus://repo/jarvis/process/{name}` | Step-by-step execution trace |

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
