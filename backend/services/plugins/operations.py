"""Shared reviewed activation path for human REST and agent MCP adapters."""

from functools import lru_cache
from pathlib import Path
from typing import Any

from services.plugins.dispatch import dispatch
from services.plugins.installation import PluginInstaller, approve_content
from services.plugins.lifecycle import PluginLifecycle
from services.plugins.policy import PluginPolicyStore, validate_policy
from services.plugins.package import PluginPackage


@lru_cache(maxsize=1)
def runtime_installer() -> PluginInstaller:
    from core.database import engine

    return PluginInstaller(
        PluginLifecycle(engine, Path(engine.url.database).parent / "plugins")
    )


async def activate_binding(
    identity: str,
    binding: str,
    installer: PluginInstaller,
    *,
    requested_by: str = "Jarvis",
    target_label: str | None = None,
    manual_review: str | None = None,
) -> dict[str, Any]:
    policies = PluginPolicyStore(installer.lifecycle.engine)
    policy = None

    async def approval(record: dict[str, Any]) -> bool:
        nonlocal policy
        policy = policies.get(identity)
        reviewed = (
            {
                "image": policy["image"],
                "credential_slots": sorted(policy["credentials"]),
                "network": "none",
                "read_only": True,
                "revision": policy["revision"],
            }
            if policy
            else None
        )
        if manual_review is not None:
            from services.plugins.manual_review import verify_review

            verify_review(
                manual_review,
                {
                    "id": identity,
                    "digest": record["digest"],
                    "target": binding,
                    "policy_revision": policy["revision"] if policy else "",
                },
            )
            return True
        return await approve_content(
            {
                **record,
                "target_agent": binding,
                "execution_policy": reviewed,
                "requested_by": requested_by,
                "target_label": target_label,
            }
        )

    async def apply(root: Path, package: PluginPackage, target: str) -> bool:
        return await dispatch(root, package, target, engine=installer.lifecycle.engine)

    def authorize(root: Path, package: PluginPackage) -> bool:
        nonlocal policy
        policy = policies.get(identity)
        return policy is not None and validate_policy(root, package, policy)

    return await installer.lifecycle.activate(
        identity, binding, approve=approval, apply=apply, authorize=authorize
    )
