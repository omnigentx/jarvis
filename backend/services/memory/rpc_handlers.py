"""Runtime-RPC handlers for the agent-facing memory tools.

Run in the MAIN backend process (can touch the DB + orchestrator + load
models). The companion ``tools/memory_server.py`` MCP subprocess wraps each
method as an LLM-facing tool and injects the agent's bound identity as
``agent_name`` from its environment — the LLM never supplies it. Every handler
therefore treats ``agent_name`` as the trusted owner and scopes all access to
it.
"""
from __future__ import annotations

import logging
import time

from core.database import (
    CommunicationRecord,
    EpisodicDocument,
    MemoryRecord,
    get_db_session,
)
from services.memory.settings import get_memory_settings
from services.retrieval.contracts import RetrievalRequest
from services.retrieval.orchestrator import RetrievalOrchestrator
from services.runtime_rpc import RuntimeRpcServer

logger = logging.getLogger("memory_rpc")

_DISABLED = {
    "error": "memory_disabled",
    "message": ("Agent memory is OFF. Enable it in Settings → Agent Memory "
                "(the toggle hot-reloads; no restart needed)."),
}


def _enabled() -> bool:
    try:
        return get_memory_settings().enabled
    except Exception:  # noqa: BLE001
        return False


def _comm_authorized(rec: CommunicationRecord, agent_name: str) -> bool:
    """Single source of comm participant authorization — delegates to the
    provider's check so the fetch gate can never drift from the search gate."""
    from services.retrieval.providers.communication_provider import _authorized
    return _authorized(rec, agent_name)


async def memory_search(*, agent_name: str, query: str, types: list | None = None,
                        mode: str = "balanced", limit: int = 5) -> dict:
    if not _enabled():
        return _DISABLED
    if not agent_name:
        return {"error": "missing bound agent identity"}
    db = get_db_session()
    try:
        orch = RetrievalOrchestrator(db, get_memory_settings())
        req = RetrievalRequest(owner_agent_name=agent_name, query=query,
                               types=types or [], mode=mode, limit=limit)
        # The tool was explicitly invoked → always run fast retrieval.
        result = await orch.retrieve(req, now=time.time(), agent_requested=True)
        # Compact, agent-facing shape: this lands verbatim in the model's context
        # window, so return only the content + an id (for memory_fetch) — not the
        # scoring/source/validity metadata the debug UI needs. ``degraded`` only
        # when true (recall is partial) so the agent can hedge. The UI route
        # (routes/memory.py) keeps the full to_dict for its debug panel.
        out: dict = {"memories": [e.to_agent_dict() for e in result.evidence]}
        if result.degraded:
            out["degraded"] = True
        return out
    finally:
        db.close()


async def memory_fetch(*, agent_name: str, evidence_ids: list) -> dict:
    """Return full authorized source content for selected evidence ids.
    Owner-scoped: an id belonging to another agent yields nothing."""
    if not _enabled():
        return _DISABLED
    if not agent_name:
        return {"error": "missing bound agent identity"}
    db = get_db_session()
    out = []
    try:
        for eid in evidence_ids or []:
            kind, _, rid = str(eid).partition(":")
            if kind == "episodic":
                doc = db.get(EpisodicDocument, rid)
                if doc and doc.owner_agent_name == agent_name:
                    out.append({"evidence_id": eid, "content": doc.content,
                                "source_id": doc.source_id})
            elif kind == "memory":
                rec = db.get(MemoryRecord, rid)
                if rec and rec.owner_agent_name == agent_name:
                    out.append({"evidence_id": eid, "content": rec.content})
            elif kind == "comm":
                # Graph/comm evidence uses the canonical {kind}:{id} scheme too;
                # re-check participant authorization (sender or recipient), the
                # same gate communication_provider applies at search time.
                crec = db.get(CommunicationRecord, rid)
                if crec and _comm_authorized(crec, agent_name):
                    out.append({"evidence_id": eid,
                                "content": f"{crec.subject or ''}\n{crec.body or ''}".strip(),
                                "source_id": crec.id})
        return {"items": out}
    finally:
        db.close()


async def memory_remember(*, agent_name: str, content: str,
                          memory_type: str = "semantic", subject_scope: str | None = None,
                          pinned: bool = False) -> dict:
    """Propose a durable memory. NEVER writes active memory directly — creates
    a candidate. Whether it auto-persists or awaits approval follows the
    approval policy (secrets always require approval)."""
    if not _enabled():
        return _DISABLED
    if not agent_name:
        return {"error": "missing bound agent identity"}
    from services.memory import candidate_service as cnd
    from services.memory.models import is_valid_subject_scope
    settings = get_memory_settings()
    # The agent doesn't pick the scope taxonomy; default to the user. A scope
    # the LLM guessed (e.g. "user_profile") is normalized, never an error —
    # failing would make the agent claim success while nothing was saved.
    scope = subject_scope or "user"
    if not is_valid_subject_scope(scope):
        scope = "user"
    requires_approval = settings.approval_policy != "auto_low_risk"
    db = get_db_session()
    try:
        cand = cnd.create_candidate(
            db, owner_agent_name=agent_name, candidate_type="agent_remember",
            payload={"memory_type": memory_type, "content": content,
                     "subject_scope": scope, "authority": "agent_observed",
                     "pinned": pinned},
            requires_approval=requires_approval,
            pinned_token_budget=settings.pinned_token_budget,
        )
        return {"candidate_id": cand.id, "status": cand.status}
    finally:
        db.close()


async def memory_forget(*, agent_name: str, memory_id: str, reason: str = "") -> dict:
    """Archive one of YOUR memories (reversible, audited). Owner-scoped."""
    if not _enabled():
        return _DISABLED
    if not agent_name:
        return {"error": "missing bound agent identity"}
    from services.memory.memory_service import MemoryService, MemoryWriteError
    db = get_db_session()
    try:
        svc = MemoryService(db, pinned_token_budget=get_memory_settings().pinned_token_budget)
        rec = svc.archive_memory(memory_id, owner_agent_name=agent_name, changed_by="agent")
        return {"status": "archived", "memory_id": rec.id}
    except MemoryWriteError as exc:
        return {"error": str(exc)}
    finally:
        db.close()


async def procedure_propose(*, agent_name: str, title: str, steps: str) -> dict:
    """Propose a reusable procedure/Skill. Always requires approval; never
    auto-published."""
    if not _enabled():
        return _DISABLED
    if not agent_name:
        return {"error": "missing bound agent identity"}
    from services.memory import candidate_service as cnd
    db = get_db_session()
    try:
        cand = cnd.create_candidate(
            db, owner_agent_name=agent_name, candidate_type="procedure",
            payload={"memory_type": "procedural", "content": f"{title}\n{steps}",
                     "subject_scope": f"agent:{agent_name}", "authority": "agent_observed"},
            requires_approval=True,
        )
        return {"candidate_id": cand.id, "status": cand.status}
    finally:
        db.close()


_METHODS = {
    "memory.search": memory_search,
    "memory.fetch": memory_fetch,
    "memory.remember": memory_remember,
    "memory.forget": memory_forget,
    "memory.procedure_propose": procedure_propose,
}


def register(server: RuntimeRpcServer) -> None:
    """Wire memory handlers onto the RPC server. Call once at boot from
    server.py (next to the other ``*_rpc_handlers.register`` calls)."""
    for name, handler in _METHODS.items():
        server.register(name, handler)
    logger.info("[memory_rpc] registered %d memory methods", len(_METHODS))
