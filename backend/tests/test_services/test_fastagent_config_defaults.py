"""Regression tests for fastagent.config.yaml MCP server defaults.

Incident 2026-05-17 (Designer "không thấy filesystem MCP"):

  ``fastagent.config.yaml`` declared the ``filesystem`` MCP server with
  default args ``["-y", "@modelcontextprotocol/server-filesystem", "./data",
  "./jarvis_workspace"]``. The second path was never created on disk →
  ``@modelcontextprotocol/server-filesystem`` validates every allowed-roots
  arg at startup and refuses to launch if any is missing → the MCP
  subprocess exited before completing the handshake → fast-agent silently
  dropped the server from the agent's tool list.

  This bit only after the auto-resume cascade (Bug B/D) dropped per-role
  ``server_overrides``, causing the agent to fall back to these base
  defaults instead of the team workspace path.

This test pins the contract: every path-shaped arg in a server's default
args MUST resolve to a directory that exists on disk. If a future edit
re-introduces a phantom path, this test fails BEFORE it lands in prod.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml


BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
CONFIG_FILE = BACKEND_DIR / "fastagent.config.yaml"


def _looks_like_path(arg: str) -> bool:
    """Heuristic: is this CLI arg a path we should validate?

    Skips:
      - flags (``-y``, ``--whatever``)
      - npm/pypi package specifiers (``@scope/name``, ``name@version``)
      - placeholders that fast-agent substitutes at spawn time
        (``{workspace_dir}``, ``{project_dir}/...``)
      - bare module/command names (no slash, no leading dot)
    """
    if not isinstance(arg, str) or not arg:
        return False
    if arg.startswith("-"):
        return False
    if arg.startswith("@") and "/" in arg:  # @modelcontextprotocol/server-filesystem
        return False
    if "{" in arg and "}" in arg:  # contains template placeholder
        return False
    # Treat as path if it starts with ".", "/", or "~" or contains a slash
    return arg.startswith((".", "/", "~")) or "/" in arg


@pytest.fixture(scope="module")
def config() -> dict:
    with CONFIG_FILE.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.mark.parametrize("server_name", [
    "filesystem",
    "time-service",
    "gmail",
    "calendar",
    "media-server",
    "story-server",
    "library-server",
    "iot-control",
    "cron-server",
    "approval-server",
    "skill_server",
    "mcp_admin",
])
def test_default_server_args_all_paths_exist(config, server_name):
    """Every relative/absolute path in a server's default args must exist.

    Per-role ``server_overrides`` (e.g. agile_team.yaml's filesystem
    override) can REPLACE these args, but only if the override actually
    propagates. When the cascade drops ``server_overrides`` (Bug B/D),
    these defaults are what the MCP subprocess sees — so they MUST be
    valid out of the box.
    """
    servers = config.get("mcp", {}).get("servers", {})
    server = servers.get(server_name)
    if not server:
        pytest.skip(f"server '{server_name}' not declared in this config")

    args = server.get("args") or []
    path_args = [a for a in args if _looks_like_path(a)]

    for path_arg in path_args:
        # Resolve relative to backend dir (the cwd fast-agent runs from)
        if path_arg.startswith("./"):
            resolved = BACKEND_DIR / path_arg[2:]
        elif path_arg.startswith("/"):
            resolved = Path(path_arg)
        elif path_arg.startswith("~"):
            resolved = Path(path_arg).expanduser()
        else:
            resolved = BACKEND_DIR / path_arg

        assert resolved.exists(), (
            f"fastagent.config.yaml server '{server_name}' has default arg "
            f"'{path_arg}' resolving to {resolved} which does NOT exist. "
            f"When a spawn falls back to these defaults (e.g. cascade drops "
            f"server_overrides), the MCP subprocess will fail to start and "
            f"the agent will silently lose the tool. Either remove the arg, "
            f"point it at an existing directory, or create the directory at "
            f"backend boot."
        )


def test_filesystem_default_does_not_include_broken_jarvis_workspace(config):
    """Specific regression: the 2026-05-17 Designer incident root cause.

    Pin the exact filesystem default to NOT include the phantom
    ``./jarvis_workspace`` path. If someone re-adds it (for whatever
    well-intentioned reason), this test catches it before deploy.
    """
    fs = config.get("mcp", {}).get("servers", {}).get("filesystem", {})
    args = fs.get("args") or []
    assert "./jarvis_workspace" not in args, (
        "filesystem default args MUST NOT include './jarvis_workspace' — "
        "the directory is never created and breaks MCP startup. Use a "
        "per-role server_overrides in team_templates/agile_team.yaml to "
        "point filesystem at the actual team workspace at spawn time."
    )
