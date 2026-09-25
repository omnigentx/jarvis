# Team work and Atlassian MCP acceptance

## Team control contract

Jarvis can bind multiple independent team sessions to one conversation. A
team's `session_id` is the durable identity; team names are only labels and
may repeat. Requirement changes are SQLite rows with monotonically increasing
revisions per team. `expected_revision` rejects a stale edit with HTTP 409;
an `idempotency_key` makes a retried request return its original revision.
The PM receives each change in its session-specific MessageBus inbox. The
`delivered` status means **queued in that inbox**, not accepted or applied by
the PM. The Team Monitor receives a small SSE event when queueing succeeds.
Pending rows are replayed once at startup or on explicit retry; there is no
background status poll.

## Atlassian output contract (Cloud)

| Operation | Agent-facing payload | Detail on demand |
| --- | --- | --- |
| Jira search, `brief` | Issue identity, summary, triage fields, browser URL, pagination | `jira_get_issue` returns the selected issue and its full fields |
| Jira attachment manifest | ID, filename, size, content type, creation time | Fetch exactly one attachment by ID, capped while streaming |
| Confluence page, `metadata` | Identity, URL, version, attachment manifest, body length | `confluence_get_page` in `full` mode returns the body |
| Confluence body diff | Existing version-to-version text diff | Attachment version changes require a separate manifest comparison |

`full` remains the default for compatibility. Agents choose `brief` or
`metadata` when triaging. On a deterministic synthetic fixture, the compact
Jira search used 1,332 rather than 4,252 `cl100k_base` tokens; Confluence
metadata used 72 rather than 1,466. These are output-size measurements, not
live tenant measurements or a claim about total agent cost. Run
`UV_FROZEN=1 uv run python scripts/measure_atlassian_output.py` from
`backend/` to reproduce.

## Self-created MCP security boundary

Generated MCP code is reviewed against a source SHA-256 before dependency
installation, protocol/function tests, or global promotion. The tool returns
a pending approval immediately; a later call with unchanged content proceeds
only after an explicit approval. Promotion copies reviewed source to a
content-addressed runtime directory. Catalog create/update through Jarvis
also requires a content-bound approval. Agents cannot use this path to edit
the built-in admin server.

This is a review gate, not an operating-system sandbox. Generated servers
still run under the backend's operating-system identity and share its Python
environment. Private per-agent skill and MCP ownership, automatic promotion,
and dependency isolation need a separate implementation and security review
before they can be described as production-ready.

## Verification and open acceptance gates

- Backend unit and service suite: 2,220 passed, 4 skipped, 5 deselected, 1
  expected failure on the isolated worktree.
- Fast-agent team isolation tests: 4 passed. Team subprocess E2E: 5 passed,
  1 skipped.
- Atlassian unit tests for Jira/Confluence servers and attachments: 175
  passed. Existing full-output behavior remains covered.
- The complete Atlassian unit suite has 10 failures on this branch and the
  same 10 failures on the clean base commit (`a038a36`), verified in a
  separate temporary checkout. They concern Service Desk comments,
  changelog/user serialization and a zero-argument tool schema. This MR
  adds 14 passing tests without introducing new suite failures.
- Dashboard Chromium fixture tests: 3 passed, including the revision SSE
  badge. These use synthetic SSE events, not a live agent run.
- Cloud integration and browser acceptance against a real Atlassian test
  tenant remain open. The available configured base URL returned HTTP 404 at
  Jira `/rest/api/3/myself` and the designated test project and space were
  unavailable. Do not direct write tests at a production project or space.

The MR remains draft until the Cloud tenant test, private-scope security
boundary, and an end-to-end PM acknowledgement contract are accepted or
explicitly moved to follow-up work.
