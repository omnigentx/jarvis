---
name: hf-space-deployer
description: Deploy fast-agent MCP Servers to Hugging Face Spaces using Docker.
---
# HF Space Deployer
Deploy fast-agent agents as MCP servers to Hugging Face Spaces.
## Deployment Models
| | Shared Secrets | Token Passthrough |
|---|---|---|
| **Who pays?** | You (Space owner) | Users (their HF account) |
| **Setup** | Add API keys as Space secrets | Enable `FAST_AGENT_SERVE_OAUTH=hf` |
| **Best for** | Internal tools, demos | Public deployments |
## Quick Start
```bash
hf repo create <user>/<space> --repo-type space --space-sdk docker --exist-ok
hf upload <user>/<space> --repo-type space --commit-message "Deploy fast-agent"
```
## Space File Structure
```
space-directory/
├── README.md           # HF Space config with YAML header
├── Dockerfile          # Docker setup with Python 3.13 + uv
├── hf-api-agent.md     # Your agent card (filename = tool name)
└── hf_api_tool.py      # Optional: Python tools
```
## Source
- Repository: https://github.com/fast-agent-ai/skills
- Path: skills/hf-space-deployer/
