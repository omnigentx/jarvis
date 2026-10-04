# Plugin handoff audit — 2026-10-04

## Current verdict

**The user's Atlassian Rovo workflow has NOT passed acceptance.** PR172/173 improved manual review and UI but did not make this package usable. Their successful skill-only fixtures and CI counts do not establish Rovo compatibility. This report corrects the product-level handoff, not the historical test results.

## Reproduction using the actual vendor package

- Jarvis revision: `b88258a3aa67aefe276d217324de765740829bcd`.
- Upstream: `openai/plugins`, revision `5fd93af4cd0c623e020d0cc7e9ce178b4ac1f70f`, subtree `plugins/atlassian-rovo`, version 1.0.6.
- Runtime download through the existing bounded acquisition implementation, followed by real `inspect_package` and `validate_policy`. Downloaded files were temporary and removed; no vendor implementation is committed.
- Digest: `8616fb097443c9f83f396f8e8aadf5b1994405ff5bcf5231de73c671df8356ce`, matching the earlier user screenshot.
- Skills: 0; actual MCP transport: HTTP; endpoint: `https://mcp.atlassian.com/v1/mcp/authv2`.
- Observed blockers: `agents`, `host_connectors`, `mcp_requires_policy_review`.
- Actual validation error: `This plugin needs an unsupported runtime adapter`.
- No plugin execution or successful Rovo tool call occurred. See `rovo-inspection.json`.

Reproduce from backend:

```sh
UV_FROZEN=1 PYTHONPATH=. uv run python scripts/probe_rovo_compatibility.py
```

The probe exits 2 while blocked. This is a compatibility probe, not an OAuth or functional E2E test. A successful future probe is necessary but insufficient for acceptance.

## Code findings

1. `backend/services/plugins/package.py:248` and `:253` block any file under `agents/`. Rovo's `agents/openai.yaml` is interface metadata (display name, description, icons), not an executable agent definition. The current agent-definition explanation is too broad.
2. `backend/services/plugins/package.py:281` reports any MCP as requiring policy, without distinguishing remote HTTP from sandboxed stdio.
3. `backend/services/plugins/policy.py:88` refuses the incompatible package; `backend/services/plugins/sandbox.py:51` rejects non-stdio transports. A Docker image policy cannot establish this remote HTTP connection or its authorization.
4. `frontend/src/components/plugins/PluginExecutionPolicy.vue:46` presents the stdio sandbox form whenever server_names exists. For this HTTP package it offers a path that cannot succeed.
5. The `.app.json` references a Codex-host connector identity. Repository reputation does not provide that connector's authorization in Jarvis. Its required/optional relationship to remote MCP needs explicit adapter design; never silently discard it.

## Why handoff failed

| Decision | Evidence actually available | Gap |
|---|---|---|
| Treat plugin workflow as accepted | Registered skill reader, local fixture, mock fault tests | User's zero-skill HTTP connector package was never the acceptance case |
| Fix explanation and bulk selection | Correct selector/confirmation/runtime-ACK behavior for supported adapters | Does not resolve actual plugin activation |
| Put unsupported components in backlog | Limitation disclosed in PR | A blocking user requirement was treated as optional follow-up |
| Retag release after merge | Exact commit CI, deployment health check | Service health does not establish plugin login/list/call or user outcome |

## Replacement acceptance gate

Use `docs/plugin-handoff-gates.md`. Reports identify the exact requested plugin, tested commit/digest, targets and authorization path. Distinguish mock/replay, actual package acquisition, live runtime and actual external calls. Do not declare the user's workflow ready or close its acceptance task while required cases are blocked, skipped or replaced by another plugin.

## Outstanding work (not completed by this audit)

- Accurate metadata-versus-agent classification and remote-MCP compatibility guidance.
- Scoped remote HTTP/OAuth connection and credential lifecycle, explicit connector semantics and safe URL policy.
- Real Rovo UI install/connect/activate/tool-use in the existing agent and team, including all-current-agent scope and desktop/mobile, with observable failure/recovery and fresh screenshots.
- No claim of Rovo functionality until actual authorized external tool results validate the user's task. Existing built-in Jira tools are not a substitute for proof of this plugin.
