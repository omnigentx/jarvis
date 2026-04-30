---
name: lsp-setup
description: LSP-enable a Python or TypeScript repository for fast-agent development. Use when creating or refreshing agent cards with LSP function tools.
---
# LSP Setup
Enable LSP-based code navigation in a repository by creating an agent card with LSP function tools.
## Prerequisites
- **Python**: `ty` language server (`uv tool install ty`)
- **TypeScript**: `typescript-language-server` (`npm install -g typescript-language-server typescript`)
## Setup Steps
1. Examine repo root for `pyproject.toml` (Python) or `tsconfig.json` (TypeScript)
2. Create `.fast-agent/agent-cards/` directory
3. Copy template files: `dev.md` + `multilspy_tools.py`
4. Configure `_REPO_ROOT` and `_ALLOWED_DIRS` in `multilspy_tools.py`
5. Verify with `fast-agent go`
## Key Configuration
```python
# .fast-agent/agent-cards/multilspy_tools.py
_REPO_ROOT = Path(__file__).resolve().parents[2]  # 2 levels below repo root
_ALLOWED_DIRS = {"src", "tests"}
_ALLOWED_FILES = {"conftest.py"}
```
## Source
- Repository: https://github.com/fast-agent-ai/skills
- Path: skills/lsp-setup/
