# Contributing to Jarvis

Thanks for your interest! This project is in active development and contributions are welcome.

## Before you start

- Check existing issues and pull requests to avoid duplicate work.
- For substantial changes (new agents, new MCP tools, architecture shifts), open a discussion or issue first so we can align on direction.
- Read [`AGENTS.md`](AGENTS.md) and [`backend/ADDING_AGENT_GUIDE.md`](backend/ADDING_AGENT_GUIDE.md) — they explain how the spawn system, agent cards, and MCP tool servers fit together.

## Dev setup

```bash
# Clone with submodules (fast-agent, figma-ui-mcp, mcp-atlassian, RealtimeSTT, RealtimeTTS)
git clone --recurse-submodules https://github.com/<your-user-or-org>/jarvis.git
cd jarvis

# Configure secrets (do not commit)
cp backend/.env.example backend/.env
cp backend/fastagent.secrets.yaml.example backend/fastagent.secrets.yaml
# Edit both files with your API keys

# Bring the stack up
docker compose up -d --build

# Run backend tests
cd backend && uv run pytest

# Run dashboard tests
cd ../dashboard && npm run test:unit && npm run test:e2e
```

## Branch & commit conventions

- Work on a feature branch off `main`. Keep branches focused.
- Commit messages: short imperative subject ("fix race in spawn registry"), longer body for "why" if needed.
- Squash trivial fixup commits before opening a PR.

## Code style

- **Python**: follow surrounding style. The repo uses `ruff` and type hints where present — match what you see in the file.
- **TypeScript / Vue**: keep dashboard components small; prefer composition API.

## Pull request checklist

Before requesting review:

- [ ] Tests pass locally (`uv run pytest` for backend, `npm run test:unit && npm run test:e2e` for dashboard)
- [ ] No secrets, hardcoded paths, or personal info introduced
- [ ] Updated docs / SKILL.md / `.env.example` if behavior or config changed
- [ ] PR description explains *why*, not just *what*

## Submodules

`backend/fast-agent`, `backend/figma-ui-mcp`, `backend/mcp-atlassian`, `backend/RealtimeSTT`, and `backend/RealtimeTTS` are pinned submodules (the last two are forks of `KoljaB/RealtimeSTT` / `KoljaB/RealtimeTTS` under `omnigentx`). If you need to change code inside a submodule:

1. Open a PR in the submodule's own repo.
2. After it merges, bump the submodule pointer in `jarvis` with a follow-up PR.

Do **not** commit modifications inside submodule directories from the parent repo.

## Reporting bugs

Open a GitHub issue with:

- A minimal reproduction
- Expected vs. actual behavior
- Logs (`docker compose logs jarvis-backend`) or stack traces
- Environment (host OS, Docker version, branch / commit)

## Reporting security issues

See [`SECURITY.md`](SECURITY.md). Don't file public issues for vulnerabilities.

## License

By contributing, you agree your contributions will be licensed under the project's [MIT License](LICENSE).
