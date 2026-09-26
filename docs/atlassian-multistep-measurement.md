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
`SCRUM`. The local ignored `backend/fastagent.secrets.yaml` still points at
the retired site; new local credentials and URLs are required before a live
read-only run. Never add API tokens to this document or the benchmark output.
