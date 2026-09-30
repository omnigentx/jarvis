"""Restore only durable, reviewed bindings to replacement runtime instances."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from weakref import WeakKeyDictionary

from services.plugins.lifecycle import PluginLifecycle
from services.plugins.package import inspect_package
from services.plugins.policy import PluginPolicyStore

logger = logging.getLogger(__name__)
_tasks: WeakKeyDictionary[Any, asyncio.Task | None] = WeakKeyDictionary()


async def restore_bindings(
    agent: Any, binding: str, lifecycle: PluginLifecycle
) -> None:
    from services.plugins.runtime_adapter import apply_to_agent

    policies = PluginPolicyStore(lifecycle.engine)
    for candidate in await asyncio.to_thread(lifecycle.list):
        own = any(
            item["agent"] == binding and item["status"] == "ready"
            for item in candidate["bindings"]
        )
        if not own and not candidate["global_enabled"]:
            continue
        try:
            root = lifecycle.package_path(candidate["id"])
            package = await asyncio.to_thread(inspect_package, root)
            if package.digest != candidate["digest"]:
                lifecycle._status(candidate["id"], "integrity_failed", binding)
                continue
            policy = await asyncio.to_thread(policies.get, candidate["id"])
            if not own:
                if (policy["revision"] if policy else "") != candidate[
                    "global_policy_revision"
                ]:
                    raise ValueError("Global policy changed after review")
                from services.plugins.policy import validate_policy

                async def approved(_record):
                    return True

                async def apply(root, package, _binding, policy=policy):
                    return await apply_to_agent(agent, root, package, policy)

                await lifecycle.activate(
                    candidate["id"],
                    binding,
                    approve=approved,
                    apply=apply,
                    authorize=lambda root, package, policy=policy: (
                        policy is not None and validate_policy(root, package, policy)
                    ),
                )
            else:
                acknowledged = await apply_to_agent(agent, root, package, policy)
                lifecycle._status(
                    candidate["id"],
                    "ready" if acknowledged else "activation_failed",
                    binding,
                )
        except asyncio.CancelledError:
            lifecycle._status(candidate["id"], "activation_interrupted", binding)
            raise
        except Exception:  # noqa: BLE001 — runtime boundary; do not expose credential-bearing errors
            # Redact exception details, isolate one bad package from others.
            logger.warning(
                "[plugins] Restore failed candidate=%s agent=%s",
                candidate["id"],
                binding,
            )
            lifecycle._status(candidate["id"], "activation_interrupted", binding)


def attach_static_restoration(agent: Any, binding: str) -> None:
    """Gate the first model call; no timer or reload of conversation history."""
    if agent in _tasks:
        return
    from fast_agent.agents.tool_runner import ToolRunnerHooks

    from routes.plugins import get_installer
    from services.sse_progress import merge_hooks

    def launch() -> asyncio.Task:
        from services.plugins.maintenance import start_maintenance

        lifecycle = get_installer().lifecycle
        start_maintenance(lifecycle)
        return asyncio.create_task(restore_bindings(agent, binding, lifecycle))

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        task = None
    else:
        task = launch()
    _tasks[agent] = task

    async def ready(_runner, _messages) -> None:
        nonlocal task
        if task is None:
            task = launch()
            _tasks[agent] = task
        await asyncio.shield(task)

    gate = ToolRunnerHooks(before_llm_call=ready)
    existing = agent.tool_runner_hooks
    agent.tool_runner_hooks = merge_hooks(gate, existing) if existing else gate
