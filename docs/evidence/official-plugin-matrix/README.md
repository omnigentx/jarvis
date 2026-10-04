# Official plugin acceptance and prior handoff audit

Observed 2026-10-04, full localhost Jarvis (UI 3038/backend 8048), isolated runtime DB. Actual vendor GitHub downloads and real existing Jarvis agent; no fixture, legacy-MCP or shell fallback in the two skill-use calls. Manual install paths were exercised through Chrome Settings UI with source and content confirmation, followed by immediate real chat use.

## Actual matrix

| Package | Immutable source | Result |
|---|---|---|
| Anthropic frontend-design 1.1.0 | anthropics/claude-code @ 2bfb629dfaff0c8318047a4beb93cf1dc5b58b18, plugins/frontend-design | Downloaded, reviewed, activated on Jarvis; real read_skill returned vendor content; chat applied it to three UX suggestions respecting Inter/dark. PASS for this manual skill workflow. |
| Anthropic claude-opus-4-5-migration 1.0.0 | same repo/commit, plugins/claude-opus-4-5-migration | Downloaded, reviewed, activated; real read_skill returned vendor content. Output correctly identifies target string, Haiku exclusion and evidence-based prompt-change condition. PASS for read/analyze; no code/model changes requested or tested. |
| OpenAI build-ios-apps 0.1.2 | openai/plugins @ 5fd93af4cd0c623e020d0cc7e9ce178b4ac1f70f, plugins/build-ios-apps | First UI download FAILED with 422 Duplicate MCP server name. After parser fix/restart, same UI download succeeds with 9 skills and xcodebuildmcp. NOT functional acceptance: activation remains blocked by metadata classification, executable content and missing execution policy. |
| OpenAI atlassian-rovo 1.0.6 | same OpenAI commit, plugins/atlassian-rovo | Prior actual UI OAuth/install/activation/read and post-restart new-chat read succeeded; see ../rovo-live-oauth. Read/search permissions only. |

Digests: frontend-design b2e78c655ab3f098b982c5780fca0beba38b740a4a2827d7d01d8b6563febc3a; migration 4bdbe7aa85f38585c0c44bbb051608f40f5cff055fe9bff2ca44d2dec94666aa; build-ios-apps b61f11cd5bf2098b7a9bf41bf1d30e72dea9a248e4ce8cb25370a5cc8d7e2fbc.

`actual-skill-use.json` records actual tool inputs and result hashes/lengths instead of copying vendor skill implementations into the repo. `actual-skill-use.jpg` shows the chat output and two tool calls. `duplicate-mcp-before.jpg` shows the pre-fix UI error; `build-ios-blocked.jpg` shows the post-fix remaining blockers. Runtime restoration after the parser-fix restart showed the two Anthropic bindings Ready again, but their post-restart use was not rerun in this matrix.

## Newly reproduced defect and bounded fix

`backend/services/plugins/package.py::_servers` read the implicit `.mcp.json` and then read it again when the manifest declared `mcpServers: "./.mcp.json"`. This is exactly what the real build-ios-apps manifest contains. The parser falsely reported a duplicate server. It now deduplicates resolved **file paths**, never distinct declarations/server names. Existing conflicting inline configuration remains rejected. RED: two Codex/Claude contract tests failed with the same PackageError before the fix. GREEN: 33 package tests; plugin service/route/runtime regression 231 passed, 3 skipped (OCI integration prerequisites absent), 3 warnings. See backend-tests.txt. Backend restarted before repeating the same real package UI download.

Additional gap: actual build-ios-apps `agents/openai.yaml` contains only `interface` including `default_prompt`. The current strict display_metadata allowlist excludes that field and still produces an `agents` blocker; it is not evidence of a real executable agent definition. Its executable resources and npx xcodebuildmcp@latest also require a reviewed compatible runtime; no host execution, network/sandbox bypass, or blanket allowlisting was performed. This package is not counted as installed-and-usable.

## Why the previous two handoffs were incorrect

Reviewed actual merged PR bodies #172 and #173 and old source at b88258a3aa67aefe276d217324de765740829bcd:

- #172 fixed source reputation/direct manual confirmation, but computer-use acquisition used a self-authored fixture skill. It explicitly did not revalidate real marketplace downloads or MCP sandbox execution.
- #173 fixed blocker messages/all-current targets. Its acceptance likewise used fixture packages and real agents reading a skill, not the actual Rovo transport/account integration. The PR explicitly documented unsupported connectors and deferred them to SCRUM-55.
- The user's actual Rovo package had zero skills, display metadata under agents/openai.yaml, a Codex-host connector identity and HTTP OAuth MCP. Old package.py:253 classified metadata as agents; :250 blocked .app.json; :281 required MCP policy. policy.py:88 rejected unsupported adapters; sandbox.py:51 rejected non-stdio/url transport. No HTTP OAuth connection path existed. Agent selection or source approval could not make that package usable.
- Source approval authorizes download/inspection, not activation/account consent. The first screenshot showed PLUGIN_SOURCE approval; reporting that as a complete installation also hid a distinct lifecycle stage.

The technical compatibility gap was known. The handoff error was mine: I accepted a different fixture workflow as evidence, treated narrowed UI fixes/CI as sufficient for the requested real-plugin workflow, and allowed a blocking adapter requirement to be deferred while handing off and retagging. The tests were valid for their narrow scope; the acceptance decision was invalid for the user's requested outcome.

## Remaining gates

PR #174 remains draft. Follow-up actual agent self/subordinate install, immediate skill use, denial, real Rovo credential-fault/refresh tests and live Settings push evidence are documented in `../plugin-agent-team-acceptance/README.md`. Rovo access for the two team agents awaits explicit grant confirmation; production HTTPS consent smoke remains a deployment gate. Automatic approval continuation and capability-aware delegation remain separate backlog work. Reputable-source labels do not guarantee compatibility or security. Existing docs/plugin-handoff-gates.md requires exact real-package install/activate/use evidence and explicit blockers before handoff. Further build-ios compatibility/runtime work is tracked in SCRUM-58; it is not silently presented as completed.
