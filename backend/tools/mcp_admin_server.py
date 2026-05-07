"""MCP Admin Tool Server — lets Jarvis curate its own MCP catalog.

Two flows are exposed as tools:

  Path A — config-only:
    Use when an MCP server already exists (an npm package, a hosted URL).
    Just register it: command/args/env/url + smoke test → done.

      mcp_create_server, mcp_update_server, mcp_delete_server,
      mcp_attach_to_agent, mcp_detach_from_agent, mcp_test_server.

  Path B — Jarvis-authored:
    Use when no off-the-shelf server fits and you want to design a new one.
    Pipeline (run in order; each tool returns the result the agent should
    inspect before proceeding):

      1. mcp_check_environment
      2. mcp_recommended_packages         (read curated allowlist)
      3. mcp_check_package_safety         (per non-recommended package)
      4. mcp_scaffold_server              (write boilerplate + manifest)
      5. mcp_static_check                 (forbidden patterns are warned)
      6. (Edit server.py via the Edit tool / filesystem MCP)
      7. mcp_install_dependencies         (per-server .venv)
      8. mcp_run_smoke_test               (protocol-level)
      9. mcp_run_tool_test                (per-tool, with assertion DSL)
     10. mcp_run_test_suite               (pytest in the server dir)
     11. mcp_verify                       (gate: ready to promote?)
     12. mcp_promote                      (generated → catalog + attach)

    For hot-fixes after promotion: mcp_patch_tool then re-run from step 5.

Every tool delegates to the main backend via the RuntimeRpcServer Unix
socket so mutations land in the live process — no restart needed.

Self-improving Jarvis is gated by the same experimental flag as
skill_server: ``experimental/SELF_IMPROVING_ENABLED``. When OFF the
tools return a clear "disabled" message instead of round-tripping.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.runtime_rpc_client import RuntimeRpcError, call as rpc_call  # noqa: E402

logger = logging.getLogger("mcp_admin_server")
mcp = FastMCP("McpAdmin")


def _is_enabled() -> bool:
    try:
        from services.config_service import config_service
        v = config_service.get("experimental", "SELF_IMPROVING_ENABLED", default="false")
        return str(v).strip().lower() in ("1", "true", "yes", "on")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[mcp_admin_server] cannot read experimental flag: %s", exc)
        return False


_DISABLED_RESPONSE = {
    "error": (
        "Self-improving Jarvis is disabled. Tell the user to enable "
        "Settings → Experimental → 'Self-improving Jarvis' (no restart needed)."
    ),
    "status": 503,
}


def _bridge_error(exc: Exception) -> dict:
    return {
        "error": (
            "Could not reach the main backend RPC bridge. The mcp_admin "
            f"subprocess is up but the runtime socket isn't responding: {exc}"
        ),
        "status": 503,
    }


def _delegate(method: str, params: dict | None = None) -> dict:
    if not _is_enabled():
        return dict(_DISABLED_RESPONSE)
    try:
        return rpc_call(method, params)
    except RuntimeRpcError as exc:
        return _bridge_error(exc)


# ── Path A: catalog ───────────────────────────────────────────────────


@mcp.tool()
def mcp_list_servers() -> dict:
    """List every MCP server in the catalog with status and attached agents.

    Returns ``{"servers": [{name, transport, status, is_builtin, attached_agents, ...}]}``.
    """
    return _delegate("mcp.list_servers")


@mcp.tool()
def mcp_get_server(name: str) -> dict:
    """Detail for one MCP server (config + attached agents)."""
    return _delegate("mcp.get_server", {"name": name})


@mcp.tool()
def mcp_create_server(
    name: str, transport: str,
    command: str | None = None, args: list[str] | None = None,
    env: dict[str, str] | None = None, url: str | None = None,
) -> dict:
    """Path A: register an existing MCP server (config only).

    Args:
        name: lowercase letters / digits / hyphens.
        transport: ``stdio`` | ``http`` | ``sse``.
        command: required for stdio (e.g. ``"npx"``).
        args: command-line args (list of strings).
        env: env vars passed to the subprocess.
        url: required for http/sse.

    Smoke-tests the config before persisting; returns `{"smoke_failed": True}` on failure.
    """
    return _delegate("mcp.create_server", {
        "name": name, "transport": transport,
        "command": command, "args": args, "env": env, "url": url,
    })


@mcp.tool()
def mcp_update_server(name: str, patch: dict[str, Any]) -> dict:
    """Update one or more fields of a saved server.

    `patch` keys: ``transport``, ``command``, ``args``, ``env``, ``url``.
    Triggers reconnect on every agent currently attached.
    """
    return _delegate("mcp.update_server", {"name": name, "patch": patch})


@mcp.tool()
def mcp_delete_server(name: str) -> dict:
    """Delete a server. Built-in servers cannot be deleted (returns 403).
    Cascade-detaches from every attached agent first.
    """
    return _delegate("mcp.delete_server", {"name": name})


@mcp.tool()
def mcp_test_server(name: str) -> dict:
    """Smoke-test a saved server and refresh its tools cache.
    Returns ``{"ok": bool, "tools": [...], "error": str|None}``.
    """
    return _delegate("mcp.test_server", {"name": name})


@mcp.tool()
def mcp_attach_to_agent(server: str, agent: str) -> dict:
    """Attach a catalog server to an agent. Idempotent.

    Returns ``{"persisted": bool, "live_attached": bool, "tools_added": [...]}``.
    """
    return _delegate("mcp.attach_to_agent", {"server": server, "agent": agent})


@mcp.tool()
def mcp_detach_from_agent(server: str, agent: str) -> dict:
    """Detach a server from an agent. Interrupts any in-flight tool call on
    that server."""
    return _delegate("mcp.detach_from_agent", {"server": server, "agent": agent})


# ── Path B: env probe + safety ────────────────────────────────────────


@mcp.tool()
def mcp_check_environment() -> dict:
    """Snapshot the host runtime: Python/Node/uv/Deno/Bun/Docker versions and
    workspace path. Run this BEFORE deciding what language/framework to use
    for a new MCP server.
    """
    return _delegate("mcp.check_environment")


@mcp.tool()
def mcp_recommended_packages() -> dict:
    """Curated allowlist of safe packages by ecosystem.

    Returns ``{"python": [{name, purpose}], "node": [...]}``. Use these
    without an extra safety check; for anything outside this list call
    ``mcp_check_package_safety`` first.
    """
    return _delegate("mcp.recommended_packages")


@mcp.tool()
def mcp_check_package_safety(package_name: str, ecosystem: str = "python") -> dict:
    """Look up a package on PyPI/npm and return a risk snapshot.

    Returns warnings (e.g. very-new package, no homepage, possible typosquat).
    NOT a hard block — read the warnings and decide.
    """
    return _delegate("mcp.check_package_safety", {
        "package_name": package_name, "ecosystem": ecosystem,
    })


# ── Path B: build pipeline ────────────────────────────────────────────


@mcp.tool()
def mcp_scaffold_server(
    name: str, description: str, planned_tools: list[dict[str, Any]] | None = None,
) -> dict:
    """Create a new generated MCP server skeleton.

    Args:
        name: lowercase letters/digits/underscores; will be the server module name.
        description: human-readable summary (used in server.py docstring).
        planned_tools: list of ``{"name", "description", "args"}`` items.
                       Each tool's description must be ≥ 10 chars (LLM context).

    Creates ``mcp_workspace/generated/{name}/`` with server.py boilerplate,
    manifest.json, requirements.txt, and an empty tests/ dir. The agent
    edits server.py afterwards via the filesystem.
    """
    return _delegate("mcp.scaffold_server", {
        "name": name, "description": description,
        "planned_tools": planned_tools or [],
    })


@mcp.tool()
def mcp_static_check(name: str) -> dict:
    """Run AST parse + lint + forbidden-pattern scan on the generated server.

    Forbidden patterns (eval/exec/os.system/shell=True/etc.) are reported
    as WARNINGS and notified to the dashboard — they do not block.
    Returns ``{"ok", "issues": [...], "warnings": [...]}``.
    """
    return _delegate("mcp.static_check", {"name": name})


@mcp.tool()
def mcp_install_dependencies(name: str) -> dict:
    """Create a per-server .venv and install requirements.txt into it (uv preferred).

    Edit ``requirements.txt`` first if needed. Each generated server gets
    its OWN venv to avoid polluting the main backend's env.
    """
    return _delegate("mcp.install_dependencies", {"name": name})


@mcp.tool()
def mcp_run_smoke_test(name: str) -> dict:
    """Spawn the generated server, MCP-initialize, list tools, disconnect.

    Verifies the server speaks MCP and exposes the planned tools. NO tool
    is actually called. Reports ``{"ok", "tools", "missing", "extra"}``.
    """
    return _delegate("mcp.run_smoke_test", {"name": name})


@mcp.tool()
def mcp_run_tool_test(
    name: str, tool_name: str,
    args: dict[str, Any] | None = None,
    assertions: list[dict[str, Any]] | None = None,
) -> dict:
    """Functional test: spawn → call_tool(tool_name, args) → evaluate assertions.

    Assertion DSL — list of:
      ``{"type": "no_error"}``                         result.isError == False
      ``{"type": "field_present", "path": "data.id"}`` JSON path exists
      ``{"type": "regex_match", "path": "text", "pattern": "..."}``
      ``{"type": "type_check", "path": "x", "expected": "str|int|list|dict|bool|float"}``
      ``{"type": "duration_under_ms", "ms": 5000}``

    Returns ``{"ok", "duration_ms", "result_preview", "passed", "failed"}``.
    """
    return _delegate("mcp.run_tool_test", {
        "name": name, "tool_name": tool_name,
        "args": args or {}, "assertions": assertions or [],
    })


@mcp.tool()
def mcp_run_test_suite(name: str) -> dict:
    """Run pytest inside the per-server .venv. Output saved to
    ``mcp_workspace/test_runs/{name}-{ts}.log``. Add pytest to
    requirements.txt before calling.
    """
    return _delegate("mcp.run_test_suite", {"name": name})


@mcp.tool()
def mcp_verify(name: str) -> dict:
    """Aggregate gate before promote. Returns ``{ready: bool, blockers: [...], warnings: [...]}``.

    Required: static_check ok + install_deps ok + smoke_test ok + every
    planned tool has at least one passing functional test.
    """
    return _delegate("mcp.verify", {"name": name})


@mcp.tool()
def mcp_promote(name: str, attach_to: list[str] | None = None) -> dict:
    """Move a verified generated server into the catalog and (optionally)
    attach to listed agents. Refuses if verify().ready is False.
    """
    return _delegate("mcp.promote", {"name": name, "attach_to": attach_to or []})


@mcp.tool()
def mcp_patch_tool(name: str, tool_name: str, new_code: str) -> dict:
    """Hot-fix one tool function. ``new_code`` must define exactly one
    top-level function with the same name as ``tool_name``.

    Decorators on the existing function are preserved. After patching,
    re-runs static_check; the agent decides which other stages to re-run.
    """
    return _delegate("mcp.patch_tool", {
        "name": name, "tool_name": tool_name, "new_code": new_code,
    })


# ── Path B: workspace introspection / cleanup ────────────────────────


@mcp.tool()
def mcp_list_generated() -> dict:
    """List every server scaffolded under workspace/generated/, with status."""
    return _delegate("mcp.list_generated")


@mcp.tool()
def mcp_get_generated(name: str) -> dict:
    """Full manifest (incl. stage history) for one generated server."""
    return _delegate("mcp.get_generated", {"name": name})


@mcp.tool()
def mcp_clean_workspace(scope: str = "test_runs") -> dict:
    """Drop runtime artifacts.

      scope='test_runs' (default) — wipe pytest log files only.
      scope='<name>'              — drop one server's directory + .venv.
      scope='all'                 — refused; must be done from the dashboard.
    """
    return _delegate("mcp.clean_workspace", {"scope": scope})


if __name__ == "__main__":
    mcp.run()
