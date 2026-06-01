"""RPC handlers for team-template editing — registered with RuntimeRpcServer.

Lets the in-process MCP tool subprocess (``tools/team_template_server.py``)
call into the live backend without HTTP / API-key round-trip. Trust model
is filesystem permissions on the UDS socket — same as ``skill_rpc_handlers``
and ``mcp_rpc_handlers``.

Two surfaces:

* **Running team** (``team_template.running.*``) — wraps
  :mod:`services.team_template_service` (DB SSoT, per-session, with audit).
* **Factory yaml** (``team_template.factory.*``) — wraps
  :mod:`services.team_template_factory_service` (yaml files under
  ``backend/team_templates/``).

Errors raised by service-layer ``LookupError`` / ``ValueError`` /
``RuntimeError`` are translated into the structured ``{"error", "status"}``
envelope that ``runtime_rpc_client.call`` forwards to MCP tools.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from services import team_template_factory_service as factory_svc
from services import team_template_service as svc
from services.runtime_rpc import RuntimeRpcServer

logger = logging.getLogger("team_template_rpc")


# ── helpers ────────────────────────────────────────────────────────────────


def _err(message: str, status: int) -> dict:
    return {"error": message, "status": status}


def _session_scope():
    """Open a DB session for one RPC call. Use as ``with _session_scope() as db:``."""
    from core.database import SessionLocal

    return SessionLocal()


def _factory_yaml_path_for(name: str) -> Path:
    """Resolve the yaml path the running template was instantiated from.

    Delegates to ``factory_svc.resolve_factory_path`` so the path-traversal
    guard applies — ``name`` comes from the (LLM-writable, unvalidated)
    template ``name`` field, so raw interpolation here would let it escape
    ``team_templates/``. Raises ``factory_svc.PathTraversalError`` on a bad
    name; callers translate it to a 400.
    """
    return factory_svc.resolve_factory_path(name)


# ── factory yaml (file-level) ──────────────────────────────────────────────


def _factory_list() -> dict:
    return {"templates": factory_svc.list_factory_templates()}


def _factory_read(*, name: str) -> dict:
    try:
        return factory_svc.read_factory_template(name)
    except factory_svc.NotFoundError as exc:
        return _err(str(exc), 404)
    except factory_svc.PathTraversalError as exc:
        return _err(str(exc), 400)
    except factory_svc.FactoryTemplateError as exc:
        return _err(str(exc), 500)


def _factory_write(*, name: str, content: str) -> dict:
    try:
        return factory_svc.write_factory_template(name, content)
    except factory_svc.ValidationError as exc:
        return _err(str(exc), 400)
    except factory_svc.PathTraversalError as exc:
        return _err(str(exc), 400)
    except factory_svc.FactoryTemplateError as exc:
        return _err(str(exc), 500)


# ── running team (DB-level) ────────────────────────────────────────────────


def _running_get(*, session_id: str) -> dict:
    try:
        return {"session_id": session_id, "template": svc.get_template(session_id)}
    except svc.NotFoundError as exc:
        return _err(str(exc), 404)


def _running_patch_role(
    *,
    session_id: str,
    role: str,
    patch: dict[str, Any],
    edited_by: str = "jarvis",
    comment: str = "",
) -> dict:
    db = _session_scope()
    try:
        try:
            result = svc.apply_role_patch(
                db,
                session_id,
                role,
                patch,
                edited_by=edited_by,
                source="mcp",
                comment=comment,
            )
        except svc.ValidationError as exc:
            return _err(str(exc), 400)
        except svc.NotFoundError as exc:
            return _err(str(exc), 404)
        except svc.ConflictError as exc:
            return _err(str(exc), 409)
        return {"session_id": session_id, "role": role, **result}
    finally:
        db.close()


def _running_history(
    *,
    session_id: str,
    role: str | None = None,
    limit: int = 50,
) -> dict:
    db = _session_scope()
    try:
        rows = svc.get_history(db, session_id, role=role, limit=limit)
        return {
            "session_id": session_id,
            "role": role,
            "count": len(rows),
            "rows": rows,
        }
    finally:
        db.close()


def _running_rollback(
    *,
    session_id: str,
    audit_id: int,
    edited_by: str = "jarvis",
    comment: str = "",
) -> dict:
    db = _session_scope()
    try:
        try:
            result = svc.rollback_to(
                db, session_id, audit_id, edited_by=edited_by, comment=comment,
            )
        except svc.NotFoundError as exc:
            return _err(str(exc), 404)
        except svc.ValidationError as exc:
            return _err(str(exc), 400)
        except svc.ConflictError as exc:
            return _err(str(exc), 409)
        return {"session_id": session_id, "audit_id": audit_id, **result}
    finally:
        db.close()


def _running_reset_role(
    *,
    session_id: str,
    role: str,
    edited_by: str = "jarvis",
    comment: str = "",
) -> dict:
    try:
        template = svc.get_template(session_id)
    except svc.NotFoundError as exc:
        return _err(str(exc), 404)
    try:
        yaml_path = _factory_yaml_path_for(template.get("name") or "agile-team")
    except factory_svc.PathTraversalError as exc:
        return _err(str(exc), 400)
    db = _session_scope()
    try:
        try:
            result = svc.reset_role_to_yaml(
                db,
                session_id,
                role,
                yaml_path,
                edited_by=edited_by,
                comment=comment,
            )
        except svc.NotFoundError as exc:
            return _err(str(exc), 404)
        except svc.ConflictError as exc:
            return _err(str(exc), 409)
        return {
            "session_id": session_id,
            "role": role,
            "yaml_path": str(yaml_path),
            **result,
        }
    finally:
        db.close()


def _running_yaml_diff(*, session_id: str) -> dict:
    try:
        current_template = svc.get_template(session_id)
    except svc.NotFoundError as exc:
        return _err(str(exc), 404)

    try:
        yaml_path = _factory_yaml_path_for(current_template.get("name") or "agile-team")
    except factory_svc.PathTraversalError as exc:
        return _err(str(exc), 400)
    if not yaml_path.exists():
        return _err(f"factory yaml not found at {yaml_path}", 404)

    # Open + unwrap roles via the shared helper so the shape-guard can't drift
    # from the REST route's copy. ValidationError = non-mapping yaml → 400.
    try:
        yaml_roles = factory_svc.load_factory_roles(yaml_path)
    except factory_svc.ValidationError as exc:
        return _err(str(exc), 400)
    current_roles = current_template.get("roles") or {}

    per_role: dict[str, dict] = {}
    for role in set(yaml_roles) | set(current_roles):
        before = yaml_roles.get(role)
        after = current_roles.get(role)
        if before is None:
            per_role[role] = {"status": "added_in_db", "yaml": None, "current": after}
            continue
        if after is None:
            per_role[role] = {"status": "removed_from_db", "yaml": before, "current": None}
            continue
        diff = svc.compute_role_diff(before, after)
        per_role[role] = (
            {"status": "diverged", "fields": diff} if diff else {"status": "in_sync"}
        )

    diverged = {r: v for r, v in per_role.items() if v["status"] != "in_sync"}
    return {
        "session_id": session_id,
        "yaml_path": str(yaml_path),
        "in_sync": not diverged,
        "diverged_count": len(diverged),
        "per_role": per_role,
    }


async def _running_reload(
    *,
    session_id: str,
    roles: list[str],
    edited_by: str = "jarvis",
    inject_message: str | None = None,
) -> dict:
    """Force-kill + respawn agents. Async — reload_roles awaits inject_resume."""
    from services.team_reload import reload_roles

    if not roles:
        return _err("'roles' must be non-empty", 400)
    try:
        results = await reload_roles(
            session_id=session_id,
            roles=roles,
            edited_by=edited_by,
            inject_message=inject_message,
        )
    except LookupError as exc:
        return _err(str(exc), 404)
    return {"session_id": session_id, "results": results}


# ── registration ───────────────────────────────────────────────────────────


_METHODS: dict[str, Any] = {
    # Factory yaml
    "team_template.factory.list": _factory_list,
    "team_template.factory.read": _factory_read,
    "team_template.factory.write": _factory_write,
    # Running team
    "team_template.running.get": _running_get,
    "team_template.running.patch_role": _running_patch_role,
    "team_template.running.history": _running_history,
    "team_template.running.rollback": _running_rollback,
    "team_template.running.reset_role": _running_reset_role,
    "team_template.running.yaml_diff": _running_yaml_diff,
    "team_template.running.reload": _running_reload,
}


def register(server: RuntimeRpcServer) -> None:
    """Register every team-template method on the given RPC server.

    Call once at boot from ``server.py`` lifespan, next to the other
    ``*_rpc_handlers.register`` calls.
    """
    for name, handler in _METHODS.items():
        server.register(name, handler)
    logger.info("[team_template_rpc] registered %d methods", len(_METHODS))
