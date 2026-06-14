"""Conflict detection + resolution (spec §11.4, §20).

Tiered + efficient: on materializing a durable memory, an embedding similarity
check (cheap) decides whether a RELATED memory already exists in the same
scope. Only then is the curator LLM invoked to decide whether the new memory
is a reversal (supersede), a refinement (keep both), a merge, or noise
(reject) — so the LLM never runs on the common no-conflict path.

Histories are always preserved (versions + superseded status); active memory
is never silently overwritten. Degrades safely: no embeddings → skip the check
(create normally); embeddings but no curator → defer a conflict candidate for
human review in the Memory page.
"""
from __future__ import annotations

import logging
import math

from sqlalchemy.orm import Session

from core.database import MemoryRecord
from services.memory import curator as curator_mod
from services.memory.models import MemoryStatus

logger = logging.getLogger("memory.conflict")

SIMILARITY_THRESHOLD = 0.82      # "about the same subject" → candidate for conflict
_CONFLICT_TYPES = {"semantic", "pinned"}   # episodic is immutable history


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def find_similar(db: Session, *, owner: str, scope: str, memory_type: str,
                 content: str, embedding, threshold: float = SIMILARITY_THRESHOLD):
    """Most-similar existing ACTIVE memory in the same (owner, scope, type)
    above the threshold, or None. Excludes exact-text duplicates (handled by
    MemoryService dedupe)."""
    norm = " ".join((content or "").split()).lower()
    rows = (
        db.query(MemoryRecord)
        .filter(MemoryRecord.owner_agent_name == owner,
                MemoryRecord.subject_scope == scope,
                MemoryRecord.memory_type == memory_type,
                MemoryRecord.status == MemoryStatus.ACTIVE.value)
        .all()
    )
    rows = [r for r in rows if r.normalized_content != norm]
    if not rows:
        return None
    qv = embedding.embed_query(content)
    vecs = embedding.embed_documents([r.normalized_content for r in rows])
    best, best_sim = None, 0.0
    for r, rv in zip(rows, vecs):
        sim = _cosine(qv, rv)
        if sim > best_sim:
            best, best_sim = r, sim
    return (best, best_sim) if best_sim >= threshold else None


def _ctx_with_override(ctx, provider: str, base_url: str | None, api_key: str | None):
    """A context copy whose ``config.<provider>`` carries the curator's own
    base_url/api_key, so a curator on a separate endpoint (e.g. a cheaper
    router) never mutates the main agent's provider config."""
    import copy as _copy
    ctx2 = _copy.copy(ctx)
    ctx2.config = ctx.config.model_copy(deep=True)
    prov_cfg = getattr(ctx2.config, provider, None)
    if prov_cfg is not None:
        if base_url:
            prov_cfg.base_url = base_url
        if api_key:
            prov_cfg.api_key = api_key
    return ctx2


def build_curator():
    """Best-effort MemoryCurator. Resolves the curator LLM per the provider /
    model / base_url / api_key settings (see SettingsMemory case matrix):

      - provider Default + no model  → the main agent LLM, unchanged (inherit).
      - provider Default + model     → main provider/creds, but a different model.
      - explicit provider            → that provider; creds from the curator
                                       fields when filled, else that provider's
                                       config from Settings → LLM Provider.

    Returns None when no LLM context is available (boot/tests) — callers then
    defer to a human-reviewed conflict candidate."""
    try:
        import asyncio

        import services.shared_state as state
        from services.memory.settings import get_curator_api_key, get_memory_settings
        agents = getattr(state.agent_app, "_agents", {}) or {}
        agent = next(iter(agents.values()), None)
        if agent is None:
            return None
        cfg = get_memory_settings()
        provider = (cfg.curator_provider or "").strip()
        model = (cfg.curator_model or "").strip()
        ctx = getattr(agent, "_context", None) or getattr(agent, "context", None)
        main_llm = getattr(agent, "_llm", None)

        def _wrap(llm):
            from fast_agent.core.prompt import Prompt

            def llm_fn(prompt: str) -> str:
                resp = asyncio.get_event_loop().run_until_complete(
                    llm.generate([Prompt.user(prompt)], request_params=None, tools=None))
                parts = [getattr(b, "text", "") for b in (getattr(resp, "content", None) or [])]
                return "\n".join(p for p in parts if p)
            return curator_mod.MemoryCurator(llm_fn)

        # Inherit everything (or explicit provider without a model) → main LLM.
        if (not provider and not model) or (provider and not model):
            return _wrap(main_llm) if main_llm is not None else None

        from fast_agent.agents.agent_types import AgentConfig
        from fast_agent.agents.llm_agent import LlmAgent
        from fast_agent.llm.model_factory import ModelFactory

        if provider:
            spec = f"{provider}.{model}"
            api_key = get_curator_api_key()
            use_ctx = (_ctx_with_override(ctx, provider, cfg.curator_base_url, api_key)
                       if (cfg.curator_base_url or "").strip() or api_key else ctx)
        else:
            spec = model               # Default provider, explicit model
            use_ctx = ctx

        shell = LlmAgent(AgentConfig(name="memory-curator"), context=use_ctx)
        llm = ModelFactory.create_factory(spec)(shell)
        return _wrap(llm)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[MEMORY] curator unavailable: %s", exc)
        return None


def resolve_or_create(
    db: Session, *, owner: str, memory_type: str, content: str, scope: str,
    authority: str, now: float, sources: list | None = None,
    pinned_token_budget: int = 1500, changed_by: str = "system",
    curator=None, embedding=None,
) -> str:
    """Create the memory, resolving a conflict first if a related memory
    exists. Returns the action taken: created | superseded | merged |
    kept_both | rejected | deferred."""
    from services.memory.memory_service import MemoryService

    def _create(c=content):
        MemoryService(db, pinned_token_budget=pinned_token_budget).create_memory(
            owner_agent_name=owner, memory_type=memory_type, content=c,
            subject_scope=scope, authority=authority, sources=sources,
            changed_by=changed_by, now=now, allow_secret=True)

    if memory_type not in _CONFLICT_TYPES or embedding is None or not embedding.is_available():
        _create()
        return "created"

    similar = find_similar(db, owner=owner, scope=scope, memory_type=memory_type,
                           content=content, embedding=embedding)
    if similar is None:
        _create()
        return "created"

    existing, sim = similar
    curator = curator or build_curator()
    if curator is None:
        _defer_conflict(db, owner, existing, content, memory_type, scope, sim, now)
        return "deferred"

    decision = curator.decide(
        candidate={"content": content, "memory_type": memory_type, "scope": scope},
        conflicts=[{"id": existing.id, "content": existing.content}])
    svc = MemoryService(db, pinned_token_budget=pinned_token_budget)
    if decision.action == curator_mod.REJECT:
        return "rejected"
    if decision.action == curator_mod.SUPERSEDE:
        svc.supersede_memory(existing.id, owner_agent_name=owner, now=now, changed_by="curator")
        _create()
        return "superseded"
    if decision.action == curator_mod.MERGE:
        svc.update_content(existing.id, decision.merged_content or content,
                           owner_agent_name=owner, now=now, changed_by="curator",
                           reason="merged conflicting memory")
        return "merged"
    if decision.action == curator_mod.NEEDS_APPROVAL:
        _defer_conflict(db, owner, existing, content, memory_type, scope, sim, now)
        return "deferred"
    _create()                                  # CREATE / keep-both (refinement)
    return "kept_both"


def _defer_conflict(db, owner, existing, content, memory_type, scope, sim, now) -> None:
    """No curator → surface the conflict for human resolution in the Memory
    page (a curator-flagged candidate carrying both sides)."""
    from services.memory import candidate_service as cnd
    cnd.create_candidate(
        db, owner_agent_name=owner, candidate_type="conflict",
        payload={"memory_type": memory_type, "content": content,
                 "subject_scope": scope, "authority": "user_confirmed",
                 "conflicts_with": existing.id,
                 "existing_content": existing.content, "similarity": round(sim, 3)},
        now=now, requires_curator=True, requires_approval=True)
