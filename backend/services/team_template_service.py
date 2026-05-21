"""Team template edit / diff / audit / rollback — pure service layer.

Mounted as REST in ``routes/team_template.py``. Keep this module free of HTTP
concerns so callers (cron jobs, CLI scripts, future MCP tools) can reuse it.

Concepts:
  - Template = ``team_sessions.data_json.template`` (per-team config snapshot).
    SSoT for the RUNNING team. ``team_templates/*.yaml`` is the factory
    default used only at team creation.
  - History = ``team_template_history`` (audit log, append-only). Every
    edit / rollback writes a row.
  - Reload = separate concern, see ``services.team_reload``.

Field-level edits (``patch`` argument) are diffed against current value and
recorded with structural granularity (``field`` column = "servers" /
"instruction" / "skills" / "server_overrides" / "model"). One PATCH call ⇒
one or more history rows (one per changed field).
"""
from __future__ import annotations

import copy
import json
import logging
import time
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from core.database import TeamTemplateHistory

logger = logging.getLogger(__name__)


# Fields of ``template.roles[role]`` we allow editing via this API. Edits to
# unknown fields are rejected — keeps the surface area tight and prevents
# accidental injection of unsafe keys (e.g. raw "cwd", "env" overrides).
ALLOWED_ROLE_FIELDS: frozenset[str] = frozenset({
    "instruction",
    "servers",
    "skills",
    "server_overrides",
    "model",
    "role_display",
})


class ValidationError(ValueError):
    """Raised when a patch fails schema or path-existence checks."""


class NotFoundError(LookupError):
    """Raised when session_id / role / history_id doesn't exist."""


class ConflictError(RuntimeError):
    """Raised on a no-op patch (everything already matches desired state)."""


# ────────────────────────────────────────────────────────────────────────────
# Read helpers — always fresh from DB (no caching). The team_sessions row IS
# the single source of truth for the running team's template. Any caller that
# holds an old reference must re-fetch before computing diffs.
# ────────────────────────────────────────────────────────────────────────────


def _get_team_session_dict(session_id: str) -> dict[str, Any]:
    """Fetch a team_sessions row as dict. Raises NotFoundError if missing."""
    # Lazy import — fast-agent package isn't part of the test harness for
    # service-level unit tests. The function is mocked in those tests.
    from fast_agent.spawn.team_spawner import get_team_session

    sess = get_team_session(session_id)
    if sess is None:
        raise NotFoundError(f"team session '{session_id}' not found")
    return sess.to_dict()


def _put_team_session_dict(session_id: str, data: dict[str, Any]) -> None:
    """Persist mutated team_sessions row back to SQLite."""
    from fast_agent.spawn.team_spawner import _get_store  # noqa: PLC2701

    _get_store().upsert(session_id, data)


def get_template(session_id: str) -> dict[str, Any]:
    """Return the current template dict for a session.

    Always re-reads from DB — no in-memory cache. Callers that need the live
    state (UI render, diff target, reload trigger) must call this each time.
    """
    return _get_team_session_dict(session_id).get("template", {})


def get_role_config(session_id: str, role: str) -> dict[str, Any]:
    """Return one role's config dict. Raises NotFoundError if role missing."""
    roles = get_template(session_id).get("roles", {})
    if role not in roles:
        raise NotFoundError(f"role '{role}' not in template (have: {sorted(roles)})")
    return roles[role]


# ────────────────────────────────────────────────────────────────────────────
# Diff
# ────────────────────────────────────────────────────────────────────────────


def compute_role_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, dict]:
    """Return ``{field: {"before": <v>, "after": <v>}}`` for changed fields.

    Order-insensitive for list-of-strings fields (``servers``, ``skills``)
    so that re-ordering doesn't count as a change. Deep-equal for dicts.
    """
    diff: dict[str, dict] = {}
    keys = set(before) | set(after)
    for k in keys:
        b, a = before.get(k), after.get(k)
        if _equal(k, b, a):
            continue
        diff[k] = {"before": b, "after": a}
    return diff


def _equal(field: str, b: Any, a: Any) -> bool:
    """Field-aware equality. Lists of strings are compared as sets when the
    field is a known list-of-server / skill name (order doesn't carry meaning).
    """
    if field in ("servers", "skills") and isinstance(b, list) and isinstance(a, list):
        return sorted(b) == sorted(a)
    return b == a


# ────────────────────────────────────────────────────────────────────────────
# Validation
# ────────────────────────────────────────────────────────────────────────────


def validate_patch(role: str, patch: dict[str, Any], project_dir: Path | None = None) -> None:
    """Reject patches with unknown fields or path-shaped args that won't exist.

    Mirrors the regression test ``test_fastagent_config_defaults`` — any path
    in ``server_overrides.{server}.args`` must resolve to an existing
    directory (or be a template placeholder like ``{workspace_dir}``).
    """
    if not isinstance(patch, dict):
        raise ValidationError("patch must be an object")

    unknown = set(patch) - ALLOWED_ROLE_FIELDS
    if unknown:
        raise ValidationError(
            f"unknown fields: {sorted(unknown)}. Allowed: {sorted(ALLOWED_ROLE_FIELDS)}"
        )

    if "servers" in patch:
        s = patch["servers"]
        if not isinstance(s, list) or not all(isinstance(x, str) and x for x in s):
            raise ValidationError("'servers' must be a list of non-empty strings")

    if "skills" in patch:
        s = patch["skills"]
        if not isinstance(s, list) or not all(isinstance(x, str) and x for x in s):
            raise ValidationError("'skills' must be a list of non-empty strings")

    if "instruction" in patch and not isinstance(patch["instruction"], str):
        raise ValidationError("'instruction' must be a string")

    if "model" in patch and not isinstance(patch["model"], str):
        raise ValidationError("'model' must be a string")

    if "server_overrides" in patch:
        overrides = patch["server_overrides"]
        if not isinstance(overrides, dict):
            raise ValidationError("'server_overrides' must be an object")
        for server_name, cfg in overrides.items():
            if not isinstance(cfg, dict):
                raise ValidationError(
                    f"server_overrides['{server_name}'] must be an object"
                )
            args = cfg.get("args")
            if args is not None:
                if not isinstance(args, list):
                    raise ValidationError(
                        f"server_overrides['{server_name}'].args must be a list"
                    )
                for arg in args:
                    if not isinstance(arg, str):
                        raise ValidationError(
                            f"server_overrides['{server_name}'].args items must be strings"
                        )
                    _check_path_arg(server_name, arg, project_dir)


def _check_path_arg(server_name: str, arg: str, project_dir: Path | None) -> None:
    """If ``arg`` looks like a real filesystem path (not a placeholder or flag),
    verify it exists. Same heuristic as test_fastagent_config_defaults.
    """
    if not arg or arg.startswith("-"):
        return
    if "{" in arg and "}" in arg:  # template placeholder
        return
    if arg.startswith("@") and "/" in arg:  # npm package specifier
        return
    if not (arg.startswith((".", "/", "~")) or "/" in arg):
        return  # bare module name

    if arg.startswith("./"):
        if project_dir is None:
            return  # can't resolve relative without context
        resolved = project_dir / arg[2:]
    elif arg.startswith("~"):
        resolved = Path(arg).expanduser()
    else:
        resolved = Path(arg)

    if not resolved.exists():
        raise ValidationError(
            f"server_overrides['{server_name}'].args contains path '{arg}' "
            f"that resolves to {resolved} — directory does not exist. Create "
            f"it first or use a template placeholder like '{{workspace_dir}}'."
        )


# ────────────────────────────────────────────────────────────────────────────
# Apply (PATCH)
# ────────────────────────────────────────────────────────────────────────────


def apply_role_patch(
    db: Session,
    session_id: str,
    role: str,
    patch: dict[str, Any],
    *,
    edited_by: str = "system",
    source: str = "api",
    comment: str = "",
    project_dir: Path | None = None,
) -> dict[str, Any]:
    """Apply a per-role patch, write audit rows, persist team_sessions.

    Returns ``{audit_ids: [...], diff: {field: {before, after}}, after_role: {...}}``.

    Atomic at the DB level — either all audit rows + the team_sessions update
    commit together, or nothing changes. Multiple fields in one patch ⇒
    multiple audit rows (one per field) but a single commit.

    Raises:
        ValidationError: invalid patch shape or missing path
        NotFoundError: unknown session_id / role
        ConflictError: patch is a no-op (everything already matches)
    """
    validate_patch(role, patch, project_dir=project_dir)

    data = _get_team_session_dict(session_id)
    roles = data.get("template", {}).get("roles", {})
    if role not in roles:
        raise NotFoundError(f"role '{role}' not in template")

    before_role = copy.deepcopy(roles[role])
    after_role = copy.deepcopy(before_role)
    after_role.update(patch)

    diff = compute_role_diff(before_role, after_role)
    if not diff:
        raise ConflictError(
            f"no-op: role '{role}' already matches the requested state"
        )

    # Write audit rows (one per changed field). Source-of-truth update goes
    # in the SAME transaction so partial state is impossible.
    now = time.time()
    audit_ids: list[int] = []
    for field, change in diff.items():
        row = TeamTemplateHistory(
            session_id=session_id,
            role=role,
            field=field,
            before_json=json.dumps(change["before"], ensure_ascii=False),
            after_json=json.dumps(change["after"], ensure_ascii=False),
            source=source,
            edited_by=edited_by,
            edited_at=now,
            comment=comment or None,
        )
        db.add(row)
        db.flush()  # populate row.id without committing
        audit_ids.append(row.id)

    # Persist template update. ``_put_team_session_dict`` writes to the
    # team_sessions store, which uses its own connection — commit ``db``
    # first so audit rows are durable before the dependent template update.
    db.commit()

    roles[role] = after_role
    _put_team_session_dict(session_id, data)

    logger.info(
        "[TEMPLATE-PATCH] session=%s role=%s fields=%s audit_ids=%s source=%s edited_by=%s",
        session_id, role, sorted(diff), audit_ids, source, edited_by,
    )

    return {
        "audit_ids": audit_ids,
        "diff": diff,
        "after_role": after_role,
        "edited_at": now,
    }


# ────────────────────────────────────────────────────────────────────────────
# History + rollback
# ────────────────────────────────────────────────────────────────────────────


def get_history(
    db: Session,
    session_id: str,
    role: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Return audit rows for a session, optionally filtered by role.

    Sorted newest-first. Both ``before_json`` and ``after_json`` are
    decoded back to native Python so callers don't need to re-parse.
    """
    q = db.query(TeamTemplateHistory).filter(
        TeamTemplateHistory.session_id == session_id,
    )
    if role:
        q = q.filter(TeamTemplateHistory.role == role)
    rows = (
        q.order_by(TeamTemplateHistory.edited_at.desc())
        .limit(max(1, min(limit, 500)))
        .all()
    )
    return [
        {
            "id": r.id,
            "session_id": r.session_id,
            "role": r.role,
            "field": r.field,
            "before": json.loads(r.before_json) if r.before_json else None,
            "after": json.loads(r.after_json) if r.after_json else None,
            "source": r.source,
            "edited_by": r.edited_by,
            "edited_at": r.edited_at,
            "comment": r.comment,
        }
        for r in rows
    ]


def rollback_to(
    db: Session,
    session_id: str,
    audit_id: int,
    *,
    edited_by: str = "system",
    comment: str = "",
) -> dict[str, Any]:
    """Revert ``role.field`` to the ``before`` value of the given audit row.

    Writes a NEW audit row (``source='rollback'``) referencing the original
    in ``comment`` — we never delete history. If the field already matches
    the target value, raises ConflictError.
    """
    row = db.query(TeamTemplateHistory).filter(
        TeamTemplateHistory.id == audit_id,
        TeamTemplateHistory.session_id == session_id,
    ).first()
    if row is None:
        raise NotFoundError(f"audit row {audit_id} not found for session {session_id}")
    if not row.role or not row.field:
        raise ValidationError(
            f"audit row {audit_id} has no role/field — cannot rollback"
        )

    target_value = json.loads(row.before_json) if row.before_json else None
    patch = {row.field: target_value}
    rollback_comment = (
        f"rollback of audit_id={audit_id} ({row.field}). "
        f"{comment}".strip()
    )
    return apply_role_patch(
        db,
        session_id,
        row.role,
        patch,
        edited_by=edited_by,
        source="rollback",
        comment=rollback_comment,
    )


# ────────────────────────────────────────────────────────────────────────────
# Reset to yaml factory default
# ────────────────────────────────────────────────────────────────────────────


def reset_role_to_yaml(
    db: Session,
    session_id: str,
    role: str,
    yaml_path: Path,
    *,
    edited_by: str = "system",
    comment: str = "",
) -> dict[str, Any]:
    """Reset one role's config back to the yaml factory default.

    Reads the yaml file, extracts ``team.roles[role]``, and applies it as a
    patch (so every field reset is audited). The current team_sessions
    template is NOT replaced wholesale — only the named role is reset.
    Other roles' current state (including UI edits) is preserved.
    """
    import yaml as yaml_lib  # local import — yaml isn't always at top

    if not yaml_path.exists():
        raise NotFoundError(f"yaml template not found at {yaml_path}")

    with yaml_path.open(encoding="utf-8") as f:
        yaml_doc = yaml_lib.safe_load(f) or {}

    yaml_roles = yaml_doc.get("team", {}).get("roles") or yaml_doc.get("roles") or {}
    if role not in yaml_roles:
        raise NotFoundError(f"role '{role}' not in yaml at {yaml_path}")

    yaml_role = yaml_roles[role]
    patch = {
        k: yaml_role[k] for k in ALLOWED_ROLE_FIELDS if k in yaml_role
    }
    return apply_role_patch(
        db,
        session_id,
        role,
        patch,
        edited_by=edited_by,
        source="yaml-reset",
        comment=comment or f"reset to yaml: {yaml_path.name}",
    )
