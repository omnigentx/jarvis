---
name: jarvis-infra
description: >
  Jarvis infrastructure knowledge. Use when DSO needs to deploy, configure Docker,
  manage CI/CD pipeline, or troubleshoot environment issues.
---

# JARVIS INFRASTRUCTURE

## GitHub Repositories

| Repo | URL | Purpose |
|------|-----|---------|
| **Origin** | https://github.com/omnigentx/jarvis-v3 | User-managed source of truth |
| **Agent** | https://github.com/phucnv31/jarvis-v3 | Agent-accessible — CI/CD deploys from here |


## Docker Architecture

```
┌──────────────────────────────────────────────┐
│  docker-compose.yaml (root)                  │
│  ├── jarvis-backend (Python/FastAPI)         │
│  │   Port: 8000                              │
│  │   Volumes: ./backend → /app               │
│  │   Runtime: spawn dirs, JSONL, SQLite      │
│  └── jarvis-web (Flutter → Nginx)            │
│      Port: 80                                │
│      Build: multi-stage (Flutter + Nginx)    │
└──────────────────────────────────────────────┘
```

## Environment Variables

| Variable | Service | Purpose |
|----------|---------|---------|
| `ANTHROPIC_API_KEY` | backend | Claude API key (primary LLM) |
| `OPENAI_API_KEY` | backend | OpenAI fallback |
| `SERPAPI_API_KEY` | backend | Search API for finance/web |
| `GOOGLE_CREDENTIALS` | backend | Calendar + Gmail service account |
| `API_KEY` | backend | API key for auth endpoints |
| `CORS_ORIGINS` | backend | Allowed CORS origins |
| `DISABLE_AGENT_SPAWNER` | backend | Set `1` to disable spawn system in Docker |

## Runtime Directories

```
backend/
├── .runtime/state/               ← Spawn runtime state
│   ├── spawn_events.jsonl        ← Cross-process event log
│   ├── spawn_registry.json       ← Legacy spawn registry (migrated to SQLite)
│   └── workspaces/               ← Isolated agent workspaces
├── .runtime/inboxes/             ← Inter-agent message inboxes
├── data/jarvis.db                ← SQLite database
└── core/logs/                    ← Structured log files
```

## CI/CD Pipeline (.github/workflows/deploy.yml)

```
Push to main → GitHub Actions
  1. Build Docker image
  2. SSH to server
  3. Pull latest code
  4. docker compose build + up -d
  5. Health check
```

### Inspecting + driving the pipeline via `gh` CLI

The `gh` CLI is pre-authenticated in your shell via `execute(command=...)`. Use it for release ops and deployment monitoring — the GitHub MCP doesn't cover workflow triggers or release management.

```bash
# Monitor deploy after Dev merges to main (SAFE — read-only)
gh run list --workflow=deploy.yml -L 5
gh run watch <run-id>                      # block until done
gh run view <run-id> --log-failed | tail -100

# List + view releases (SAFE)
gh release list -L 5
gh release view v1.2.3
```

🟡 **ESCALATE before doing** (send `[APPROVAL-REQUEST]` email to PM with the exact command + reason; do NOT run until PM relays `[APPROVED]`):
- `gh workflow run deploy.yml` or any prod-deployment workflow trigger
- `gh release create vX.Y.Z` (any new release)
- `gh release edit/delete`
- DNS / CDN / cloud-provider config changes
- `kubectl apply -f` against prod cluster
- `docker compose up/down` on production hosts (not dev box)
- Pipe-to-shell: `curl ... | sh`, `wget ... | bash`
- `git push --force` (any branch), `git reset --hard` past HEAD~1

🔴 **NEVER** (refuse + escalate as a security incident):
- Read auth files: `~/.gh-config/`, `.env*`, `fastagent.secrets.yaml`, `git-credentials`, `~/.ssh/`
- Inspect env to leak tokens: `env`, `printenv`, `echo $GH_TOKEN`
- `gh auth login/logout/refresh`
- Filesystem destruction: `rm -rf /`, `rm -rf $HOME`, `rm -rf .git`, `mkfs`, `dd of=/dev/...`
- Fork bombs / high-rate network loops

🟢 **SAFE** (run freely): all read-only `gh run/release/pr/workflow view/list`, `docker compose logs/ps`, local file inspection.

### Escalation flow

```python
send_email(
    to="<PM name>",
    subject="[APPROVAL-REQUEST] <one-line summary>",
    body="""
Need approval to run: `<exact command>`
Why: <task this unblocks>
Risk: <what could go wrong>
Rollback: <how to undo if it goes wrong>
""",
)
# Wait for PM. PM uses approval-server MCP to ask user.
```

## Build & Run Commands

```bash
# Local development
cd backend && uv run uvicorn server:app --reload --port 8000

# Docker — build all
docker compose build

# Docker — restart backend only
docker compose restart jarvis-backend

# Docker — view logs
docker compose logs -f jarvis-backend --tail=50

# Run tests
cd backend && uv run pytest tests/ -v
```

## Troubleshooting

<troubleshooting_guide>
<issue>Backend fails to start</issue>
<fix>Check logs for import errors: `docker compose logs jarvis-backend | head -50`</fix>

<issue>MCP server timeout</issue>
<fix>Each MCP server has 10s startup timeout. Check `fastagent.config.yaml` for Docker command issues. Use `DISABLE_AGENT_SPAWNER=1` in Docker if spawn crashes.</fix>

<issue>Skills not loading</issue>
<fix>Verify YAML frontmatter syntax in `.fast-agent/skills/<name>/SKILL.md`.</fix>

<issue>Spawn agents showing as "running" after restart</issue>
<fix>Server auto-reconciles: checks PIDs on startup, marks dead processes as completed. Also runs periodic health check every 30s.</fix>

<issue>SQLite locked</issue>
<fix>Only one writer at a time. Check for long-running transactions in `core/database.py`.</fix>
</troubleshooting_guide>

## Local Development Setup

### Config Override Pattern

Jarvis uses a **two-layer config** so you never need to edit committed files when switching between local dev and Docker:

| File | Role | Git |
|------|------|-----|
| `fastagent.config.yaml` | Docker-ready defaults (committed) | ✅ Tracked |
| `fastagent.secrets.yaml` | Local overrides + secrets (per-machine) | ❌ gitignored |

`fastagent.secrets.yaml` deep-merges into `fastagent.config.yaml` at startup. Any key in secrets overrides the same key in config.

#### What goes where

```yaml
# fastagent.config.yaml (committed — Docker paths)
openai:
  base_url: "http://host.docker.internal:8317/v1"
openresponses:
  base_url: "http://host.docker.internal:8317/v1"
mcp:
  servers:
    figma-ui-mcp:
      args: ["/app/figma-ui-mcp/dist/server.mjs"]

# fastagent.secrets.yaml (gitignored — local dev overrides)
openai:
  api_key: "sk-proj-..."
  base_url: "http://127.0.0.1:8317/v1"
openresponses:
  base_url: "http://127.0.0.1:8317/v1"
mcp:
  servers:
    figma-ui-mcp:
      args: ["/Users/<you>/path/to/figma-ui-mcp/dist/server.mjs"]
```

#### How it resolves

| Setting | Config (Docker) | Secrets (Local) | Result |
|---------|----------------|-----------------|--------|
| `openai.base_url` | `host.docker.internal:8317` | `127.0.0.1:8317` | Local wins |
| `figma-ui-mcp.args` | `/app/figma-ui-mcp/...` | `/Users/.../figma-ui-mcp/...` | Local wins |
| `openai.api_key` | (not set) | `sk-proj-...` | From secrets |

> **Rule**: Docker paths go in `config.yaml`. Local paths and API keys go in `secrets.yaml`. Zero file edits when switching environments.

### CLIProxyAPI — Multi-Account LLM Proxy

CLIProxyAPI provides key rotation and load balancing across multiple OpenAI/Codex accounts. Runs on host via Homebrew (not Dockerized).

#### Installation

```bash
brew install eceasy/tap/cli-proxy-api
```

#### Codex Account Login

```bash
# Login multiple Codex accounts (opens browser for OAuth)
cli-proxy-api --codex-login
# Repeat for each account — tokens saved to ~/.cli-proxy-api/codex-*.json
```

#### Proxy Config (`~/.cli-proxy-api/config.yaml`)

```yaml
port: 8317
auth-dir: ""  # defaults to ~/.cli-proxy-api/
api-keys:
  - "jarvis-proxy-key"  # matches fastagent.config.yaml api_key
routing:
  strategy: "round-robin"
quota-exceeded:
  switch-project: true
openai-compatibility:
  - name: "openai"
    base-url: "https://api.openai.com/v1"
    api-key-entries:
      - api-key: "sk-proj-YOUR_KEY_1"
      - api-key: "sk-proj-YOUR_KEY_2"
    models:
      - name: "gpt-4o-mini"
        alias: "gpt-4o-mini"
      - name: "gpt-4o"
        alias: "gpt-4o"
```

#### Start & Verify

```bash
cli-proxy-api        # starts on port 8317
curl http://127.0.0.1:8317/v1/models  # verify models loaded
```

#### Architecture

```
Local dev:   fast-agent → 127.0.0.1:8317 → CLIProxyAPI → OpenAI/Codex (round-robin)
Docker:      container  → host.docker.internal:8317 → CLIProxyAPI (on host) → OpenAI/Codex
```

> **Note**: On the production server, CLIProxyAPI must also be installed and running on the host. Login Codex accounts on the server after installation.

### API Rotation Architecture

#### Problem
- Codex free-tier accounts have aggressive rate limits (429 errors)
- Single API key → frequent downtime during heavy usage
- `codexresponses` provider has hardcoded OAuth JWT parsing, can't be proxied

#### Solution: Proxy-First Design

```
┌─────────────────────────────────────────────────────────────┐
│  fast-agent (backend)                                       │
│  ┌──────────────────────┐                                   │
│  │ openresponses provider│  ← Standard Responses API        │
│  │ (no custom OAuth)     │     no JWT parsing               │
│  └──────────┬───────────┘                                   │
│             │ api_key: "jarvis-proxy-key"                    │
│             ▼                                               │
│  CLIProxyAPI (port 8317)                                    │
│  ┌──────────────────────────────────────────┐               │
│  │ Round-robin load balancer                 │              │
│  │ ┌────────┐ ┌────────┐ ┌────────┐         │              │
│  │ │Codex #1│ │Codex #2│ │Codex #3│ ...×5   │              │
│  │ └────────┘ └────────┘ └────────┘         │              │
│  │ ┌──────────┐ ┌──────────┐                │              │
│  │ │OpenAI #1 │ │OpenAI #2 │                │              │
│  │ └──────────┘ └──────────┘                │              │
│  │ Features:                                 │              │
│  │  - Auto 429 retry + account switch        │              │
│  │  - OAuth token refresh (Codex)            │              │
│  │  - quota-exceeded auto-switch             │              │
│  └──────────────────────────────────────────┘               │
│             │                                               │
│             ▼                                               │
│  OpenAI API / Codex API                                     │
└─────────────────────────────────────────────────────────────┘
```

#### Why `openresponses` instead of `codexresponses`?

| | `codexresponses` | `openresponses` |
|---|---|---|
| Auth | Custom OAuth JWT parsing (hardcoded) | Standard `api_key` header |
| Base URL | Hardcoded to Codex endpoint | Configurable via `base_url` |
| Proxy-able | ❌ JWT logic breaks proxying | ✅ Works with any OpenAI-compatible proxy |
| API | Responses API | Responses API (same) |

Both use the same underlying Responses API — `openresponses` simply delegates auth to the proxy.

#### Model Configuration

```yaml
# fastagent.config.yaml
default_model: openresponses.gpt-5.4  # Codex model via proxy

# Available models (exposed by CLIProxyAPI):
# - openresponses.gpt-5.4      ← Codex (free tier, rotated)
# - openai.gpt-4o-mini          ← OpenAI (paid, fast)
# - openai.gpt-4o               ← OpenAI (paid, capable)
```

#### Adding More Accounts

```bash
# 1. Login new Codex account
cli-proxy-api --codex-login

# 2. Add OpenAI keys → edit ~/.cli-proxy-api/config.yaml
openai-compatibility:
  - name: "openai"
    api-key-entries:
      - api-key: "sk-proj-NEW_KEY"

# 3. Restart proxy
cli-proxy-api  # auto-discovers new codex-*.json tokens
```


