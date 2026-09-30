"""Approved plugin control endpoint embedded in an existing team subprocess."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any
from weakref import WeakKeyDictionary

from sqlalchemy import create_engine

from services.plugins.ipc import PluginControlServer, socket_path
from services.plugins.lifecycle import PluginLifecycle, PluginStateError
from services.plugins.package import inspect_package
from services.plugins.policy import PluginPolicyStore
from services.plugins.runtime_adapter import apply_to_agent
from services.plugins.targets import team_binding

logger = logging.getLogger(__name__)
_servers: dict[str, PluginControlServer] = {}
_starting: dict[str, asyncio.Task] = {}


async def start_team_runtime(
    agent: Any, run_id: str, record: dict[str, Any], emit_event=None
) -> None:
    database = os.environ["SPAWN_REGISTRY_DB"]
    engine = create_engine("sqlite:///" + database)

    def publish(delta):
        if emit_event:
            emit_event("plugin_status", run_id, agent.name, **delta)

    lifecycle = PluginLifecycle(
        engine, Path(database).parent / "plugins", on_status=publish
    )
    policies = PluginPolicyStore(engine)
    binding = team_binding(record)

    async def handle(request: dict[str, Any]) -> dict[str, Any]:
        candidate = lifecycle.get(request["candidate"])
        expected = {
            "activate": "activating",
            "deactivate": "deactivating",
            "status": "ready",
        }[request["operation"]]
        if candidate["digest"] != request["digest"] or not any(
            item["agent"] == binding and item["status"] == expected
            for item in candidate["bindings"]
        ):
            raise PluginStateError("Package has no approved operation for this runtime")
        root = lifecycle.package_path(candidate["id"])
        package = await asyncio.to_thread(inspect_package, root)
        if package.digest != candidate["digest"]:
            raise PluginStateError("Approved package changed")
        if request["operation"] == "status":
            from services.plugins.mcp_runtime import server_names

            expected_skills = {
                package.name + ":" + skill.name for skill in package.skills
            }
            live_skills = {
                skill.name
                for skill in agent.skill_manifests
                if candidate["id"] in skill.path.parts and skill.path.is_file()
            }
            expected_servers = set(server_names(package))
            return {
                "ack": bool(expected_skills or expected_servers)
                and expected_skills <= live_skills
                and expected_servers <= set(agent.list_attached_mcp_servers())
            }
        policy = await asyncio.to_thread(policies.get, candidate["id"])
        ack = await apply_to_agent(
            agent, root, package, policy, remove=request["operation"] == "deactivate"
        )
        return {"ack": ack}

    server = PluginControlServer(socket_path(database, run_id), run_id, handle)
    try:
        await server.start()
        _servers[run_id] = server
        from services.plugins.restoration import restore_bindings

        await restore_bindings(agent, binding, lifecycle)
    except Exception:
        logger.exception("[plugins] Team capability endpoint failed run=%s", run_id)
        await server.close()
        engine.dispose()
    finally:
        _starting.pop(run_id, None)


_attached: WeakKeyDictionary[Any, str] = WeakKeyDictionary()


def attach_team_runtime(
    agent: Any, run_id: str, record: dict[str, Any], emit_event=None
) -> None:
    if run_id in _servers or run_id in _starting or _attached.get(agent) == run_id:
        return
    from fast_agent.agents.tool_runner import ToolRunnerHooks

    from services.plugins.live_runtime import register_runtime
    from services.sse_progress import merge_hooks

    register_runtime(agent)

    async def initialize() -> None:
        try:
            await start_team_runtime(agent, run_id, record, emit_event)
        except Exception:
            logger.exception(
                "[plugins] Team capability initialization failed run=%s", run_id
            )
        finally:
            _starting.pop(run_id, None)

    def launch() -> asyncio.Task:
        pending = asyncio.create_task(initialize())
        _starting[run_id] = pending
        return pending

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        task = None
    else:
        task = launch()
    _attached[agent] = run_id

    async def before_first_call(_runner, _messages) -> None:
        nonlocal task
        if task is None:
            task = launch()
        # This hook precedes the capability controller. Restore must finish
        # before the controller marks the first turn as using a tool snapshot.
        await asyncio.shield(task)

    gate = ToolRunnerHooks(before_llm_call=before_first_call)
    existing = agent.tool_runner_hooks
    agent.tool_runner_hooks = merge_hooks(gate, existing) if existing else gate
