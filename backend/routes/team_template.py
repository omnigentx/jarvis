"""Team-session template REST API.

Endpoints (all gated by ``verify_api_key``, all under
``/api/team-sessions/{session_id}/``):

  GET    /template                          → current template (fresh DB read)
  PATCH  /template/roles/{role}             → edit one role's config; audits
  GET    /template/history                  → audit log (newest first)
  POST   /template/rollback/{audit_id}      → revert one audit row
  POST   /template/reset/{role}             → reset role from yaml factory
  POST   /reload                            → force-kill + respawn given roles

Phase 1 (per user decisions 2026-05-17):
  - YAML is factory default; UI edits NOT persisted to yaml automatically.
    Warning to caller: edits are lost if the team is reset or recreated
    without committing the equivalent change to ``team_templates/*.yaml``.
  - Reload is force-kill-and-respawn (no wait-for-idle); explicit ``confirm``
    flag on /reload guards against accidental clicks.
  - Versioning from row 1; every PATCH writes an audit row; rollback writes
    a NEW row referencing the original (never deletes).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core.auth import verify_api_key
from core.database import get_db
from services import team_template_service as svc
from services.team_reload import reload_roles

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/team-sessions", tags=["team-template"])

# Yaml templates dir — used by /reset/{role} to look up the factory default.
_YAML_DIR = Path(__file__).resolve().parent.parent / "team_templates"


def _resolve_yaml_for_session(session_id: str) -> Path:
    """Locate the yaml template for a team session.

    Reads ``team_sessions.template.name`` to find which yaml the team was
    created from (e.g. 'agile-team' → 'agile_team.yaml').
    """
    template = svc.get_template(session_id)
    name = template.get("name") or "agile-team"
    candidate = _YAML_DIR / f"{name.replace('-', '_')}.yaml"
    if not candidate.exists():
        # Fallback: try literal name
        candidate = _YAML_DIR / f"{name}.yaml"
    return candidate


class PatchRoleBody(BaseModel):
    """Body for PATCH /template/roles/{role}.

    Caller supplies only the fields they want to change. Unknown fields are
    rejected by ``svc.validate_patch`` — keeps the API surface minimal.
    """
    patch: dict[str, Any] = Field(..., description="Partial role config, keys subset of svc.ALLOWED_ROLE_FIELDS")
    comment: str = Field("", description="Free-text audit comment; shown in history UI")


class ReloadBody(BaseModel):
    """Body for POST /reload — guarded with explicit confirm flag."""
    roles: list[str] = Field(..., description="Role keys to force-kill + respawn")
    confirm: bool = Field(False, description="MUST be true. UI shows a warning before setting.")
    inject_message: str | None = Field(
        None,
        description="Override the default sentinel message sent to each respawned agent.",
    )


class RollbackBody(BaseModel):
    comment: str = Field("", description="Why rolling back")


class ResetBody(BaseModel):
    comment: str = Field("", description="Why resetting to yaml factory")


@router.get(
    "",
    dependencies=[Depends(verify_api_key)],
)
async def list_sessions():
    """Lightweight enumeration of every team session.

    Returns ``{"sessions": [{session_id, team_name, template_name, agents_count}]}``.
    Used by the Settings → Running templates dropdown so the UI never has to
    pull the full template dict for every team just to render a selector.
    """
    # Narrow the catch to ImportError so we degrade gracefully only when the
    # fast_agent package is genuinely absent (e.g. a stripped test harness).
    # Any runtime failure inside list_team_sessions() (DB down, corrupted
    # row, etc.) must propagate as a 500 — silently returning an empty list
    # would render the Settings dropdown blank with no signal that
    # enumeration failed.
    try:
        from fast_agent.spawn.team_spawner import list_team_sessions
    except ImportError as exc:
        logger.warning("[team-template] team_spawner unavailable: %s", exc)
        return {"sessions": []}
    out = []
    for sess in list_team_sessions():
        template = sess.get("template") or {}
        out.append({
            "session_id": sess.get("session_id"),
            "team_name": sess.get("team_name"),
            "template_name": template.get("name"),
            "agents_count": len(sess.get("agents") or {}),
        })
    return {"sessions": out}


@router.get(
    "/{session_id}/template",
    dependencies=[Depends(verify_api_key)],
)
async def get_template(session_id: str):
    """Return current template dict for the session (always fresh from DB)."""
    try:
        return {"session_id": session_id, "template": svc.get_template(session_id)}
    except svc.NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.patch(
    "/{session_id}/template/roles/{role}",
    dependencies=[Depends(verify_api_key)],
)
async def patch_role(
    session_id: str,
    role: str,
    body: PatchRoleBody,
    request: Request,
    db: Session = Depends(get_db),
):
    """Apply a per-role patch. One audit row per changed field.

    The change is persisted to DB SSoT but does NOT touch the yaml template.
    To make edits survive a team-reset / recreate, the caller must commit
    the equivalent change to ``team_templates/*.yaml``. UI should show this
    warning.
    """
    edited_by = _principal(request)
    try:
        result = svc.apply_role_patch(
            db,
            session_id,
            role,
            body.patch,
            edited_by=edited_by,
            source="api",
            comment=body.comment,
        )
    except svc.ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except svc.NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except svc.ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return {
        "session_id": session_id,
        "role": role,
        **result,
        "warning": (
            "Edit applied to DB. To survive a team recreate / reset, also "
            "commit the equivalent change to team_templates/<team>.yaml."
        ),
    }


@router.get(
    "/{session_id}/template/history",
    dependencies=[Depends(verify_api_key)],
)
async def get_history(
    session_id: str,
    role: str | None = None,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    """Audit log, newest first. Optionally filter by ``role``."""
    rows = svc.get_history(db, session_id, role=role, limit=limit)
    return {"session_id": session_id, "role": role, "count": len(rows), "rows": rows}


@router.post(
    "/{session_id}/template/rollback/{audit_id}",
    dependencies=[Depends(verify_api_key)],
)
async def rollback(
    session_id: str,
    audit_id: int,
    body: RollbackBody,
    request: Request,
    db: Session = Depends(get_db),
):
    """Revert the field referenced by ``audit_id`` to its prior value.

    Writes a NEW history row (source='rollback'). The original row is kept.
    Caller responsibility: choose the right audit_id — if multiple edits
    touched the same field after the target row, rolling back to the
    earliest will silently discard intermediate changes.
    """
    edited_by = _principal(request)
    try:
        result = svc.rollback_to(
            db, session_id, audit_id, edited_by=edited_by, comment=body.comment,
        )
    except svc.NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except svc.ValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except svc.ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"session_id": session_id, "audit_id": audit_id, **result}


@router.post(
    "/{session_id}/template/reset/{role}",
    dependencies=[Depends(verify_api_key)],
)
async def reset_role(
    session_id: str,
    role: str,
    body: ResetBody,
    request: Request,
    db: Session = Depends(get_db),
):
    """Reset one role's config back to the yaml factory default.

    Writes audit rows for every field that diverged. Other roles untouched.
    """
    edited_by = _principal(request)
    yaml_path = _resolve_yaml_for_session(session_id)
    try:
        result = svc.reset_role_to_yaml(
            db,
            session_id,
            role,
            yaml_path,
            edited_by=edited_by,
            comment=body.comment,
        )
    except svc.NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except svc.ConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {
        "session_id": session_id,
        "role": role,
        "yaml_path": str(yaml_path),
        **result,
    }


@router.get(
    "/{session_id}/template/yaml-diff",
    dependencies=[Depends(verify_api_key)],
)
async def yaml_diff(session_id: str):
    """Compare the running team's template with its yaml factory default.

    READ-ONLY — never modifies the running team. Returns a per-role diff
    so the UI can show "yaml has 3 changes since this team was created;
    review and click Reset-to-yaml or Reload-from-yaml to apply".

    Decision 2026-05-17: yaml is FACTORY DEFAULT, not continuous source.
    This endpoint lets the user *see* drift without enforcing convergence.
    """
    import yaml as yaml_lib

    try:
        current_template = svc.get_template(session_id)
    except svc.NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    yaml_path = _resolve_yaml_for_session(session_id)
    if not yaml_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"factory yaml not found at {yaml_path}",
        )

    with yaml_path.open(encoding="utf-8") as f:
        yaml_doc = yaml_lib.safe_load(f) or {}
    yaml_template = yaml_doc.get("team") or yaml_doc
    yaml_roles = yaml_template.get("roles") or {}
    current_roles = current_template.get("roles") or {}

    per_role: dict[str, dict] = {}
    all_roles = set(yaml_roles) | set(current_roles)
    for role in all_roles:
        before = yaml_roles.get(role)
        after = current_roles.get(role)
        if before is None:
            per_role[role] = {"status": "added_in_db", "yaml": None, "current": after}
            continue
        if after is None:
            per_role[role] = {"status": "removed_from_db", "yaml": before, "current": None}
            continue
        diff = svc.compute_role_diff(before, after)
        if diff:
            per_role[role] = {"status": "diverged", "fields": diff}
        else:
            per_role[role] = {"status": "in_sync"}

    diverged = {r: v for r, v in per_role.items() if v["status"] != "in_sync"}
    return {
        "session_id": session_id,
        "yaml_path": str(yaml_path),
        "in_sync": not diverged,
        "diverged_count": len(diverged),
        "per_role": per_role,
    }


@router.post(
    "/{session_id}/reload",
    dependencies=[Depends(verify_api_key)],
)
async def reload_team(
    session_id: str,
    body: ReloadBody,
    request: Request,
):
    """Force-kill + respawn agents for the given roles.

    Destructive: agents in the named roles are SIGKILLed mid-task if they
    are running. Use only after a confirmation dialog. ``confirm: true``
    must be present in the body to proceed.

    For each affected agent we:
      1. Find latest spawn record + its PID
      2. SIGTERM, wait 2s, SIGKILL if still alive
      3. Respawn via inject_resume → fresh team_sessions.template from DB
    """
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail=(
                "destructive operation requires {confirm: true}. UI should "
                "show a warning explaining that running agents will be "
                "force-killed mid-task."
            ),
        )
    if not body.roles:
        raise HTTPException(status_code=400, detail="'roles' must be non-empty")

    edited_by = _principal(request)
    try:
        results = await reload_roles(
            session_id=session_id,
            roles=body.roles,
            edited_by=edited_by,
            inject_message=body.inject_message,
        )
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"session_id": session_id, "results": results}


# ── helpers ────────────────────────────────────────────────────────────────


def _principal(request: Request) -> str:
    """Best-effort actor attribution for audit rows.

    The product is single-user today (per user decision: no multi-tenant,
    default principal is "system"); reserve the column for the future. If
    a request principal becomes available we can read it from request.state
    or from a JWT claim here without touching call sites.
    """
    actor = getattr(request.state, "principal", None)
    return actor or "system"
