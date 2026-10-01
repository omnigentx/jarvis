"""Host capability budgets bound advertised context across all plugin versions."""

from pathlib import Path
from typing import Any

from services.plugins.package import PluginPackage


def skill_budget_available(agent: Any, root: Path, package: PluginPackage) -> bool:
    namespace = package.name + ":"
    current = [
        skill for skill in agent.skill_manifests if not skill.name.startswith(namespace)
    ]
    descriptors = [
        (skill.name, skill.description, str(skill.path)) for skill in current
    ]
    descriptors += [
        (namespace + skill.name, skill.description, str(root / skill.path))
        for skill in package.skills
    ]
    return (
        len(descriptors) <= 64
        and sum(len("\n".join(item).encode()) for item in descriptors) <= 32768
    )
