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

`full` remains the default for compatibility. Agents can request `brief` or
`metadata` for navigation, then fetch the relevant full issue or page. On a
deterministic synthetic fixture, the compact Jira search used 1,332 rather
than 4,252 `cl100k_base` tokens; Confluence metadata used 72 rather than
1,466. The fixture repeats the same acceptance-criteria text many times, so
these numbers describe omitted output bytes, **not redundant information or
end-to-end token savings**. Run
`UV_FROZEN=1 uv run python scripts/measure_atlassian_output.py` from
`backend/` to reproduce.

### Content relevance audit of historical tool results

Local agent context snapshots contain one complete Jira search result for ten
real issues and five complete Confluence page results from earlier sessions.
The Jira call explicitly requested `description`; its ten descriptions contain
scope, deliverables, proposed solutions, and acceptance criteria. A `brief`
projection would remove all ten descriptions. The five Confluence bodies
(about 7,400–12,400 characters each) contain roadmap phases, risks, and
architecture decisions. A `metadata` projection would remove every body.
These fields are necessary for analysis, implementation, and QA tasks; a
subsequent full read would be required. The historical calls used the default
`full` mode, so the compact modes have **no observed production token saving**
in these sessions. The compact response may be useful for finding which issue
or page to open, but that use case still needs task-level measurement of
follow-up reads, total tokens, tool calls, latency, and answer quality.

The original page bodies were retrieved from complete context snapshots;
later compacted snapshots truncate their visible tool results. This audit
does not include a live Cloud tenant run of the new modes.

The multi-step replay, its limits, and the proposed content-file lifecycle
are documented in `docs/atlassian-multistep-measurement.md`. The replay does
not justify enabling compact responses or a file cache by default yet.

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
  tenant remain open. The earlier configured base URL returned HTTP 404 at
  Jira `/rest/api/3/myself`. The user has recreated the test site at
  `omnigentx.atlassian.net` with Jira project `SCRUM`; local ignored
  credentials and URLs still need updating before a read-only live run.
  Do not direct write tests at a production project or space.

The MR remains draft until the Cloud tenant test, private-scope security
boundary, and an end-to-end PM acknowledgement contract are accepted or
explicitly moved to follow-up work.
