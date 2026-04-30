---
name: terminal-execution
description: >
  Guide for using the execute tool to run shell commands in workspace.
  Use when agent needs to: build/test code, run scripts, check system status,
  install dependencies, or execute deployment commands.
---

# Terminal Execution Skill

Guide for using the `execute` tool to run shell commands safely and efficiently.

## When to Use Terminal

✅ **USE when:**
- Build/compile code (`uv run`, `npm run build`, `flutter build`)
- Run tests (`pytest`, `npm test`, `flutter test`)
- Check system status (`docker ps`, `systemctl status`)
- Install dependencies (`pip install`, `npm install`, `apt-get`)
- Quick file/folder checks (`ls`, `find`, `grep`, `cat`)
- Git operations (`git status`, `git diff`, `git log`)
- Docker operations (`docker compose up`, `docker build`)
- Migration scripts, seed data

❌ **DO NOT use when:**
- Reading/writing files → use `filesystem` tools (`read_text_file`, `write_file`)
- GitHub API operations → use `github` tools
- Long interactive sessions → out of scope

## Execute Tool Syntax

The `execute` tool (built-in from ShellRuntime) takes a single parameter:

```
execute(command="<shell command>")
```

**Key characteristics:**
- Runs in the **workspace directory** (not home dir)
- **90s timeout** — process killed if no output for 90s
- **Watchdog**: warns every 30s if no output
- Output **auto-truncated** if too long (prevents token overflow)
- Returns **exit code** at end: `process exit code was 0`
- Each call is a **separate shell session** — env vars and `cd` don't persist
- **No shell prefix needed**: correct `"ls -la"`, wrong `"bash -c ls -la"`

## Efficient Patterns

### Chain Commands
```bash
cd repo && npm install && npm test        # Sequential (stops on error)
echo "=== Tests ===" ; pytest ; echo "=== Lint ===" ; ruff check .  # Run all
```

### Check Before Acting
```bash
docker info > /dev/null 2>&1 && echo "Docker OK" || echo "Docker NOT running"
lsof -ti:8000 && echo "Port 8000 in use" || echo "Port 8000 available"
```

### Filter Long Output
```bash
docker logs container_name 2>&1 | tail -20      # Last 20 lines
grep -r "ERROR" logs/ | head -10                 # Find errors
find . -name "*.py" | wc -l                      # Count files
```

### Build & Test
```bash
cd backend && uv run pytest --tb=short -q 2>&1 | tail -20
cd frontend && flutter test --reporter compact 2>&1 | tail -20
docker compose -f docker-compose.yaml build --no-cache 2>&1 | tail -30
```

<safety_rules>
🔴 NEVER:
- `rm -rf /` or any unrestricted recursive delete
- Expose secrets/tokens/passwords via stdout
- Run `curl | bash` from untrusted sources
- Modify system files outside workspace (`/etc/`, `/usr/`)
- Kill system processes (`kill -9 1`, `killall`)

🟡 CAUTION:
- `rm -rf` → always specify exact path, check first
- `chmod`/`chown` → only within workspace
- `docker system prune` → ask user first
- `apt-get install` / `pip install` → only in venv/container

🟢 SAFE:
- All read-only commands (`ls`, `cat`, `grep`, `find`, `docker ps`)
- Build/test commands within workspace
- Read-only git commands (`status`, `log`, `diff`)
</safety_rules>

<error_handling>
When a command fails:
1. Read stderr FIRST — usually contains root cause
2. DO NOT retry immediately — analyze the error first
3. Try a simpler command — isolate the issue
4. Report clearly — include command + error output
</error_handling>

## Combining with Other Skills

| Skill | Terminal Use |
|-------|-------------|
| `code-review` | Run tests to verify fixes |
| `git-workflow` | `git status`, `git diff`, `git log` |
| `cicd-pipeline` | Validate workflow, local builds |
| `test-driven-development` | `pytest`, `npm test` |
| `jarvis-knowledge` | Explore codebase structure |
