---
name: jarvis-knowledge
description: >
  Knowledge base for the Jarvis AI Assistant codebase. Use when understanding project structure,
  finding files to modify, or adding new features. Shared between SA, Dev, and DevOps roles.
---

# JARVIS CODEBASE KNOWLEDGE

## GitHub Repositories

| Repo | URL | Access |
|------|-----|--------|
| **Upstream** (source of truth) | https://github.com/omnigentx/jarvis | Public read-only |
| **Your fork** (agents push here) | https://github.com/<your-username>/jarvis | Full access — commit, push, create PR/issues, deploy via GitHub Actions |

> 🧪 **Experimental**: When agent self-improvement is enabled, agents (Jarvis, sub-agents) push to your personal fork — never to upstream. Configure the fork URL in `fastagent.secrets.yaml`.


## Project Structure

```
jarvis/
├── backend/
│   ├── agent.py                ← Agent definitions (Jarvis, PersonalAgent, IoT, Music, Audio)
│   ├── server.py               ← FastAPI app entry point + lifespan
│   ├── fastagent.config.yaml   ← MCP servers, models, runtime config
│   ├── fastagent.secrets.yaml  ← Sensitive keys (gitignored)
│   │
│   ├── routes/                 ← API endpoints
│   │   ├── __init__.py         ← Router registry (all_routers)
│   │   ├── chat.py             ← /chat — main chat endpoint + SSE streaming
│   │   ├── agents.py           ← /agents — spawn management, team status, activities
│   │   ├── agent_timeline.py   ← /agents/timeline — agent activity timeline
│   │   ├── tts.py              ← /tts — text-to-speech generation
│   │   ├── stories.py          ← /stories — story management + crawling
│   │   ├── sessions.py         ← /sessions — conversation sessions
│   │   ├── library.py          ← /library — audio library
│   │   └── auth.py             ← /auth — API key authentication
│   │
│   ├── services/               ← Business logic
│   │   ├── shared_state.py     ← Global singletons (agent_app, spawn_bridge, etc.)
│   │   ├── spawn_progress_bridge.py ← Cross-process JSONL → SSE forwarding
│   │   ├── sse_progress.py     ← SSE progress event manager
│   │   ├── activity_stream.py  ← Global activity SSE broadcast
│   │   ├── dynamic_agents.py   ← Hot-reload agent cards at runtime
│   │   ├── session_service.py  ← Session CRUD + history management
│   │   ├── history.py          ← Conversation history storage
│   │   ├── tts.py              ← TTS engine (Google Cloud TTS)
│   │   ├── tts_pregen_job.py   ← Background TTS pre-generation
│   │   ├── background_jobs.py  ← Job scheduler framework
│   │   ├── crawl_poller.py     ← Story crawl job processor
│   │   └── library_manager.py  ← Audio library management
│   │
│   ├── core/                   ← Infrastructure
│   │   ├── database.py         ← SQLite + SQLAlchemy models
│   │   ├── agent_registry_db.py ← Spawn records SQLite storage
│   │   ├── auth.py             ← API key verification
│   │   └── logging_config.py   ← Structured logging setup
│   │
│   ├── tools/                  ← Custom MCP tool servers
│   │   ├── calendar_server.py  ← Google Calendar tools
│   │   ├── gmail_server.py     ← Gmail tools
│   │   ├── iot_server.py       ← Roborock robot vacuum control
│   │   ├── story_server.py     ← Story crawl/read tools (largest tool)
│   │   ├── library_server.py   ← Local audio library tools
│   │   ├── media_server.py     ← Audio player control
│   │   ├── time_server.py      ← Current time tool
│   │   └── crawl_resume.py     ← Resume interrupted crawl jobs
│   │
│   ├── fast-agent/             ← Local fork of fast-agent framework
│   │   └── src/fast_agent/spawn/  ← Multi-agent spawn system
│   │       ├── spawn_hooks.py        ← SpawnLifecycleHooks protocol + NoOp base
│   │       ├── isolated_spawner.py   ← Core spawn engine (foreground + background)
│   │       ├── isolated_runner.py    ← FastAgent subprocess runner
│   │       ├── team_spawner.py       ← Team template spawner (agile team)
│   │       ├── spawn_registry.py     ← In-memory spawn record tracking
│   │       ├── spawn_events.py       ← Event emission for cross-process monitoring
│   │       ├── spawn_display.py      ← Rich display for spawn progress
│   │       ├── message_bus.py        ← Inter-agent email/inbox system
│   │       ├── card_generator.py     ← Dynamic agent card generation
│   │       ├── config_reader.py      ← MCP config parsing
│   │       ├── workspace_manager.py  ← Workspace isolation
│   │       ├── subagents_tool.py     ← Subagent delegation tools
│   │       └── servers/
│   │           ├── agent_spawner_server.py ← Main MCP server (21 tools) + _FileBasedSpawnLifecycleHooks
│   │           ├── email_server.py        ← Inter-agent messaging server
│   │           └── meeting_room_server.py ← Multi-agent meeting protocol
│   │
│   ├── team_templates/         ← Team definitions
│   │   └── agile_team.yaml     ← 7-role agile team (PM, BA, SA, Dev, Designer, QE, DSO)
│   │
│   ├── .fast-agent/            ← Agent runtime
│   │   ├── skills/             ← Skill files (YAML frontmatter + MD)
│   │   └── agent_cards/        ← Dynamic agent card definitions
│   │
│   ├── tests/                  ← Test suite (pytest)
│   ├── Dockerfile              ← Backend container
│   └── docker-compose.yaml     ← Backend services
│
├── frontend/                   ← Vue 3 + Vite web app
│   └── src/
│       ├── views/              ← Page components (chat, agents, timeline, settings)
│       ├── components/         ← Reusable UI components
│       ├── composables/        ← Vue composables (SSE streams, etc.)
│       └── stores/             ← Pinia stores
│
└── .github/workflows/          ← CI/CD (deploy.yml)
```

## Key Architectural Patterns

### Agent System
- **Static agents** defined in `agent.py`: Jarvis (root), PersonalAgent, IoTAgent, MusicAgent, AudioReaderAgent
- **Dynamic agents** loaded from `.fast-agent/agent_cards/*.md` at startup via `dynamic_agents.py`
- **Team agents** spawned at runtime from `team_templates/agile_team.yaml` via `team_spawner.py`
- Jarvis delegates to sub-agents via `agent__<AgentName>` tool calls

### Spawn System (Multi-Agent)
- `isolated_spawner.py` — spawns agents as isolated subprocesses with own MCP servers
- **UDS socket** for event IPC (replaced legacy JSONL file approach)
- `SpawnLifecycleHooks` protocol in `spawn_hooks.py` — 11 lifecycle phases
- **Backend restart kills ALL child agents** (same process group, no session detach)

See [spawn-event-pipeline.md](references/spawn-event-pipeline.md) for full architecture, env vars, event types, pause/resume flow, and process lifecycle.

### Inter-Agent Communication
- **Email**: `message_bus.py` + `email_server.py` — async messaging
- **Meeting**: `meeting_room_server.py` — multi-agent turn-taking
- **Messages dir**: `.runtime/state/messages/{session_id}/`

### Quick Reference: Pause/Resume
- Pause = `SIGUSR1`, Resume = `SIGUSR2`
- PID lookup must use `find_by_name()` (NOT `list_running()` — breaks resume)
- Valid inject statuses: `running`, `pending`, `idle`, `paused`

## How to Add New Features

1. **New static agent**: Add to `agent.py` with `@fast.agent()` decorator
2. **New dynamic agent**: Create `.fast-agent/agent_cards/<Name>.md` with agent card format
3. **New skill**: Create `.fast-agent/skills/<name>/SKILL.md` with YAML frontmatter
4. **New MCP tool server**: Create `tools/<name>_server.py` + register in `fastagent.config.yaml`
5. **New API endpoint**: Add route in `routes/` + register in `routes/__init__.py`
6. **New team role**: Add role definition in `team_templates/agile_team.yaml`
7. **New spawn tool**: Add to `agent_spawner_server.py` + expose in `agent.py` `_JARVIS_TOOLS`

## Tech Stack
- **Backend**: Python, FastAPI, fast-agent framework, SQLAlchemy, SQLite
- **Frontend**: Vue 3, Vite, Tailwind v4, Pinia
- **Deploy**: Docker Compose, GitHub Actions CI/CD
- **LLM**: Anthropic Claude (primary), OpenAI (fallback) via fast-agent
- **MCP**: Model Context Protocol — all tools are MCP servers
- **TTS**: Google Cloud Text-to-Speech

## Fast-Agent Skills Reference
Official fast-agent skills in `references/fast-agent-skills/`:

| Skill | Use Case |
|-------|----------|
| `agent-card-hooks` | Hook functions (tool_hooks, lifecycle_hooks) |
| `compaction-strategies` | History compaction (rolling window, truncation) |
| `fast-agent-automation` | CLI/Docker/HF Jobs automation |
| `hf-space-deployer` | Deploy MCP servers to HF Spaces |
| `hf-static-space-deployer` | Deploy static sites to HF Spaces |
| `lsp-setup` | LSP tooling (ty/typescript-language-server) |
| `session-investigator` | Debug sessions, tool loops, timing |

Source: https://github.com/fast-agent-ai/skills (v1.1.2)

## Debugging Pitfalls

See [debugging-pitfalls.md](references/debugging-pitfalls.md) for full details on each issue.

| # | Pitfall | Key Rule |
|---|---------|----------|
| 1 | UDS buffer overflow (silent event loss) | `spawn_event_socket.py` buffer must be ≥4MB for `runtime_config` events |
| 2 | PID lookup breaks resume | Use `find_by_name()`, NEVER `list_running()` for PID lookup |
| 3 | Inject workspace missing | Derive messages dir from `SPAWN_PROJECT_DIR + session_id` |
| 4 | DB status ≠ reality | Always `kill -0 <pid>` before trusting DB status |
| 5 | Inject rejects idle/paused | Valid statuses: `running`, `pending`, `idle`, `paused` |
| 6 | Dashboard broadcast guards | Only broadcast `started` for `idle` → `running` transition |
