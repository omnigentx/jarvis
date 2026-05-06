"""RPC handlers for skill management — registered with RuntimeRpcServer.

Each handler runs in the main backend process so it can mutate the live
``state.agent_app`` (via ``skill_service``) and trigger
``rebuild_agent_instruction`` synchronously inside the active event loop.

Wire-format note: handlers receive only JSON-friendly types and return
JSON-friendly dicts. Service-level dataclasses (``SkillSummary`` /
``SkillDetail``) get unpacked into plain dicts here so the
``RuntimeRpcServer`` can serialise them.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from core.database import NotificationModel, get_db_session
from services import skill_service as svc
from services.runtime_rpc import RuntimeRpcServer

logger = logging.getLogger("skill_rpc")


# ----- Serialisers --------------------------------------------------------


def _summary(s: svc.SkillSummary) -> dict:
    return {
        "name": s.name,
        "description": s.description,
        "is_builtin": s.is_builtin,
        "used_by": s.used_by,
        "parse_error": s.parse_error,
    }


def _detail(d: svc.SkillDetail) -> dict:
    base = _summary(d)
    base["content"] = d.content
    return base


def _err(exc: svc.SkillValidationError) -> dict:
    return {"error": exc.message, "status": exc.status_code}


def _push_notification(title: str, preview: str, content: str, *, kind: str) -> None:
    """Best-effort notification — write directly to the shared SQLite. We
    do this here (RPC handler) instead of inside skill_service so
    dashboard-initiated edits via ``/api/skills/...`` don't fire
    duplicate notifications. Only Jarvis self-improvement → notification.
    """
    try:
        with get_db_session() as db:
            db.add(NotificationModel(
                type="agent_result",
                title=title,
                preview=preview[:200],
                content=content,
                content_type="markdown",
                is_read=0,
                created_at=datetime.now().timestamp(),
                metadata_json=json.dumps({
                    "agent": "Jarvis",
                    "exec_mode": "self_improvement",
                    "kind": kind,
                }),
            ))
            db.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[skill_rpc] notification push failed: %s", exc)


# ----- Read handlers (sync) ----------------------------------------------


def skill_list() -> dict:
    return {"skills": [_summary(s) for s in svc.list_skills()]}


def skill_get(*, name: str) -> dict:
    try:
        return _detail(svc.get_skill(name))
    except svc.SkillValidationError as exc:
        return _err(exc)


# ----- Mutating handlers (mix sync/async) --------------------------------


def skill_create(*, name: str, description: str, body: str) -> dict:
    content = (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n\n"
        f"{body}\n"
    )
    try:
        result = svc.create_skill(name, content)
    except svc.SkillValidationError as exc:
        return _err(exc)
    _push_notification(
        title=f"Jarvis created skill: {name}",
        preview=description,
        content=(
            f"**Skill `{name}` created by Jarvis.**\n\n_{description}_\n\n"
            "Review the body in the Skills library and edit if needed.\n\n---\n\n"
            f"{body}"
        ),
        kind="skill_create",
    )
    return {"created": True, "name": result.name, "is_builtin": result.is_builtin}


async def skill_update(*, name: str, content: str) -> dict:
    try:
        result = await svc.update_skill(name, content, expected_mtime_ns=None)
    except svc.SkillValidationError as exc:
        return _err(exc)
    _push_notification(
        title=f"Jarvis updated skill: {name}",
        preview=result.description or "",
        content=f"**Skill `{name}` updated by Jarvis.**\n\nReview the new content in the Skills library.",
        kind="skill_update",
    )
    return {"updated": True, "name": result.name}


async def skill_delete(*, name: str) -> dict:
    try:
        result = await svc.delete_skill(name)
    except svc.SkillValidationError as exc:
        return _err(exc)
    removed = result.get("removed_from_agents") or []
    _push_notification(
        title=f"Jarvis deleted skill: {name}",
        preview=f"Removed from {len(removed)} agent(s)",
        content=(
            f"**Skill `{name}` deleted by Jarvis.**\n\n"
            f"Removed from agents: {', '.join(removed) or '(none)'}"
        ),
        kind="skill_delete",
    )
    return result


async def skill_attach(*, skill: str, agent: str) -> dict:
    try:
        result = await svc.attach_skill_to_agent(agent, skill)
    except svc.SkillValidationError as exc:
        return _err(exc)
    persisted = bool(result.get("persisted"))
    _push_notification(
        title=f"Jarvis attached '{skill}' to {agent}",
        preview="persisted to agent card" if persisted else "runtime only — restart will revert",
        content=(
            f"**Jarvis attached skill `{skill}` to `{agent}`.**\n\n"
            + ("Persisted to agent card — survives restart." if persisted
               else "Runtime-only change — reverts on backend restart unless you edit agent.py.")
        ),
        kind="skill_attach",
    )
    return result


async def skill_detach(*, skill: str, agent: str) -> dict:
    try:
        result = await svc.detach_skill_from_agent(agent, skill)
    except svc.SkillValidationError as exc:
        return _err(exc)
    _push_notification(
        title=f"Jarvis detached '{skill}' from {agent}",
        preview="",
        content=f"**Jarvis detached skill `{skill}` from `{agent}`.**",
        kind="skill_detach",
    )
    return result


# ----- Registration --------------------------------------------------------


_METHODS: dict[str, Any] = {
    "skill.list": skill_list,
    "skill.get": skill_get,
    "skill.create": skill_create,
    "skill.update": skill_update,
    "skill.delete": skill_delete,
    "skill.attach": skill_attach,
    "skill.detach": skill_detach,
}


def register(server: RuntimeRpcServer) -> None:
    """Wire every skill handler onto the given RPC server. Call once at
    boot from server.py.
    """
    for name, handler in _METHODS.items():
        server.register(name, handler)
    logger.info("[skill_rpc] registered %d skill methods", len(_METHODS))
