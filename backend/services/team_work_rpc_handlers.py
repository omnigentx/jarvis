"""Live backend handlers for the Jarvis team-work MCP tools."""
from __future__ import annotations

import logging

from services import team_work_service as work
from services.runtime_rpc import RuntimeRpcServer

logger = logging.getLogger(__name__)


def _authorize(caller_agent: str) -> dict | None:
    if caller_agent != "Jarvis":
        return {"error": "Only Jarvis can direct teams", "status": 403}
    return None


def _find(*, caller_agent: str, query: str = "", limit: int = 20) -> dict:
    if error := _authorize(caller_agent):
        return error
    try:
        return {"teams": work.find_teams(query, limit)}
    except work.TeamWorkError as exc:
        return {"error": str(exc), "status": exc.status}


def _revisions(
    *, caller_agent: str, session_id: str, limit: int = 20,
    before_revision: int | None = None,
) -> dict:
    if error := _authorize(caller_agent):
        return error
    try:
        binding = work.get_binding(session_id)
        return {"session_id": session_id, "revisions": work.get_team_revisions(
            session_id, binding["conversation_id"], limit=limit,
            before_revision=before_revision,
        )}
    except work.TeamWorkError as exc:
        return {"error": str(exc), "status": exc.status}


async def _change(
    *, caller_agent: str, session_id: str, change_text: str,
    idempotency_key: str | None = None,
    expected_revision: int | None = None,
    source_refs: list[str] | None = None,
) -> dict:
    if error := _authorize(caller_agent):
        return error
    try:
        binding = work.get_binding(session_id)
        revision = work.create_revision(
            session_id, binding["conversation_id"], change_text,
            idempotency_key=idempotency_key,
            expected_revision=expected_revision,
            source_refs=source_refs,
        )
        try:
            return await work.deliver_revision(revision["id"])
        except Exception as exc:
            logger.warning("[TEAM-WORK] Delivery pending for %s: %s",
                           revision["id"], exc, exc_info=True)
            revision["delivery_error"] = str(exc)
            return revision
    except work.TeamWorkError as exc:
        return {"error": str(exc), "status": exc.status}


async def _retry(*, caller_agent: str, session_id: str) -> dict:
    if error := _authorize(caller_agent):
        return error
    try:
        binding = work.get_binding(session_id)
        return {"delivered": await work.retry_pending(
            session_id, binding["conversation_id"]
        )}
    except work.TeamWorkError as exc:
        return {"error": str(exc), "status": exc.status}


def register(server: RuntimeRpcServer) -> None:
    server.register("team_work.find", _find)
    server.register("team_work.revisions", _revisions)
    server.register("team_work.change", _change)
    server.register("team_work.retry", _retry)
