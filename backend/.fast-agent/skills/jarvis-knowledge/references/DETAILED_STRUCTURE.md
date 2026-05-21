# Jarvis Codebase — Detailed Structure

## Full Project Structure

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
│   │   └── cron_scheduler.py   ← AI-managed cron job system
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
│   │   ├── story_server.py     ← Story crawl/read tools
│   │   ├── cron_server.py      ← Cron scheduler tools
│   │   ├── time_server.py      ← Current time tool
│   │   └── media_server.py     ← Audio player control
│   │
│   ├── fast-agent/             ← Local fork of fast-agent framework
│   │   └── src/fast_agent/spawn/  ← Multi-agent spawn system
│   │       ├── isolated_spawner.py   ← Core spawn engine
│   │       ├── team_spawner.py       ← Team template spawner
│   │       ├── message_bus.py        ← Inter-agent email/inbox
│   │       └── servers/
│   │           ├── agent_spawner_server.py ← Main MCP server (21 tools)
│   │           ├── email_server.py        ← Inter-agent messaging
│   │           └── meeting_room_server.py ← Multi-agent meeting protocol
│   │
│   ├── team_templates/         ← Team definitions
│   │   └── agile_team.yaml     ← 7-role agile team
│   │
│   └── .fast-agent/            ← Agent runtime
│       ├── skills/             ← Skill files (YAML frontmatter + MD)
│       ├── agent_cards/        ← Dynamic agent card definitions
│       └── sessions/           ← Session history files
│
├── dashboard/                  ← Vue 3 + Vite Ops Dashboard
│   └── src/
│       ├── views/              ← Page components
│       ├── components/         ← Reusable UI components
│       ├── composables/        ← Vue composables (SSE streams, etc.)
│       └── stores/             ← Pinia stores
│
└── .github/workflows/          ← CI/CD (deploy.yml)
```

## Spawn System Architecture

```
MCP subprocess (agent) → spawn_events.jsonl → SpawnProgressBridge
  → ProgressEventManager (chat SSE)
  → ActivityStreamManager (global SSE)
  → SQLite agent_activities table
  → SQLite spawn_records table
```

## Inter-Agent Communication

- **Email**: `message_bus.py` + `email_server.py` — async messaging
- **Meetings**: `meeting_room_server.py` — real-time multi-agent meetings
- **Status**: `check_teammate_status` — check running/idle/completed

## How to Add New Features

1. **New agent**: `.fast-agent/agent_cards/<Name>.md`
2. **New skill**: `.fast-agent/skills/<name>/SKILL.md` with YAML frontmatter
3. **New MCP tool**: `tools/<name>_server.py` + register in `fastagent.config.yaml`
4. **New API endpoint**: `routes/` + register in `routes/__init__.py`
5. **New team role**: `team_templates/agile_team.yaml`
