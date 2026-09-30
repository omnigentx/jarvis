# Runtime plugins — implementation checkpoint (SCRUM-42)

Status: **WIP; not a production acceptance report or Ready-to-merge claim.**

## Implemented

- Common metadata inventory for portable, Claude and Codex manifests; Codex overlay replacement semantics.
- Catalog discovery against immutable GitHub commits, including external URL/git-subdir sources. Catalog labels do not authorize external publishers.
- Runtime downloads select the plugin subtree instead of fetching the entire repository. Every downloaded file is checked against its pinned Git blob hash. No plugin code is vendored into Jarvis.
- Source approval before download; separate content/target/hash approval before activation.
- SQLite package/binding inventory, cross-process operation locks, digest revalidation, truthful runtime ACKs, cancellation and failure states.
- Skills adapter uses the public fast-agent instruction-refresh API. Prevents implicit shell grants; namespaced removal identifies exact package paths so removing an old version preserves the new version.
- Main-process skill activation and removal; inactive file cleanup retains audit records. Active or uncertain bindings remain pinned.
- Settings plugin preview, explicit skill-content review as text, approval links, target selection and pushed SSE state. Stale snapshots cannot overwrite newer events.
- Missing runtime manifests invalidate a persisted Ready claim when inventory is read.

## Evidence and its limits

| Verification | Observed result | Limit |
|---|---|---|
| Backend plugin/API suites | 90 passed | Includes mocks for approval/runtime error contracts |
| Frontend unit suite | 240 passed | Includes three new snapshot/SSE regression tests |
| Playwright UI flow | Desktop + 390px mobile pass | Backend mocked; not live Jarvis/team E2E |
| Frontend build | Passed | Existing bundle-size/import warnings remain |
| Ruff on new Python modules/tests | Passed | Scoped to new implementation |
| Public catalog discovery | OpenAI 65 entries, Anthropic 13 entries | Snapshot counts, not compatibility counts |
| Real Anthropic frontend-design package | Activate → read_skill → remove succeeded, shell remained disabled | Isolated real McpAgent; no chat LLM, Jarvis server or spawned team |

Reproduce the real-source probe from `backend` with `PYTHONPATH=.:fast-agent/src uv run python scripts/probe_plugin_runtime.py`. It creates an isolated McpAgent, performs no LLM calls, and deletes downloaded content on exit.

`real-package-probe.json` records the exact commit/digest, ACKs, read length and time. The observed 2.443 seconds and zero LLM calls describe this one skill probe; they establish neither teamwork latency nor token savings.

`plugins-desktop.png` and `plugins-mobile.png` show the mocked failure/review state, not a successful live installation. The test-results directory also holds Playwright traces on failure.

## Defects found by tests/probes

1. Real Codex catalogs use `url` and `git-subdir` sources; the first parser rejected the entire catalog. Added strict GitHub URL normalization and regression tests.
2. Skill refresh can implicitly enable shell in fast-agent. Added a capability-escalation guard and real McpAgent regression test.
3. ZIP acquisition exceeded the limit for the real Claude repository. Replaced acquisition with selected Git-tree/blob downloads while keeping bounded archive inspection tests.
4. Namespace-only removal deleted a newer plugin version's skills. Removal now uses exact package skill paths.
5. Persisted Ready survived missing runtime. Inventory now reports needs_reactivation when live manifests are absent.
6. AgentApp.get_agent returns None for an unknown agent. Added an explicit negative ACK instead of allowing AttributeError.

## Decisions

- Fail closed on unsupported capabilities. MCP, executable hooks/scripts, host-specific connectors, commands, agents and other unsupported contributions cannot obtain Ready from this implementation.
- Do not inject whole skill bodies into instruction context. The real runtime exposes a reader and advertises metadata; body is loaded explicitly.
- Reuse the existing approval and SSE systems. No UI polling or dependency-install commands run during inspection.
- Current authentication is single-tenant. No arbitrary client-provided user ID is presented as a security boundary.
- Never purge ready or uncertain bindings. Acknowledged disabled/unbound packages may expire after 24 hours of inactivity; this provisional retention differs from the separate seven-day Atlassian content cache.

## Remaining acceptance gates

- Secure MCP transport/configuration adapters, credential UX and executable isolation policy with actual sandbox enforcement.
- Run/session-scoped acknowledged capability updates for active team subprocesses, safe turn boundaries, timeout/cancel/reconnect/resume behavior.
- Durable bindings restored or explicitly reconciled after backend/agent restart; no config-reload overwrite of plugin skills.
- Package updates, disabled/uninstalled states exposed through the complete UI, global-promotion approval and agent/team permission rules.
- Automatic maintenance/crash recovery and cleanup of bounded operation-lock/audit directories. The current cleanup is opportunistic during install, not a retention scheduler.
- Live authenticated Jarvis UI/chat and already-running team E2E, manual computer-use inspection, approval replay, security adversarial tests and token/tool-call A/B evidence.
- Full desktop/mobile visual acceptance, including installation success, pending approval, unsupported capability and disconnected/reconnected states.

GitNexus analyzed 53 new implementation functions: no HIGH/CRITICAL result, two MEDIUM inventory helpers. Five traversal results are partial because the installed graph backend rejected a read query as a write; `impact.json` retains these limitations. They must be resolved or supplemented before final acceptance. Existing Settings grouping/label functions were checked separately (LOW).
