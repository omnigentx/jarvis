# Official Atlassian Rovo: real localhost acceptance

Date: 2026-10-04. This supplements the historical failure in `docs/evidence/plugin-handoff-audit/README.md`.

## Outcome established

The actual `openai/plugins` package `plugins/atlassian-rovo` 1.0.6, commit `5fd93af4cd0c623e020d0cc7e9ce178b4ac1f70f`, digest `8616fb097443c9f83f396f8e8aadf5b1994405ff5bcf5231de73c671df8356ce`, was downloaded from the catalog through Chrome UI, inspected, connected through actual Atlassian OAuth, activated on the existing Jarvis runtime, and used immediately in a real chat to read SCRUM-55 from Atlassian Cloud. No fixture, legacy Jira MCP, shell or curl substituted for this call. The backend was restarted, then a new chat repeated the real read successfully without reinstallation or new consent.

- Full localhost backend: 8048; UI: 3038; isolated SQLite/runtime directory.
- Actual Rovo server: `https://mcp.atlassian.com/v1/mcp/authv2`.
- OAuth consent retained read/search toolsets for this acceptance run; write toolsets were deselected.
- 27 tools discovered. Actual call: `plg-747d77396094fee4bcb3__getJiraIssue`, `cloudId=omnigentx.atlassian.net`, `issueIdOrKey=SCRUM-55`, fields `summary,status`.
- Result: key SCRUM-55, expected summary, status To Do at observation time. See `real-tool-use.json` for actual input/output and usage.
- `real-plugin-chat.jpg`: first successful actual call; `real-plugin-ready.jpg`: package/account/binding state; `real-plugin-after-restart.jpg`: new chat after restart; `real-plugin-mobile.jpg`: 390×844 viewport, Jarvis binding Ready and controls.

## Root cause and changes

The display-only `agents/openai.yaml` was misclassified as an executable agent. The package's Codex connector identity required an explicit native-Rovo adapter; Jarvis does not emulate Codex connectors. A sandboxed stdio policy cannot authorize HTTP MCP. This change classifies the known metadata/connector accurately and implements OAuth plus a host-owned MCP transport bridge through public MCP SDK and fast-agent attachment APIs. Downloaded executable plugin code is not run on the host. Other agent definitions, unknown connectors and arbitrary remote endpoints remain blocked.

Tokens/client registrations are encrypted in the Jarvis DB. Refresh requests serialize across processes. Consent is bounded, state/PKCE checked by the SDK, callbacks are one-use and redacted, and results are pushed over SSE. Disconnect requires disabled bindings/global sharing; uninstall/expiry removes credentials. Remote policy review shows actual reviewed HTTP endpoints, not a false network:none sandbox claim.

Production OAuth requires `JARVIS_PUBLIC_URL=https://<your-host>` and routing `/api/plugins/oauth/callback` to the backend. Keep the DB/master key persistent together. Ensure reverse-proxy/access logging does not retain OAuth callback query strings. Local acceptance disabled access logs; production deployment has not been tested by this run.

## Tests and limitations

- Plugin regression including remote routes: 222 passed, 3 skipped in `backend-tests.txt` (including auth, callback redaction, live binding restriction, disconnect cleanup/event).
- Full backend run: 2552 passed, 4 skipped, 5 deselected, 1 xfailed; see `backend-full-tests.txt`.
- Frontend unit tests: 248 passed; production build passed.
- Real browser E2E: manual official package install/connect/activate/use, followed by restart and a new chat/use.
- RED → GREEN tests covered metadata classification, remote consent prerequisites, OAuth HTTP response forwarding and credential cleanup.
- Skipped OCI/isolated integration cases are not counted as passed. Their environment has not been provisioned for this run.
- All-current-agent activation review renders all 8 current targets. The final grant was not submitted; real multi-agent OAuth usage/agent-initiated remote account connection is not claimed.
- Token expiry/rotation, revoked consent and production HTTPS callback need extended acceptance. PR stays draft until the full required matrix passes; this report does not claim production or full team acceptance.

## Observed orchestration and cost gap

The first ordinary chat delegated to ResearchAgent without this plugin. Its missing-tool response was incorrectly attributed to the parent Jarvis. The parent actually had the tools; an explicit direct call succeeded. Backlog **SCRUM-57** tracks capability-aware delegation and accurate error attribution. No generic prompt change was made.

Successful direct read: one remote tool call, 2.5 seconds tool duration. Two LLM turns used 66,701 input + 156 output tokens; 36,237 effective input after reported cache hits. This is a measurement, not a saving claim: existing tool schemas/context are still large. The first unsuccessful delegation took 10.9 seconds and added unnecessary agent work. No dollar estimate is given because the proxy model alias `coding-agent` does not identify billable routing/pricing.
