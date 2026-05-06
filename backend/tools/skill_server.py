"""Skill MCP Tool Server — lets Jarvis curate its own skill library.

This subprocess hosts the MCP tools (so any agent declaring
``servers=["skill_server"]`` discovers them automatically). Every tool is
a thin shim that forwards to the main backend's RuntimeRpcServer over a
Unix domain socket — see ``services/runtime_rpc.py``. Running the actual
mutation in the live backend process is the whole point of this design:
``rebuild_agent_instruction`` updates the running Jarvis instance, the
notification rows go to the live SQLite session, and the result is
visible on the next LLM turn without restart.

The experimental flag ``experimental/SELF_IMPROVING_ENABLED`` is checked
locally on every call (DB read, cheap) so toggling OFF skips the RPC
round-trip entirely and surfaces a clear "disabled" message immediately.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# Allow imports from backend/.
sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.runtime_rpc_client import RuntimeRpcError, call as rpc_call  # noqa: E402

logger = logging.getLogger("skill_server")
mcp = FastMCP("SkillService")


def _is_enabled() -> bool:
    """Read the experimental flag fresh on every call so the Settings
    toggle hot-reloads without restart.
    """
    try:
        from services.config_service import config_service
        v = config_service.get("experimental", "SELF_IMPROVING_ENABLED", default="false")
        return str(v).strip().lower() in ("1", "true", "yes", "on")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[skill_server] could not read experimental flag: %s", exc)
        return False


_DISABLED_RESPONSE = {
    "error": (
        "Self-improving Jarvis is disabled. Tell the user to enable it in "
        "Settings → Experimental → 'Self-improving Jarvis'. The toggle takes "
        "effect immediately — no restart required."
    ),
    "status": 503,
}


def _bridge_error(exc: Exception) -> dict:
    return {
        "error": (
            "Could not reach the main backend's RPC bridge. The "
            "Self-improving Jarvis subprocess is up but the runtime socket "
            f"isn't responding: {exc}"
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


# ----- Read tools --------------------------------------------------------


@mcp.tool()
def skill_list() -> dict:
    """Return every skill in the library with metadata.

    Returns:
        ``{"skills": [{"name", "description", "is_builtin", "used_by", "parse_error"}, ...]}``
    """
    return _delegate("skill.list")


@mcp.tool()
def skill_get(name: str) -> dict:
    """Return a single skill including its full body content.

    Args:
        name: Skill directory name (lowercase-kebab-case).
    """
    return _delegate("skill.get", {"name": name})


# ----- Mutating tools ----------------------------------------------------


@mcp.tool()
def skill_create(name: str, description: str, body: str) -> dict:
    """Create a brand-new skill on disk.

    Becomes available in the library immediately. To make it active for
    an agent, follow up with ``skill_attach``.

    Args:
        name: Lowercase-kebab-case (e.g. "analyzing-logs"). 1-64 chars.
        description: One-line trigger summary — when this skill should fire.
        body: Markdown body. Frontmatter (name + description) is generated;
              pass only the markdown content below the frontmatter block.
    """
    return _delegate("skill.create", {"name": name, "description": description, "body": body})


@mcp.tool()
def skill_update(name: str, content: str) -> dict:
    """Replace a skill's full SKILL.md content.

    The frontmatter ``name`` field must still match the skill's directory.
    Built-in skills can be edited; the new body and description take
    effect on the next agent turn (the backend rebuilds each agent's
    instruction automatically).

    Args:
        name: Existing skill name.
        content: Full SKILL.md text (frontmatter block + markdown body).
    """
    return _delegate("skill.update", {"name": name, "content": content})


@mcp.tool()
def skill_delete(name: str) -> dict:
    """Delete a user-created skill. Built-in skills cannot be deleted.

    Removes the skill directory plus any references in agent cards.
    """
    return _delegate("skill.delete", {"name": name})


@mcp.tool()
def skill_attach(skill: str, agent: str) -> dict:
    """Attach a skill to an agent so it appears in that agent's prompt next turn.

    Card-based agents persist the change to YAML; code-based agents
    (e.g. Jarvis itself) carry the change at runtime only — it reverts
    when the backend restarts unless the user edits agent.py.

    Args:
        skill: Skill name to attach.
        agent: Target agent name (use "Jarvis" to attach to self).
    """
    return _delegate("skill.attach", {"skill": skill, "agent": agent})


@mcp.tool()
def skill_detach(skill: str, agent: str) -> dict:
    """Detach a skill from an agent (symmetric to ``skill_attach``)."""
    return _delegate("skill.detach", {"skill": skill, "agent": agent})


if __name__ == "__main__":
    mcp.run()
