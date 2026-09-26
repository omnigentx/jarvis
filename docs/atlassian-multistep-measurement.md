# Atlassian multi-step output audit and file-cache proposal

## Reproducible replay of historical results

Run from `backend/`:

```bash
UV_FROZEN=1 uv run python scripts/measure_atlassian_multistep.py \
  --db /path/to/jarvis.db
```

The script opens an existing Jarvis SQLite database read-only, finds the newest
complete agent-context snapshot with Atlassian results, and prints only
counts and token measurements. It never prints issue/page content. Snapshot
`821` in the local development database contains one real Jira search (ten
issues), one Confluence search, and five complete page reads. There were no
compact-mode calls, Jira issue follow-up reads, or repeated Confluence page
reads in this trace. `cl100k_base` measures serialized tool output, not the
model's complete input, cached tokens, billing, latency, or answer quality.
The observed seven tool results introduced 20,035 output tokens in order:
Confluence search 2,047; Jira search 4,810; then five page reads of 3,525,
3,110, 2,015, 2,167, and 2,361. The script reports cumulative totals at
each step so later repetitions can be compared without changing the metric.

### Exploratory read-only Jira Cloud run on the replacement tenant

The existing local Jira credential authenticated successfully to the new
`omnigentx.atlassian.net` site and project `SCRUM`. A direct run through this
PR's `JiraFetcher` and response projection found four issues, all without a
description. The same `cl100k_base` serialization counted:

| Cloud path | Tool-output tokens | Tool calls |
| --- | ---: | ---: |
| Full search, four issues | 1,174 | 1 |
| Brief search only | 594 | 1 |
| Brief search + actual `get_issue` for one issue | 899 | 2 |
| Brief search + actual `get_issue` for all four | 1,820 | 5 |

The full and brief searches returned the same issue ordering. This is one
small, sparse project and one sequential run; latency was not repeated or
controlled for order. It establishes that follow-up reads can erase output
savings even on a live tenant, but it cannot test whether omitted descriptions
would hurt answer quality.

The newly provisioned Confluence site initially returned HTTP 401 on read
endpoints, then returned 200 with the same credentials. Its exact cause was
not established. A later direct run through this PR's `ConfluenceFetcher`
measured two pages:

| Cloud page | Body characters | Full tokens | Metadata tokens | Full fetch | Metadata fetch |
| --- | ---: | ---: | ---: | ---: | ---: |
| Page 1 | 571 | 307 | 147 | 784 ms | 800 ms |
| Page 2 | 7,280 | 2,474 | 242 | 842 ms | 750 ms |

For page 2, `metadata` followed by `full` would introduce 2,716 tool-output
tokens and two upstream page fetches, compared with 2,474 tokens and one
fetch for `full` alone. The second fetch was performed to measure its latency;
the same page version (`1`) was returned. This small, one-pass sample does not
measure total LLM usage or answer quality.

| Step using the same real content | Output tokens | Tool calls | Interpretation |
| --- | ---: | ---: | --- |
| Jira search, full | 4,810 | 1 | All ten descriptions available |
| Jira brief, then one selected issue | 2,507 | 2 | Follow-up issue body is an optimistic proxy built from the search result |
| Jira brief, then five selected issues | 4,440 | 6 | Only 370 fewer output tokens than full; actual `jira_get_issue` can be larger |
| Jira brief, then all ten selected issues | 6,772 | 11 | 1,962 more output tokens than full, even with the optimistic proxy |
| Confluence search + five full pages | 15,225 | 6 | Observed access pattern |
| Confluence search + metadata, then five full pages | 15,992 | 11 | 767 extra output tokens and five extra calls when all bodies are needed |

The Confluence `metadata` mode currently calls `get_page_content` before
removing the body from its response, so a metadata-first path also performs an
upstream full-page fetch. The search result already contains titles, links and
short content snippets. Whether a metadata read improves selection needs a
task-level comparison; it should not be counted as an automatic saving.

An isolated file-search probe wrote the first real page body to a private
temporary file, verified its SHA-256 after reading, searched for `risk`, and
returned a bounded three-hit excerpt: 160 tokens versus 3,196 tokens for that
body. The file was removed on normal exit. This proves exact file retention
and bounded retrieval for that query, **not** that an agent would answer a
question correctly from the excerpt or avoid a real MCP call. The observed
trace has no repeated page read, so it provides no measured cache hit benefit.

## Existing spill lifecycle

`fast-agent` already spills tool text over 64 KiB to `.tool-outputs` in the
team workspace (or cwd if `TEAM_WORKSPACE` is absent) and places a path plus
preview in agent context. The five observed pages were smaller than that
threshold, so they remained in context. Spill files have no TTL, size quota,
content version, or deduplication. Explicit team deletion removes its
workspace, but normal completion does not remove these files, and cwd spills
are outside team deletion. On the local development machine, two spill
directories contain 65 files totaling about 6.8 MB; the oldest files are
over 100 days old. Some files may still be referenced by resumable sessions,
so existing files must be inventoried before migration or deletion.

## Proposed content-cache lifecycle

This is a design proposal; the PR does not enable the cache yet.

1. **Scope and identity.** The chosen policy is a per-user cache reusable
   across that user's teams for seven days of inactivity. Key entries by
   authenticated Jarvis user, Atlassian tenant, credential principal,
   resource type/ID, source version or `updated` value, conversion format,
   and requested field set. Team members may share an entry only when the
   owner and effective Atlassian principal match. The current Jarvis API key
   is single-tenant and does not distinguish multiple human users; until a
   real per-request identity exists, cache sharing must be limited to the
   existing default owner or disabled for identity-ambiguous clients.
2. **Storage.** Keep bytes in opaque content-addressed files under a private
   per-user cache directory. Keep version, source URL, SHA-256, size,
   created/last-access times, expiry, and reference state in SQLite. Markdown
   may be a body format, but Markdown filenames are not the version database.
   Write atomically; use `0700` directories and `0600` files; reject symlinks
   and paths outside the user cache root. Do not cache errors, partial results,
   credentials, or attachment binaries in the first version.
3. **Agent access.** Return a small content reference, source version and
   preview. Provide `search_content(ref, query, max_hits, context_lines)` and
   `read_range(ref, start, length)` with strict result-size limits and source
   line numbers. A generic filesystem path or unbounded file read is not a
   sufficient search interface. Every read rechecks owner and credential
   scope.
4. **Freshness.** Invalidate after Jarvis writes to the resource and when a
   newly observed version/`updated` value changes. Expose an explicit fresh
   read for critical decisions. Without Atlassian push notifications, an
   external edit can remain unseen until a fresh check or expiry; this must
   be visible to the agent.
5. **Cleanup without polling.** Expire entries on cache access/write and at
   backend startup; evict least recently used unreferenced entries when a
   per-user byte quota is reached; delete on explicit owner-data deletion,
   credential revocation, or tenant removal. Team completion or deletion does
   not remove a user's shared entries. Startup reconciliation removes orphan
   files after a grace period. Cleanup must never follow symlinks or delete
   outside the managed cache root.

The selected retention is seven days since last access; the byte quota still
needs a measured value. When a resumed agent holds an expired reference, the
tool returns a structured miss and fetches the resource again rather than
reading stale data. Existing `.tool-outputs` spills need a separate migration
and cleanup policy because they are not indexed by user or resource version.

## Acceptance measurement still required

On a valid Cloud test tenant, replay representative *complete tasks* with
the same model and task inputs under full, compact-on-demand, and file-cache
variants. Record total input/output/cached tokens, Atlassian and local tool
calls, upstream bytes, wall time (including p95), cache hits, answer accuracy
against required facts, and stale/permission failures. Include tasks that
inspect one, several, and all search results; revisit a page after context
compaction; handle a page update; and resume or delete a team. Randomize
variant order and repeat runs to estimate variance. Unit, integration, E2E,
and computer-use tests should cover file isolation, cleanup, reconnection,
and full-content fallback before the cache is enabled by default.

The replacement Cloud site is `omnigentx.atlassian.net` with Jira project
`SCRUM`. The developer's ignored `backend/fastagent.secrets.yaml` now points
at the replacement site, and both Jira and Confluence read endpoints currently
authenticate. Other environments must configure their own URLs and
credentials before MCP E2E tests. Never add API tokens to
this document or the benchmark output.

## Read-only task-level A/B probe on the replacement Cloud site

This follow-up used the configured local OpenAI-compatible proxy and the
actual `JiraFetcher`/`ConfluenceFetcher` with this PR's response projections.
The `coding-agent` alias resolved to `gpt-5.6-luna` on every recorded call.
Each arm received the same system prompt, user question, and available
read-only tool schemas at a given workflow stage. We randomized arm order
with a fixed seed and ran two repetitions per arm and question. The table uses
the API's **total LLM token usage** summed across all model calls, including
tool messages, rather than `cl100k_base` tool-output estimates. We also
inspected every tool-call sequence and final answer against the live source.
No Atlassian writes, team runs, file-cache hits, or credential changes were
part of this probe.

| Question and workflow | A: full total tokens, runs | B: compact total tokens, runs | B versus A, mean | Tool calls A → B | Answer check |
| --- | ---: | ---: | ---: | ---: | --- |
| Jira triage: identify the two in-progress issues and the Story; full versus brief search | 1,808; 1,810 | 1,236; 1,234 | −574 (−31.7%) | 1 → 1 | 2/2 correct in both |
| Confluence content: maximum simultaneous editors and five content types; full page versus metadata-first, then full on demand | 3,976; 3,990 | 5,101; 5,126 | +1,131 (+28.4%) | 2 → 3 | 2/2 correct in both |
| Confluence metadata: version and attachment filename/type; full page versus metadata-first | 3,876; 3,879 | 1,656; 1,656 | −2,222 (−57.3%) | 2 → 2 | 2/2 correct in both |

The Confluence metadata-first arm above was a **staged experimental wrapper**:
the agent could search and read page metadata first; the full-body tool became
available after that read. It is not the current production default. In a
separate free-choice run with the full-body tool immediately available, the
model selected the full page even for the metadata-only question. Both arms
then made the same two calls and used approximately 4,000 total LLM tokens.
Changing a response-mode default while leaving `full` selectable had the
same behavior: the model explicitly requested `full` on every run. These
observations limit any claim that opt-in compact modes reduce real agent
spend without a workflow change.

Confluence's `metadata` projection still fetches the full page from Atlassian
before omitting its body from the agent response. The metadata-first body
question therefore added a second upstream full-page fetch. The measured
reduction on the metadata-only question is in **LLM context**, not upstream
bytes. With only two runs per arm, wall times (about 7–11 seconds for these
Confluence tasks) cannot support a latency claim.

An additional Jira question asked for the description state of all four
issues. The full-search arm used 4,746 and 4,785 total LLM tokens; the
brief-search arm used 3,519 and 3,517. Both arms called `jira_get_issue` for
all four issues, so each made five tool calls. The Jira REST API returned an
empty ADF document (`content: []`) for each description, but both MCP full
search and `jira_get_issue` omitted the `description` key entirely. The full
arm answered cautiously that description state was unavailable; the brief
arm asserted that all were empty. The latter happened to match the upstream
API but was **not supported by the MCP result it saw**. This exposes a real
information-quality gap in the full projection at the time of the A/B run,
so the smaller token total must not be counted as a verified
quality-preserving win for that question. The subsequent mcp-atlassian fix
(`fda688d`) preserves `description: ""` for empty ADF, `description: null`
when Jira explicitly returns null, and absence when the field was not
returned or requested. A read-only recheck of all four Cloud issues found
`description: ""` in both full search and `jira_get_issue`, while `brief`
still omitted it. The A/B token and answer figures above are from before
this fix and have not been remeasured afterward.

These tasks use four sparse Jira issues and one Confluence onboarding page,
not representative team documents. No answer was judged by a model; the
checks used required keys/facts, with the description case reviewed manually
against the raw API and MCP outputs. The proxy did not report cached-input
tokens separately. The probe is evidence for these paths only, not a
production-wide token, latency, or teamwork claim. The resulting decision is
to retain `full` as the default, use `brief` for triage when its fields are
sufficient, and avoid forcing Confluence metadata before body questions.
The Jira description state now has model and MCP tool tests. Confluence's
metadata upstream fetch still needs a separate fix and measurement before a
wider rollout.
