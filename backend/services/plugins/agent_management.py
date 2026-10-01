"""Bound agent identity and persisted hierarchy govern scoped plugin requests.

Shell is trusted under this deployment's stated trust model. MCP arguments may
select a target; they never select the caller or confer management authority.
"""

from typing import Any

from services.plugins.installation import approve_source
from services.plugins.operations import activate_binding, runtime_installer
from services.plugins.targets import resolve_target, runtime_candidate


def static_agents() -> dict:
    from agent import fast

    return fast.agents


def team_records() -> dict:
    from services import shared_state

    return shared_state.registry_db.get_all() if shared_state.registry_db else {}


def team_template(session_id: str) -> dict:
    from services.team_template_service import get_template

    return get_template(session_id)


def authorized_target(
    caller_agent: str, session_id: str, target_agent: str, run_id: str | None
) -> tuple[str, str | None]:
    if not isinstance(caller_agent, str) or not caller_agent or len(caller_agent) > 128:
        raise PermissionError("Missing bound caller identity")
    if not isinstance(session_id, str) or "${" in session_id or len(session_id) > 128:
        raise PermissionError("Invalid bound team session")
    name = target_agent or caller_agent
    if not isinstance(name, str) or not name or len(name) > 128:
        raise PermissionError("Invalid target agent")
    if not session_id:
        definitions = static_agents()
        caller = definitions.get(caller_agent)
        if not caller or "plugin_management" not in (
            getattr(caller.get("config"), "servers", None) or []
        ):
            raise PermissionError("Caller lacks plugin-management capability")
        if name != caller_agent and caller_agent != "Jarvis":
            raise PermissionError("Only Jarvis may manage another in-process agent")
        if run_id:
            record = team_records().get(run_id)
            if (
                caller_agent != "Jarvis"
                or not record
                or record.get("agent_name") != name
                or not runtime_candidate(record)
            ):
                raise PermissionError("Target is not a managed live team run")
        elif name not in definitions:
            raise PermissionError("Select an explicit managed team run")
        return name, run_id

    records = [
        r
        for r in team_records().values()
        if r.get("session_id") == session_id and runtime_candidate(r)
    ]
    callers = [r for r in records if r.get("agent_name") == caller_agent]
    targets = [
        r
        for r in records
        if r.get("agent_name") == name and (not run_id or r.get("run_id") == run_id)
    ]
    if len(callers) != 1 or len(targets) != 1:
        raise PermissionError("Caller or target team runtime is absent or ambiguous")
    template = team_template(session_id)
    roles = template.get("roles") or {}
    role, target_role = callers[0].get("role"), targets[0].get("role")
    if (
        role not in roles
        or target_role not in roles
        or "plugin_management" not in (roles[role].get("servers") or [])
    ):
        raise PermissionError("Caller lacks persisted plugin-management capability")
    if name != caller_agent and (
        role != template.get("orchestrator") or target_role == role
    ):
        raise PermissionError("Only the team orchestrator may manage a subordinate")
    return name, targets[0]["run_id"]


def summary(record: dict[str, Any], binding: str | None = None) -> dict[str, Any]:
    """Bounded metadata, never package bodies, environment or policy secrets."""
    keys = (
        "id",
        "name",
        "version",
        "repo",
        "commit",
        "subdirectory",
        "digest",
        "status",
        "bindings",
    )
    result = {key: record[key] for key in keys if key in record}
    result["skills"] = [skill["name"] for skill in record.get("skills", [])]
    result["server_names"] = list(record.get("servers", {}))
    result["blockers"] = record.get("blockers", [])
    if binding is not None:
        result["bindings"] = [
            item for item in record.get("bindings", []) if item["agent"] == binding
        ]
    return result


async def inventory(*, caller_agent: str, session_id: str = "") -> dict:
    name, selected = authorized_target(caller_agent, session_id, "", None)
    binding = resolve_target(name, selected)
    import asyncio

    records = await asyncio.to_thread(runtime_installer().lifecycle.list)
    plugins = []
    for record in records:
        item = summary(record, binding)
        item["stored_status"] = item.pop("status", "unknown")
        item["bindings"] = [
            {"agent": destination["agent"], "stored_status": destination["status"]}
            for destination in item["bindings"]
        ]
        plugins.append(item)
    return {
        "plugins": plugins,
        "availability": "Activate to verify the live runtime ACK",
    }


async def activate(
    *,
    caller_agent: str,
    identity: str,
    session_id: str = "",
    target_agent: str = "",
    run_id: str | None = None,
) -> dict:
    name, selected = authorized_target(caller_agent, session_id, target_agent, run_id)
    installer = runtime_installer()
    binding = resolve_target(name, selected)
    result = await activate_binding(
        identity, binding, installer, requested_by=caller_agent, target_label=name
    )
    return {**summary(result, binding), "target_agent": name, "run_id": selected}


async def add(
    *,
    caller_agent: str,
    repo: str,
    commit: str,
    subdirectory: str = "",
    session_id: str = "",
    target_agent: str = "",
    run_id: str | None = None,
) -> dict:
    name, selected = authorized_target(caller_agent, session_id, target_agent, run_id)
    installer = runtime_installer()

    async def review_source(record: dict[str, Any]) -> bool:
        return await approve_source(record, requested_by=caller_agent)

    result = await installer.install(repo, commit, subdirectory, approve=review_source)
    if "id" not in result or result.get("status") in {"rejected", "expired"}:
        return summary(result)
    binding = resolve_target(name, selected)
    result = await activate_binding(
        result["id"], binding, installer, requested_by=caller_agent, target_label=name
    )
    return {**summary(result, binding), "target_agent": name, "run_id": selected}
