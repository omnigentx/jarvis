"""Live skills adapter through fast-agent's supported instruction refresh API."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from services.plugins.package import PluginPackage

logger = logging.getLogger(__name__)
_DEFAULT = object()


async def apply_skills(
    root: Path,
    package: PluginPackage,
    agent_name: str,
    *,
    app: Any = _DEFAULT,
    refresh: Callable[..., Awaitable[Any]] | None = None,
) -> bool:
    """Return true only after an actual live instruction rebuild.

    Subprocess agents are intentionally not treated as in-process instances.
    Their capability update needs a separate acknowledged IPC adapter.
    """
    from fast_agent.skills.registry import SkillRegistry

    if app is _DEFAULT:
        from services import shared_state

        app = shared_state.agent_app
    if app is None:
        return False
    try:
        agent = app.get_agent(agent_name)
    except (KeyError, ValueError, AttributeError):
        return False
    if agent is None:
        return False
    # Older runtimes implicitly enable shell when adding the first skill.
    # The supported scoped-reader API preserves current shell permissions;
    # without it, decline activation rather than grant new shell access.
    if (
        getattr(agent, "shell_runtime_enabled", None) is False
        and not getattr(agent, "_no_shell_requested", False)
        and not callable(getattr(agent, "set_skill_reader_preference", None))
    ):
        logger.warning(
            "[plugins] Skill activation would grant shell agent=%s", agent_name
        )
        return False
    if refresh is None:
        from fast_agent.core.instruction_refresh import rebuild_agent_instruction

        refresh = rebuild_agent_instruction
    previous = list(agent.skill_manifests)
    namespace = package.name + ":"
    manifests = [
        manifest for manifest in previous if not manifest.name.startswith(namespace)
    ]
    for skill in package.skills:
        path = root / skill.path
        loaded = SkillRegistry(
            directories=[path.parent], base_dir=root
        ).load_manifests()
        selected = [
            manifest for manifest in loaded if manifest.path.resolve() == path.resolve()
        ]
        if len(selected) != 1:
            return False
        manifests.append(replace(selected[0], name=namespace + skill.name))
    return await _refresh_manifests(agent, manifests, refresh)


async def _refresh_manifests(
    agent: Any, manifests: list[Any], refresh: Callable[..., Awaitable[Any]]
) -> bool:
    previous = list(agent.skill_manifests)
    previous_instruction = getattr(agent, "instruction", None)
    prefer_reader = getattr(agent, "set_skill_reader_preference", None)
    previous_reader = getattr(agent, "skill_read_tool_name", None) == "read_skill"
    try:
        if prefer_reader:
            prefer_reader(bool(manifests))
        result = await refresh(agent, skill_manifests=manifests)
        if result.rebuilt_instruction is not True:
            raise RuntimeError("Runtime did not rebuild instruction")
        return True
    except BaseException as exc:
        # The upstream refresh can update manifests before a later step fails.
        # Restore through public setters; never monkey-patch the agent methods.
        if prefer_reader:
            prefer_reader(previous_reader)
        if hasattr(agent, "set_skill_manifests"):
            agent.set_skill_manifests(previous)
        if previous_instruction is not None and hasattr(agent, "set_instruction"):
            agent.set_instruction(previous_instruction)
        logger.warning("[plugins] Skill refresh did not complete")
        if isinstance(exc, Exception):
            return False
        raise


async def remove_skills(
    root: Path,
    package: PluginPackage,
    agent_name: str,
    *,
    app: Any = _DEFAULT,
    refresh: Callable[..., Awaitable[Any]] | None = None,
) -> bool:
    """Acknowledge namespace removal after an actual runtime rebuild."""
    if app is _DEFAULT:
        from services import shared_state

        app = shared_state.agent_app
    if app is None:
        return False
    try:
        agent = app.get_agent(agent_name)
    except (KeyError, ValueError, AttributeError):
        return False
    if agent is None:
        return False
    if refresh is None:
        from fast_agent.core.instruction_refresh import rebuild_agent_instruction

        refresh = rebuild_agent_instruction
    namespace = package.name + ":"
    paths = {(root / skill.path).resolve() for skill in package.skills}
    manifests = [
        manifest
        for manifest in agent.skill_manifests
        if not (
            manifest.name.startswith(namespace)
            and getattr(manifest, "path", None) is not None
            and manifest.path.resolve() in paths
        )
    ]
    return await _refresh_manifests(agent, manifests, refresh)
