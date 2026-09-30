"""Dispatch an approved operation to one live runtime without replacing it."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.engine import Engine

from services.plugins.ipc import request_update, socket_path
from services.plugins.lifecycle import PluginStateError
from services.plugins.package import PluginPackage
from services.plugins.policy import PluginPolicyStore
from services.plugins.runtime_adapter import apply_to_agent
from services.plugins.targets import confirmed_team_processes_absent, live_team_run


async def dispatch(
    root: Path,
    package: PluginPackage,
    binding: str,
    *,
    engine: Engine,
    remove: bool = False,
) -> bool:
    if binding.startswith("team:"):
        try:
            record = live_team_run(binding)
        except PluginStateError:
            if remove and confirmed_team_processes_absent(binding):
                return True
            raise
        database = str(engine.url.database)
        try:
            return await request_update(
                socket_path(database, record["run_id"]),
                run_id=record["run_id"],
                candidate=root.parent.name,
                digest=package.digest,
                operation="deactivate" if remove else "activate",
            )
        except (ConnectionRefusedError, FileNotFoundError):
            if remove and confirmed_team_processes_absent(binding):
                return True
            raise
    from services import shared_state

    if shared_state.agent_app is None:
        return False
    agent = shared_state.agent_app.get_agent(binding)
    if agent is None:
        return False
    policy = PluginPolicyStore(engine).get(root.parent.name)
    return await apply_to_agent(agent, root, package, policy, remove=remove)
