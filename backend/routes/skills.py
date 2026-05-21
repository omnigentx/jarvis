"""Skill management API — ``/api/skills/*``.

Lets the dashboard CRUD skills shipped under ``.fast-agent/skills``. The route
is a thin shell around :mod:`services.skill_service` which holds the
validation, atomic-IO, optimistic-locking and built-in protection logic.

Auth uses the same ``verify_api_key`` dependency as the rest of the dashboard
APIs. Every error path raises :class:`SkillValidationError`, which we map to a
deterministic HTTP status code.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.auth import verify_api_key
from services import skill_service as svc

logger = logging.getLogger("skills_api")
router = APIRouter(prefix="/api/skills", tags=["skills"])


# ----- Bodies -------------------------------------------------------------


class CreateBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    content: str = Field(..., max_length=svc.MAX_BYTES)


class UpdateBody(BaseModel):
    content: str = Field(..., max_length=svc.MAX_BYTES)
    # mtime_ns is a 19-digit nanosecond timestamp — overflows
    # Number.MAX_SAFE_INTEGER (2^53) on the JS side. Accept either an int
    # (legacy clients / curl) or a string (the dashboard sends a string to
    # preserve precision through JSON.parse).
    expected_mtime_ns: Optional[int | str] = None


# ----- Helpers ------------------------------------------------------------


def _raise(exc: svc.SkillValidationError) -> None:
    detail = {"message": exc.message}
    if exc.detail:
        detail.update(exc.detail)
    raise HTTPException(status_code=exc.status_code, detail=detail)


def _to_summary_dict(s: svc.SkillSummary) -> dict:
    return {
        "name": s.name,
        "description": s.description,
        "is_builtin": s.is_builtin,
        "used_by": s.used_by,
        # Stringify nanoseconds: a 19-digit int (e.g. 1762243200000000000)
        # exceeds Number.MAX_SAFE_INTEGER, so transit as JSON number would
        # lose precision in the browser and break optimistic-locking
        # comparisons (silent 409 on every save).
        "mtime_ns": str(s.mtime_ns),
        "parse_error": s.parse_error,
    }


def _to_detail_dict(s: svc.SkillDetail) -> dict:
    d = _to_summary_dict(s)
    d["content"] = s.content
    return d


# ----- Routes -------------------------------------------------------------


@router.get("", dependencies=[Depends(verify_api_key)])
async def list_skills_route():
    return {"skills": [_to_summary_dict(s) for s in svc.list_skills()]}


@router.get("/_template", dependencies=[Depends(verify_api_key)])
async def get_template():
    return {"content": svc.render_template()}


@router.get("/{name}", dependencies=[Depends(verify_api_key)])
async def get_skill_route(name: str):
    try:
        return _to_detail_dict(svc.get_skill(name))
    except svc.SkillValidationError as exc:
        _raise(exc)


@router.post("", dependencies=[Depends(verify_api_key)], status_code=201)
async def create_skill_route(body: CreateBody):
    try:
        return _to_detail_dict(svc.create_skill(body.name, body.content))
    except svc.SkillValidationError as exc:
        _raise(exc)


@router.put("/{name}", dependencies=[Depends(verify_api_key)])
async def update_skill_route(name: str, body: UpdateBody):
    expected: Optional[int]
    if body.expected_mtime_ns is None:
        expected = None
    else:
        try:
            expected = int(body.expected_mtime_ns)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail={"message": "expected_mtime_ns must be an integer or numeric string."},
            )
    try:
        return _to_detail_dict(await svc.update_skill(name, body.content, expected))
    except svc.SkillValidationError as exc:
        _raise(exc)


@router.delete("/{name}", dependencies=[Depends(verify_api_key)])
async def delete_skill_route(name: str):
    try:
        return await svc.delete_skill(name)
    except svc.SkillValidationError as exc:
        _raise(exc)


# ----- Attach / detach -----------------------------------------------------
# PUT/DELETE on the relationship resource. Idempotent verbs match the
# "set membership" semantics — calling attach twice is a 409 from the service,
# not a state corruption.


@router.put(
    "/{name}/agents/{agent}",
    dependencies=[Depends(verify_api_key)],
)
async def attach_skill_route(name: str, agent: str):
    try:
        return await svc.attach_skill_to_agent(agent, name)
    except svc.SkillValidationError as exc:
        _raise(exc)


@router.delete(
    "/{name}/agents/{agent}",
    dependencies=[Depends(verify_api_key)],
)
async def detach_skill_route(name: str, agent: str):
    try:
        return await svc.detach_skill_from_agent(agent, name)
    except svc.SkillValidationError as exc:
        _raise(exc)


# ----- Reload (blast radius + force-kill respawn) -------------------------
#
# Skills are file-based; agents read them at spawn time. After ``PUT /api/skills/{name}``
# the content on disk is updated but every alive agent still holds the
# pre-edit version in memory. These two endpoints give the dashboard:
#
#   GET  /{name}/reload-preview  → who would be affected, no side-effects
#   POST /{name}/reload          → force-kill + respawn (requires confirm)
#
# Per user decision 2026-05-17: no wait-for-idle; explicit confirm dialog.
# Cross-team operation: one skill is typically referenced by many roles in
# many sessions (e.g. team-communication). We reload across all of them.


class SkillReloadBody(BaseModel):
    confirm: bool = Field(False, description="MUST be true; UI shows blast-radius warning before set.")
    inject_message: str | None = Field(
        None,
        description="Override the default sentinel sent to each respawned agent.",
    )


@router.get(
    "/{name}/reload-preview",
    dependencies=[Depends(verify_api_key)],
)
async def reload_preview(name: str):
    """Show which teams + roles + agents would be force-killed by /reload.

    Pure read — no kills, no respawn. UI calls this to render the
    confirmation dialog ("This will restart 7 agents across 2 teams").
    """
    from services.team_reload import find_sessions_using_skill

    sessions = find_sessions_using_skill(name)
    agent_count = sum(len(s["roles"]) for s in sessions)  # rough — 1 agent per role
    return {
        "skill": name,
        "sessions": sessions,
        "session_count": len(sessions),
        "approx_agent_count": agent_count,
        "warning": (
            "Reloading will SIGKILL every running agent that has this skill "
            "and respawn them mid-task. In-flight tool calls will abort."
            if agent_count > 0
            else "No alive agents use this skill — reload is a no-op."
        ),
    }


@router.post(
    "/{name}/reload",
    dependencies=[Depends(verify_api_key)],
)
async def reload_skill(name: str, body: SkillReloadBody):
    """Force-kill + respawn every agent using ``name``.

    Requires ``confirm: true``. Returns per-session results so the UI can
    show which agents successfully respawned vs. errored.
    """
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail=(
                "destructive operation requires {confirm: true}. Call "
                f"GET /api/skills/{name}/reload-preview first to compute "
                "blast radius."
            ),
        )

    from services.team_reload import reload_by_skill

    out = await reload_by_skill(name, edited_by="system", inject_message=body.inject_message)
    return out
