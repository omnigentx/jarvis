"""Human-approval gate for high-blast-radius server-side actions.

Used by:
* :func:`services.mcp_admin_service.install_dependencies` — block
  ``pip install`` against an LLM-editable ``requirements.txt`` until the
  user OKs the package list.
* :func:`services.cron_scheduler.CronScheduler._execute_agent_turn` —
  block the first execution of any new ``exec_payload`` so a poisoned
  cron entry can't fire unsupervised at 03:00.

The gate piggybacks on the existing approval pipeline
(:mod:`approval_service`): create an ApprovalRequestModel, wait for the
user to decide via the dashboard. Decisions are remembered by
``(approval_type, scope_key, content_hash)`` so a repeat run with the
same content auto-proceeds without prompting again. A rejected hash
stays rejected — re-asking on every cron fire would be noise.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Default: wait up to 1 hour for user. After that, treat as rejected so the
# caller (cron tick, tool call) returns control rather than hanging forever.
DEFAULT_GATE_TIMEOUT_S = 3600.0


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _matches_scope(row, scope_key: str, content_hash: str) -> bool:
    try:
        meta = json.loads(row.metadata_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return False
    return meta.get("scope_key") == scope_key and meta.get("content_hash") == content_hash


def _find_prior_decision(
    approval_type: str, scope_key: str, content_hash: str
) -> Optional[str]:
    """Return ``'approved'`` / ``'rejected'`` if a *resolved* approval for
    this exact (type, scope_key, content_hash) exists. ``None`` otherwise."""
    from core.database import ApprovalRequestModel, SessionLocal

    db = SessionLocal()
    try:
        rows = (
            db.query(ApprovalRequestModel)
            .filter(
                ApprovalRequestModel.approval_type == approval_type,
                ApprovalRequestModel.status.in_(("approved", "rejected")),
            )
            .order_by(ApprovalRequestModel.resolved_at.desc())
            .all()
        )
        for row in rows:
            if _matches_scope(row, scope_key, content_hash):
                return row.status
    finally:
        db.close()
    return None


def _find_pending_match(
    approval_type: str, scope_key: str, content_hash: str
) -> Optional[str]:
    """Return the approval_id of an existing *pending* approval that
    matches the (type, scope_key, content_hash) — so concurrent cron fires
    coalesce onto a single user prompt instead of stacking duplicates."""
    from core.database import ApprovalRequestModel, SessionLocal

    db = SessionLocal()
    try:
        rows = (
            db.query(ApprovalRequestModel)
            .filter(
                ApprovalRequestModel.approval_type == approval_type,
                ApprovalRequestModel.status == "pending",
            )
            .all()
        )
        for row in rows:
            if _matches_scope(row, scope_key, content_hash):
                return row.id
    finally:
        db.close()
    return None


async def gate(
    *,
    approval_type: str,
    scope_key: str,
    content_md: str,
    title: str,
    agent_name: str = "Jarvis",
    timeout_s: float = DEFAULT_GATE_TIMEOUT_S,
) -> tuple[bool, str]:
    """Block until the user approves (or rejects) the gated action.

    Args:
        approval_type: discriminator stored on the row, e.g. ``"mcp_install"``.
        scope_key:     stable subject identity, e.g. ``"mcp:server_foo"`` or
                       ``"cron:abcd1234"``.
        content_md:    markdown shown in the approval modal — should include
                       the exact payload + a "use at your own risk" warning.
        title:         short header for the approval card.
        agent_name:    affects auto-pause + UI attribution. Default Jarvis.
        timeout_s:     max wait. On timeout, return as rejected.

    Returns:
        (approved, reason) — ``reason`` is a short string for logging /
        propagating back to the caller's error envelope.
    """
    # Local import keeps services/__init__.py free of an import cycle —
    # approval_service depends on activity_stream_manager which depends on
    # other services that import this gate.
    from services.approval_service import approval_service

    h = _content_hash(content_md)

    prior = _find_prior_decision(approval_type, scope_key, h)
    if prior == "approved":
        logger.info(
            "[GATE] %s/%s — prior approval for hash=%s; proceeding without prompt",
            approval_type, scope_key, h,
        )
        return True, "previously approved (same content hash)"
    if prior == "rejected":
        logger.info(
            "[GATE] %s/%s — prior rejection for hash=%s; refusing",
            approval_type, scope_key, h,
        )
        return False, "previously rejected (same content hash)"

    existing_id = _find_pending_match(approval_type, scope_key, h)
    if existing_id:
        logger.info(
            "[GATE] %s/%s — joining existing pending approval %s",
            approval_type, scope_key, existing_id,
        )
        approval_id = existing_id
    else:
        record = approval_service.create_approval({
            "agent_name": agent_name,
            "approval_type": approval_type,
            "title": title,
            "content": content_md,
            "content_format": "markdown",
            "urgency": "normal",
            "metadata": {
                "scope_key": scope_key,
                "content_hash": h,
            },
        })
        approval_id = record["id"]
        logger.info(
            "[GATE] %s/%s — created approval %s; awaiting user",
            approval_type, scope_key, approval_id,
        )

    try:
        resolved = await asyncio.wait_for(
            approval_service.wait_for_resolution(approval_id),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "[GATE] %s/%s — timeout after %.0fs",
            approval_type, scope_key, timeout_s,
        )
        return False, f"approval timeout ({timeout_s:.0f}s)"

    if resolved.get("user_decision") == "approve":
        return True, "user approved"
    return False, f"user rejected: {resolved.get('user_comment') or 'no comment'}"
