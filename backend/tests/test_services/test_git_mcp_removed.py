"""Regression guard: the `git` MCP server stays removed.

We dropped the built-in `git` MCP server (`uvx mcp-server-git`) because it
is redundant — agents have shell `execute`, the `github` MCP server, and the
Docker image (`Dockerfile.base`) ships both `git` and `gh` so agents shell out
for git/PR/CI ops. See PR #71.

`seed_from_yaml` only inserts and never deletes, and built-in catalog rows
can't be removed via the UI, so the ONLY thing keeping `git` out of fresh
installs is its absence from these two source files. Without a guard, a
well-intentioned future edit could re-add it and nothing would go red.

These tests parse the REAL config + template (not synthetic fixtures) and
fail BEFORE a re-add lands.
"""
from __future__ import annotations

from pathlib import Path

import yaml

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
CONFIG_FILE = BACKEND_DIR / "fastagent.config.yaml"
AGILE_TEAM_FILE = BACKEND_DIR / "team_templates" / "agile_team.yaml"


def test_git_mcp_server_absent_from_seed_catalog():
    """The real fastagent.config.yaml must not declare a `git` MCP server.

    `github` and `gitnexus` are the legitimate git-adjacent servers and MUST
    remain — asserting their presence also proves this test is reading a
    populated config, not an empty/misparsed one.
    """
    config = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
    servers = config.get("mcp", {}).get("servers", {})

    assert "git" not in servers, (
        "fastagent.config.yaml re-introduced the `git` MCP server. It was "
        "removed in PR #71 — agents use `git`/`gh` CLI via the execute tool "
        "instead. If you genuinely need it back, update this guard too."
    )
    assert {"github", "gitnexus"} <= set(servers), (
        "expected `github` and `gitnexus` MCP servers in the catalog — their "
        "absence means this test is reading the wrong/empty config."
    )


def test_no_agile_team_role_uses_git_mcp_server():
    """No role in the real agile_team.yaml may list the bare `git` server.

    Distinct from `github`/`gitnexus`: this checks the exact token `git` in
    each role's `servers` list, mirroring how fast-agent resolves server names.
    """
    template = yaml.safe_load(AGILE_TEAM_FILE.read_text(encoding="utf-8"))
    roles = template.get("roles", {})
    assert roles, "agile_team.yaml has no roles — wrong/empty template parsed."

    offenders = [name for name, role in roles.items() if "git" in (role.get("servers") or [])]
    assert not offenders, (
        f"agile_team.yaml roles {offenders} list the `git` MCP server, removed "
        f"in PR #71. Roles use `git`/`gh` CLI via the execute tool instead. "
        f"Also drop any `server_overrides.git` block for these roles."
    )
