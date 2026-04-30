---
name: fast-agent-automation
description: Automate fast-agent runs from CLI, Docker, and Hugging Face Jobs. Use when users need repeatable non-interactive execution.
---
# Fast-Agent Automation
Build repeatable automation around `fast-agent` CLI, container, and cloud job execution.
## Execution Modes
- **local CLI**: `fast-agent go --message "..." --results ./out.json`
- **Docker**: Containerized execution with `uv`
- **Hugging Face Jobs**: Scheduled/cloud execution with secrets management
## Key Commands
### Single-shot local
```bash
fast-agent go \
 --model sonnet \
 --message "Summarize findings" \
 --results ./artifacts/run.json
```
### Card-based run
```bash
fast-agent go \
 --card ./cards \
 --agent researcher \
 --model sonnet \
 --message "Summarize findings" \
 --results ./artifacts/run.json
```
### Multi-model compare
```bash
fast-agent go \
 --card ./cards \
 --model "haiku,sonnet" \
 --message "Give a concise plan" \
 --results ./artifacts/compare.json
```
## Model Selection Policy
- Agent card `model:` outranks CLI `--model`
- Prefer omitting `model` in cards for runtime flexibility
- Pass `--model` in CI/Jobs wrappers
## Critical Output Distinction
- Terminal output (`stdout`): for humans
- `--results` file: machine-readable agent message history (always use for automation)
## Security
Never relay raw secret values. Only relay env var **names** through approved channels.
## Source
- Repository: https://github.com/fast-agent-ai/skills
- Path: skills/fast-agent-automation/
