---
name: mcp-authoring
description: >
  Discover, install, scaffold, attach and debug MCP (Model Context Protocol)
  servers via the mcp_admin tools — your self-extension surface. Use when the
  user asks to add a new capability, wire up an off-the-shelf MCP package,
  inspect what's available, debug a flaky tool, or remove an attachment.
  Includes context-budget rules so the catalog doesn't dominate your context.
---

# MCP AUTHORING & USAGE

You can extend yourself with new MCP servers via the `mcp_admin` tools, AND
you can inspect/attach/detach existing ones. Read the matching section below
for the situation in front of you.

## Decision tree — pick the section

| Situation | Section |
|---|---|
| "What MCPs do I have? / Show me the catalog" | **Discovery** |
| "Install jq / add the github MCP / register this URL" — server already exists | **Path A** |
| "I need a custom tool that doesn't exist anywhere" | **Path B** |
| "Tool X is broken / silently failing" | **Hot-fix** then re-verify |
| "Detach this server from agent Y" | `mcp_detach_from_agent` (one call) |

## Discovery — inspect before mutating

Before adding/changing anything, find out what's already there. The right
tool depends on what you actually need to know:

| You need… | Call | Cost |
|---|---|---|
| List of catalog server names + transport + who's attached | `mcp_list_servers()` | compact (~80 chars/server) |
| Full config of ONE server (command, args, env, cwd) | `mcp_get_server(name="…")` | small |
| Full config of EVERY server (rare — usually wrong) | `mcp_list_servers(verbose=True)` | **heavy** (~500 chars/server × N) |
| Live tool list + descriptions for one server | `mcp_test_server(name="…")` | spawn subprocess, descriptions auto-trimmed to 240 chars |
| Generated (Path-B) workspace inventory | `mcp_list_generated()` | compact (just tool names) |
| Full manifest + history of one generated server | `mcp_get_generated(name="…")` | larger |

**Default to compact lists.** The catalog has ~28 servers; calling the
verbose version pulls ~13 KB into context. Get the names compactly, then
`mcp_get_server(name)` only the ones you'll actually act on.

## Path A vs Path B — never mix

| Situation | Flow |
|---|---|
| Server already exists as an npm/pypi package or a hosted URL | **Path A** (config-only) |
| You'd need to write the tool code yourself | **Path B** (scaffold-then-test) |

If you start Path B and then realize an existing package solves the
problem, drop the scaffold (`mcp_clean_workspace(scope="<name>")`) and
restart in Path A.

## Path A — install an existing MCP server

Five steps. Each calls one tool.

1. `mcp_check_environment()` — confirm `npx`/`uv`/`python` is available
   for the package's runtime.
2. (Optional) `mcp_check_package_safety(package_name="...", ecosystem="python|node")`
   if the package isn't on the recommended allowlist. Read the warnings
   block — short age, missing homepage, or typo-squat resemblance is
   reason to ask the user before proceeding.
3. `mcp_create_server(name=..., transport="stdio"|"http"|"sse",
   command=..., args=[...], env={...}, url=...)` — this also smoke-tests
   the config and returns `smoke_failed: True` if the server can't start.
4. If create succeeded but smoke failed: read the error, fix the
   command/args/env, then `mcp_update_server(name, patch=...)`.
5. `mcp_attach_to_agent(server="...", agent="Jarvis")` (or another agent).
   The `tools_added` array tells you what new capabilities you just gained.

**Done.** You can call the new tools immediately on the next turn.

## Path B — scaffold + code + test + promote

Twelve steps. The pipeline is staged: each tool's output tells you
whether to proceed or fix something first.

### Step 1: Probe environment

Call `mcp_check_environment()` and pick the language. Prefer Python
(your venv is set up); fall back to Node if the user explicitly wants TS
and `node` is present.

### Step 2: Read recommended packages

Call `mcp_recommended_packages()` and prefer the listed packages — they
have a known security track record. For anything else, call
`mcp_check_package_safety(package_name=..., ecosystem=...)` and read the
`warnings` array. If you see "very new", "no homepage", or "possible
typosquat", ask the user before adding it to `requirements.txt`.

### Step 3: Plan the tool surface

Decide every tool the server will expose BEFORE scaffolding. For each:

- **name**: snake_case, lowercase, ≤ 30 chars. Use a consistent prefix
  per server (e.g. `github_create_issue`, `github_list_repos`) — keeps
  tool discovery clean once attached.
- **description**: ≥ 10 chars; describe what the LLM should know
  (purpose + when to use + what comes back). This text goes into other
  agents' system prompts — be precise. Vague descriptions are the #1
  reason a tool never gets called.
- **args**: list of `{"name", "type"}` pairs.

Don't over-design — start with the minimum tools that solve the user's
ask. You can always `mcp_patch_tool(...)` or scaffold a v2 later.

**For deeper tool-design guidance** — naming conventions, API-coverage
vs. workflow-tool tradeoffs, response shape (JSON vs. Markdown),
pagination, MCP tool annotations (`readOnlyHint`/`destructiveHint`/
`idempotentHint`/`openWorldHint`), `outputSchema` + `structuredContent`,
and the 10-Q&A evaluation framework — see the upstream
[`mcp-builder`](../mcp-builder/SKILL.md) skill. Jarvis's current
scaffold emits bare `@mcp.tool()` (no annotations/outputSchema), but if
you intend to publish the server outside Jarvis, those design choices
matter and `mcp-builder` covers them.

### Step 4: Scaffold

```
mcp_scaffold_server(
  name="<lowercase_with_underscores>",
  description="<one sentence — appears in server.py docstring>",
  planned_tools=[
    {"name": "do_x", "description": "Do X to Y. Returns Z.",
     "args": [{"name": "input", "type": "str"}]},
    ...
  ],
)
```

Returns the directory path. The scaffold creates:
- `server.py` — FastMCP boilerplate with `@mcp.tool()` stubs raising NotImplementedError
- `tests/test_smoke.py` — placeholder pytest file
- `manifest.json` — spec snapshot (with `spec_hash` for drift detection)
- `requirements.txt` — only `mcp` listed; add more as needed

### Step 5: Implement tool bodies

Use the `Edit` tool (or filesystem MCP) to replace each
`raise NotImplementedError(...)` with real code. Read these rules:

**MUST**
- Keep the `@mcp.tool()` decorator and the function name unchanged —
  static_check enforces this against the manifest.
- Return JSON-serializable values (`str`, `int`, `dict`, `list`, `bool`).
  For complex output, return a `dict` and document keys in the docstring.
- Handle errors by returning `{"error": "...", "status": 4xx}` rather
  than raising — raised exceptions become MCP errors with no detail.
- Add type hints to every arg and the return value.

**SHOULD avoid (will trigger forbidden-pattern WARNINGS — not blocks)**
- `eval()`, `exec()`, `os.system()` — use `subprocess.run([...])` instead
- `subprocess.run(..., shell=True)` — use list-form arguments
- `pickle.loads(...)` from untrusted sources
- `__import__("os")` dynamic imports

If you DO need one of these (rare), justify in a comment so the warning
on the dashboard makes sense.

### Step 6: Static check (fast feedback loop)

```
mcp_static_check(name="...")
```

Returns `{ok, issues, warnings}`. **Issues block** (syntax errors,
missing/rogue tool definitions). **Warnings inform** (forbidden patterns,
lint hits). Fix every issue. Read every warning and decide if it's
intentional.

Iterate: edit code → static_check → repeat until `ok: true`.

### Step 7: Install dependencies

If you added new packages to `requirements.txt`:

```
mcp_install_dependencies(name="...")
```

Each generated server gets its OWN `.venv` — no conflict with the
backend's env. Stderr tail is in the response if install fails.

### Step 8: Smoke test (protocol-level)

```
mcp_run_smoke_test(name="...")
```

Spawns the server, runs MCP `initialize`, calls `list_tools`, disconnects.
Verifies your tool count matches the manifest. Does NOT call any tool.

If `missing` or `extra` is non-empty: your decorator names don't match
the manifest. Edit, static_check again, then re-smoke.

### Step 9: Functional tests (per-tool)

For EACH planned tool:

```
mcp_run_tool_test(
  name="...",
  tool_name="do_x",
  args={"input": "happy_path_value"},
  assertions=[
    {"type": "no_error"},
    {"type": "field_present", "path": "content.0.text"},
    {"type": "duration_under_ms", "ms": 5000},
  ],
)
```

Cover at least:
- **happy path** — typical input, full assertion set
- **edge case** — empty / null / boundary input
- **error path** — bad input → expect graceful error response

Available assertion types:
- `{"type": "no_error"}` — `result.isError == False`
- `{"type": "field_present", "path": "a.b.c"}` — JSON path exists
- `{"type": "regex_match", "path": "text", "pattern": "..."}`
- `{"type": "type_check", "path": "x", "expected": "str|int|list|dict|bool|float"}`
- `{"type": "duration_under_ms", "ms": 5000}`

Hard timeout per call: 30 seconds. If your tool legitimately needs
longer, redesign — long-running ops should kick off a background job
and return a job_id immediately.

### Step 10: Test suite (optional but recommended)

If you wrote pytest tests in `tests/`:

1. Add `pytest` to `requirements.txt` and re-run `mcp_install_dependencies`.
2. Run `mcp_run_test_suite(name="...")`.

Suite output is saved to `mcp_workspace/test_runs/{name}-{ts}.log`.

### Step 11: Verify

```
mcp_verify(name="...")
```

The gate. Returns `{ready, blockers, warnings, tested_tools, planned_tools}`.

`ready: true` requires:
- static_check passed
- install_deps passed
- smoke_test passed
- every planned tool has at least one passing functional test

`warnings` is informational — drift between scaffold spec and current
manifest, forbidden patterns, etc. They DON'T block but you should read
them and decide.

If `ready: false`: fix the blockers, re-run the relevant stage, verify again.

### Step 12: Promote

```
mcp_promote(name="...", attach_to=["Jarvis"])
```

Moves the generated server into the live catalog and attaches to the
listed agents. After promote you can `mcp_test_server(name)` anytime to
re-smoke + refresh the tool description cache.

**Promote refuses if `verify` is not ready.**

## Hot-fix after promote

If a tool misbehaves in production:

```
mcp_patch_tool(name="...", tool_name="do_x", new_code="""
def do_x(input: str) -> str:
    \"\"\"Updated implementation.\"\"\"
    return ...
""")
```

The decorator is preserved automatically. After patching, re-run from
Step 6 (static_check → smoke → tool_test → verify). The catalog row
points at the same `server.py`, so the next `mcp_test_server(name)`
picks up the new behaviour.

## Self-protection

You CANNOT mutate `mcp_admin` or `skill_server` via your own tools —
the RPC layer rejects with status 423. If the user wants to remove or
reconfigure those, they must use the dashboard MCP page. Don't try to
bypass — explain the limit and direct the user to the UI.

You CANNOT delete built-in catalog servers (those seeded from
`fastagent.config.yaml` on first boot). Built-ins are editable but
deletion returns 403.

## Workspace housekeeping

`mcp_clean_workspace(scope="test_runs")` — clears pytest log files
(safe, default). Use after a long iteration session.

`mcp_clean_workspace(scope="<name>")` — drops one generated server's
directory entirely. Use this if you scaffolded something you no longer
want, or as cleanup after a failed Path B that you're abandoning.

`mcp_clean_workspace(scope="all")` is BLOCKED for you — only the
dashboard can do that.

## Anti-patterns

- **Skipping verify before promote** — promote will reject anyway, but
  iterating verify → promote → fail → retry wastes turns. Always verify first.
- **Calling create_server with `command="rm"` or other destructive
  binaries** — even though there's no allowlist, this is unsafe; refuse.
- **Tool descriptions < 10 chars** — scaffold rejects, but if you sneak
  short descriptions in via patch_tool, the LLM consuming this server
  won't know when to use it.
- **Returning raw exceptions** — wrap in `{"error": str(exc), "status": 500}`.
- **Sharing state via globals between tool calls** — each MCP call is a
  fresh subprocess invocation; persist via files or the main DB instead.
- **Path A with relative `args` and no `cwd`** — if the smoke-test passes
  but `mcp_attach_to_agent` fails with `McpError: Connection closed`, the
  subprocess is dying because it can't find a relative file. Pass an
  absolute `cwd`, or absolute paths in `args`. (Path B sets cwd
  automatically; this only bites Path-A configs.)

## Context budget — keep mcp_admin payloads small

`mcp_admin` tool responses are designed to be cheap by default but you can
still blow up your context if you call them carelessly:

- **Don't call `mcp_list_servers(verbose=True)` unless you need
  command/args/env/cwd.** The compact default has everything you need to
  decide *which* server to dig into. For "show me what's available" tasks,
  the compact response is enough.
- **Don't loop `mcp_test_server` over every catalog server.** Each call
  spawns a subprocess and returns a tool list. If you need a tool inventory,
  the catalog already caches descriptions — only re-test when the user says
  a server is broken or you just promoted/updated it.
- **Don't read tool descriptions back into your reply.** They're in your
  context already; reciting them wastes tokens. Refer by name.
- **Tool descriptions are capped at ~240 chars in agent payloads.** A
  trailing `…` means truncated — call `mcp_get_server(name)` if you need
  the full text (rare).
- **Path B per-stage logs**: stages return small status dicts (`{ok, error,
  tools, missing, extra}`). The full pytest log is on disk, not in your
  context — direct the user to the dashboard if they need it.

If you find yourself with >100 KB of `mcp_admin__*` tool results in a
session, you're over-inspecting. Pivot to `mcp_get_*` for one-off lookups.

## When NOT to use mcp_admin

- The user wants a one-off shell command — use the `execute` tool, not
  Path B. Don't scaffold a permanent server for a throwaway action.
- The user wants to read/write files — use the filesystem MCP that's
  already attached, not a new server.
- The user wants to test code, not deploy a tool — use the `tests/`
  directory + `pytest`, not Path B's pipeline.
- You're already mid-Path-B and the user asks an unrelated question —
  finish or abandon the pipeline first; don't leave a half-promoted
  workspace.

## Realtime visibility

Every `mcp.*` mutation broadcasts to the dashboard activity stream.
The user sees stage progression live. If you stall mid-pipeline, the
last-seen stage in the activity feed tells them where you are.
