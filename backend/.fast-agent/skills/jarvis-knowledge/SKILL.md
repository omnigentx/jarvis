---
name: jarvis-knowledge
description: >
  Knowledge base for the Jarvis AI Assistant codebase. Use when understanding
  project structure, finding files to modify, or adding new features.
---

# Jarvis Codebase Knowledge

## GitHub Repositories

| Repo | URL | Access |
|------|-----|--------|
| **Origin** (user-managed) | https://github.com/omnigentx/jarvis-v3 | User only |
| **Agent** (agents use this) | https://github.com/phucnv31/jarvis-v3 | Full access — commit, push, create MR |

> Agents use the `phucnv31/jarvis-v3` repo for all Git operations.

## High-Level Architecture

```
jarvis_v3/
├── backend/              ← FastAPI + fast-agent (Python 3.11+)
│   ├── server.py         ← Entry point
│   ├── agent.py          ← Agent definitions
│   ├── routes/           ← API endpoints
│   ├── services/         ← Business logic
│   ├── core/             ← Infrastructure (DB, auth, logging)
│   ├── tools/            ← Custom MCP tool servers
│   ├── fast-agent/       ← Framework submodule (spawn system)
│   ├── team_templates/   ← Team definitions (agile_team.yaml)
│   └── .fast-agent/      ← Skills, agent cards, sessions
├── dashboard/            ← Vue 3 + Vite + Tailwind v4
└── .github/workflows/    ← CI/CD
```

## Key Patterns

| Pattern | Location | Description |
|---------|----------|-------------|
| Static agents | `agent.py` | Jarvis, PersonalAgent, IoTAgent, etc. |
| Dynamic agents | `.fast-agent/agent_cards/*.md` | Hot-reload at startup |
| Team agents | `team_templates/agile_team.yaml` | Spawned at runtime |
| Inter-agent email | `message_bus.py` + `email_server.py` | Async messaging |
| Meetings | `meeting_room_server.py` | Real-time turn-taking |
| SSE pipeline | spawn events → bridge → SSE/SQLite | Real-time UI updates |

## Tech Stack

- **Backend**: Python, FastAPI, fast-agent, SQLAlchemy, SQLite
- **Dashboard**: Vue 3, Vite, Tailwind v4, Pinia
- **Deploy**: Docker Compose, GitHub Actions CI/CD
- **LLM**: OpenAI-compatible via CLIProxyAPI (key rotation + load balancing)

## References

| Topic | File |
|-------|------|
| Full project tree + spawn architecture | [DETAILED_STRUCTURE.md](references/DETAILED_STRUCTURE.md) |
