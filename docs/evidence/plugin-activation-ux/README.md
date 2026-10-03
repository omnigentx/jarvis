# Plugin activation UX acceptance

## Root cause and scope

The supplied screenshot shows `agents`, `host_connectors` and `mcp_requires_policy_review`. `backend/services/plugins/package.py` identifies incompatible capabilities. `frontend/src/views/settings/SettingsPlugins.vue` disabled Activate through `supported()` but printed only internal blocker codes. The target selector had no bulk option. Selecting Jarvis could not resolve missing runtime adapters.

This change adds localized explanations, a truthful Activation blocked label, action guidance next to the disabled button, and an explicit All available agents option. It does not bypass backend compatibility or sandbox policy checks. Host connectors and plugin-defined agents remain unsupported; source reputation never grants execution rights.

## Bulk semantics

- Current available runtimes only, including explicit team run IDs returned by the existing targets API. No future-agent or global-sharing grant.
- Deduplicate targets, snapshot the scope before review, and show every target in the dialog.
- Obtain an existing signed, target-specific review token for each runtime before confirmation. Digest or execution-policy differences abort the whole review without activation.
- Activate sequentially through the existing manual endpoint, with no automatic retry or polling. A failed runtime does not block later targets. The displayed result uses that runtime's ACK, not the aggregate package Ready status.
- Expired/changed tokens and stopped runtimes retain existing backend failure behavior. A failed inventory refresh retains individual activation outcomes.

## Tests

TDD: the new activation unit suite first failed with ERR_MODULE_NOT_FOUND; implementation then passed all three cases.

- Frontend unit suite: 247 passed.
- Frontend build: passed (existing chunk-size warnings remain).
- Plugin Playwright suite: 13 passed. Includes desktop and 390×844 mobile, truthful partial failures, disabled unsupported package, safe content display, source consent, policy saving, changed digest and stopped-target review with zero activation calls.
- Existing backend manual review, activation and execution-policy tests: 16 passed.

## Direct computer-use acceptance

Used local UI at `http://127.0.0.1:3036/settings`, real FastAPI plugin routes, real SQLite lifecycle, and three real fast-agent McpAgent runtimes (Jarvis, Dev, QA). Marketplace acquisition and unrelated boot endpoints were test fixtures; no third-party executable plugin or LLM call was used. This is a runtime integration test, not evidence of a live marketplace connector executing successfully.

1. Add the self-authored fixture via UI, confirm source notice, download.
2. Select All available agents (3): Activate becomes enabled.
3. Review: Jarvis, Dev, QA are listed; confirm is disabled until consent checked.
4. Confirm: all three bindings become Ready; each calls the actual `read_skill` tool immediately with isError=false. See `runtime-read-skill.json`.
5. Add fixture with unsupported agents/host connectors; select Jarvis: activation stays blocked with readable explanations.
6. Repeat all-target confirmation on mobile, including enabling and clicking Confirm; three results remain Ready.

Evidence images are actual computer-use screenshots, not mockups. `all-targets-results-desktop.jpg` shows selected bulk scope, enabled Activate and three acknowledged results. `mobile-all-targets-review.jpg` shows the review scope at 390×844. Playwright screenshots use controlled API responses, separately from these runtime integration screenshots.
