"""Apply common plugin capabilities to one existing runtime instance."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from services.plugins.activation import apply_skills, remove_skills
from services.plugins.lifecycle import RuntimeUncertain
from services.plugins.live_runtime import register_runtime
from services.plugins.mcp_runtime import attach_servers, detach_servers, server_names
from services.plugins.package import PluginPackage
from services.plugins.policy import validate_policy


async def apply_to_agent(
    agent: Any,
    root: Path,
    package: PluginPackage,
    policy: dict[str, Any] | None,
    *,
    remove: bool = False,
) -> bool:
    """The controller protects current tool results and next LLM snapshots."""
    app = SimpleNamespace(get_agent=lambda _name: agent)
    if package.servers and not remove:
        if policy is None:
            return False
        validate_policy(root, package, policy)

    async def mutate() -> bool:
        if not remove:
            from services.plugins.capability_budget import skill_budget_available

            if not skill_budget_available(agent, root, package):
                return False
            expected_skills = {
                package.name + ":" + skill.name for skill in package.skills
            }
            paths = {(root / skill.path).resolve() for skill in package.skills}
            present = {
                skill.name
                for skill in agent.skill_manifests
                if skill.path.resolve() in paths
            }
            expected_servers = set(server_names(package))
            if (
                (expected_skills or expected_servers)
                and expected_skills <= present
                and (not expected_skills or agent.skill_read_tool_name == "read_skill")
                and expected_servers <= set(agent.list_attached_mcp_servers())
            ):
                return True
        if remove:
            if package.skills and not await remove_skills(
                root, package, agent.name, app=app
            ):
                return False
            return await detach_servers(agent, package) if package.servers else True
        if package.servers:  # noqa: SIM102 — explicit transactional attachment/removal stages
            if not await attach_servers(
                agent,
                root,
                package,
                image=policy["image"],
                credentials=policy["credentials"],
            ):
                return False
        if package.skills:  # noqa: SIM102 — skill failure rolls MCP back
            if not await apply_skills(root, package, agent.name, app=app):
                if package.servers:  # noqa: SIM102 — explicit transactional attachment/removal stages
                    if not await detach_servers(agent, package):
                        raise RuntimeUncertain(
                            "MCP cleanup after skill failure was not acknowledged"
                        )
                return False
        return True

    return await register_runtime(agent).update(mutate)
