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
