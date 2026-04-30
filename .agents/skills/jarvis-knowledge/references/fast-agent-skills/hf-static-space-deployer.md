---
name: hf-static-space-deployer
description: Deploy static websites/SPA to Hugging Face Spaces using sdk:static.
---
# HF Static Space Deployer
Deploy static web content to Hugging Face Spaces.
## Deployment Modes
1. **Plain static files** (no build step): `sdk: static`, `app_file: index.html`
2. **Build-first static app** (React/Vite/etc.): + `app_build_command`
3. **Not for server-side**: Use Docker Spaces for backend needs
## Standard Workflow
1. Authenticate with Hugging Face
2. Create Space: `repo_type=space`, `space_sdk=static`
3. Upload project files
4. Validate app URL and behavior
## Security
- Static Spaces run inside an iframe
- Variables/secrets available via `window.huggingface.variables`
- Do not treat frontend variables as confidential
## Source
- Repository: https://github.com/fast-agent-ai/skills
- Path: skills/hf-static-space-deployer/
