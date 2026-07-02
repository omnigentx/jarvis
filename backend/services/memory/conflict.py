"""LLM-judged conflict resolution at save time (rare, background, lossless).

ADD-only capture (``candidate_service``) deliberately keeps every fact and never
supersedes at write time — a changed fact (AcmeCorp→NovaCorp) keeps BOTH, dated,
and recency-weighted ranking picks the winner at READ time. That breaks down for
a NEW fact that DIRECTLY contradicts an existing one on the same single-valued
attribute (current employer, home city, name) when both were saved close in time:
recency can't separate them, so recall surfaces both and the agent can only ask
"which is right?" instead of answering.

This module closes that narrow gap. It is the resolver the ``fast_extractor``
docstring already named (``conflict.build_curator``). Design:

  • RARE by a cheap gate: an embedding near-neighbour check fires the LLM ONLY
    when a semantically-close prior fact in the same slot exists. Most saves have
    no near-neighbour → zero LLM cost. The gate threshold is intentionally loose
    (catch candidates); the LLM is the precise decider, so a false gate hit just
    costs one judged "no conflict", never a wrong supersede.
  • ONE small LLM call decides if it is a REAL same-attribute contradiction and
    which existing fact the new one replaces. Facts that can coexist (two skills,
    a second job, unrelated facts) are NOT superseded.
  • LOSSLESS: we ``supersede`` (status change, row + version history kept,
    restorable) — never delete. A wrong call is recoverable, never data loss.
  • OFF THE HOT PATH: scheduled fire-and-forget at persist time (mirrors
    ``knowledge_graph.schedule_extract_and_store``), so it never blocks the write
    or the agent's reply. The next recall simply sees a cleaned slot.

Defence-in-depth: even if this misses (LLM unavailable, gate too tight), the
recall block now stamps each memory's created date (``retrieval_hook``), so the
agent can still recency-tiebreak two conflicting memories itself.

``generate_fn``/``embed_fn`` are injectable so tests drive a scripted LLM +
deterministic vectors with no network and no model load.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

logger = logging.getLogger("memory.conflict")

# Loose gate: only PURPOSE is to make the LLM call rare by skipping saves with no
# semantically-close sibling. The LLM is the real judge, so erring low here is
# safe (a false hit → one "no conflict" verdict), erring high would miss real
# conflicts. 0.45 cosine catches "works at X" vs "works at Y" comfortably.
_GATE_SIM = 0.45
# Same-slot active memories to embed-compare per save (bounded cost). For
# personal memory a single (owner, scope, type) slot is small; if a slot ever
# grows past this, switch the gate to the dense index (vector search) — noted so
# the next reader doesn't have to rediscover it.
_MAX_CANDIDATES_SCAN = 40
# Top near-neighbours actually handed to the LLM (keeps the prompt small).
_MAX_CANDIDATES_LLM = 5

_CONFLICT_PROMPT = """You decide whether a NEW memory makes an EXISTING memory OBSOLETE.

Supersede ONLY when they describe the SAME single-valued attribute of the user
(e.g. current employer, home city, current role, name) and CANNOT both be true
at once — the NEW one replaces the EXISTING one.

Do NOT supersede when the two can coexist: different attributes, two skills, a
second job, a past-vs-present pair that's still worth keeping, or merely related
facts. When unsure, do NOT supersede.

NEW (just saved):
{new}

EXISTING (older), by number:
{existing}

Return ONLY JSON, no prose, no code fence:
{{"superseded": [<numbers of EXISTING that the NEW one replaces>]}}
Empty list if none.
"""


def _shared_embed(texts: list[str]) -> list[list[float]]:
    # Pass the CONFIGURED embedding model (not the bge-m3 default): the gate's
    # vectors must share the SAME space as the indexed memories. With no args the
    # shared provider defaults to BAAI/bge-m3 while the memories were embedded with
    # Qwen3-Embedding-0.6B — the cosine gate would then be meaningless and silently
    # disable conflict resolution on the default config (PR #120 review). Mirrors
    # orchestrator.py / memory_index_worker.py, and reuses the same warm singleton.
    from services.indexing.embedding_provider import get_shared_embedding_provider
    from services.memory.settings import get_memory_settings
    cfg = get_memory_settings()
    return get_shared_embedding_provider(
        cfg.embedding_model, cfg.embedding_revision).embed_documents(texts)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _top(scored: list) -> list:
    """Apply the similarity floor + top-N cap. Shared by both gate paths."""
    scored = [(c, s) for c, s in scored if s >= _GATE_SIM]
    scored.sort(key=lambda t: t[1], reverse=True)
    return [c for c, _ in scored[:_MAX_CANDIDATES_LLM]]


def _stored_scores(cands: list, qv: list[float]) -> dict | None:
    """Similarity of each candidate to ``qv``, read from the ALREADY-STORED dense
    vectors (LadybugDB HNSW) — NO re-embed. Returns ``{record_id: cosine_sim}`` for
    the candidates the index knows, or ``None`` if the dense index is unavailable
    (indexing lag / dense outage) so the caller can fall back to embedding.

    This is the "switch the gate to the dense index" the module header anticipated.
    The old path re-embedded the WHOLE (owner, scope, type) slot on every save —
    one batch padded to the longest fact, so cost was O(N x max_len). On a small
    real store that measured ~150s of pure CPU, and (running on the event loop via
    ``schedule_resolve_conflicts``) it stalled the whole turn → the chat SSE hung
    and the UI showed "Unknown error". Here the caller embeds only the NEW fact and
    we reuse the candidates' stored vectors instead."""
    if not cands:
        return {}
    try:
        from services.indexing.ladybug_store import get_ladybug_store
        from services.memory.settings import get_memory_settings
        store = get_ladybug_store(get_memory_settings().ladybug_path)
        # Over-fetch: the near neighbours we care about are the same-slot cands, so
        # ask for at least as many hits as there are candidates.
        hits = store.vector_search(owner=cands[0].owner_agent_name,
                                   query_embedding=qv,
                                   limit=max(len(cands), _MAX_CANDIDATES_SCAN))
    except Exception as exc:  # noqa: BLE001 — dense outage → embedding fallback
        logger.warning("[MEMORY] conflict gate dense lookup failed (%s) — "
                       "embedding fallback", exc)
        return None
    if not hits:
        return None
    # VectorHit.distance is cosine DISTANCE; similarity = 1 - distance.
    return {h.record_id: 1.0 - h.distance for h in hits}


def _gate_by_embed(new_content: str, cands: list, embed_fn) -> list:
    """Score candidates by RE-EMBEDDING them. Used for the injected-``embed_fn``
    tests and as the fallback when the dense index is unavailable. Kept as the
    original single batch call so scripted-``embed_fn`` tests are unchanged."""
    try:
        vecs = embed_fn([new_content] + [c.content for c in cands])
    except Exception as exc:  # noqa: BLE001 — gate is best-effort
        logger.warning("[MEMORY] conflict gate embed failed: %s", exc)
        return []
    if not vecs or len(vecs) != len(cands) + 1:
        return []
    qv = vecs[0]
    return _top([(c, _cosine(qv, v)) for c, v in zip(cands, vecs[1:])])


def _gate(new_content: str, cands: list, embed_fn) -> list:
    """Keep same-slot candidates whose similarity to the new fact clears the gate,
    top-N. Any failure → no candidates (skip the LLM), never raise.

    Production (``embed_fn is None``): embed ONLY the new fact, then score the
    candidates against their stored vectors via the dense index (:func:`_stored_scores`)
    — see that function for why the old whole-slot re-embed was removed. An injected
    ``embed_fn`` (tests / explicit) keeps the direct embed path; the dense path also
    falls back to it when the index is unavailable."""
    if embed_fn is not None:
        return _gate_by_embed(new_content, cands, embed_fn)
    if not cands:
        return []
    try:
        qv = _shared_embed([new_content])[0]
    except Exception as exc:  # noqa: BLE001 — gate is best-effort
        logger.warning("[MEMORY] conflict gate embed failed: %s", exc)
        return []
    scores = _stored_scores(cands, qv)
    if scores is None:                    # dense index unavailable → re-embed fallback
        return _gate_by_embed(new_content, cands, _shared_embed)
    return _top([(c, scores.get(c.id, 0.0)) for c in cands])


def _parse_superseded(raw: str, n: int) -> list[int]:
    """Tolerant parse of ``{"superseded": [..]}`` → 1-based indices in [1, n].
    Ignores fences/prose; a malformed reply yields [] (no supersede)."""
    if not raw:
        return []
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return []
    try:
        obj = json.loads(m.group(0))
    except (ValueError, TypeError):
        return []
    out: list[int] = []
    for v in (obj.get("superseded") or []):
        try:
            i = int(v)
        except (ValueError, TypeError):
            continue
        if 1 <= i <= n and i not in out:
            out.append(i)
    return out


async def _judge(new_content: str, cands: list, generate_fn) -> list[int]:
    existing = "\n".join(f'[{i + 1}] "{c.content}"' for i, c in enumerate(cands))
    prompt = _CONFLICT_PROMPT.format(new=f'"{new_content}"', existing=existing)
    raw = await generate_fn(prompt)
    return _parse_superseded(raw, len(cands))


async def resolve_conflicts(record_id: str, *, generate_fn=None, embed_fn=None,
                            now: float | None = None) -> list[str]:
    """Resolve same-slot contradictions for a just-saved memory. Returns the
    record_ids that were superseded (empty when nothing conflicts). Best-effort:
    any failure logs and returns [] — the ADD-only fact stays, nothing breaks."""
    from services.memory.fast_extractor import build_extractor_generate_fn
    generate_fn = generate_fn or build_extractor_generate_fn("memory:conflict")
    if generate_fn is None:
        return []  # no agent context (boot/tests) → no-op, like KG extraction
    from core.database import MemoryRecord, get_db_session
    db = get_db_session()
    try:
        rec = db.get(MemoryRecord, record_id)
        if rec is None or rec.status != "active":
            return []
        # Candidate slot = same owner + same subject_scope + same memory_type.
        # Scope alone is coarse (all 'user' facts share it), so memory_type +
        # the embedding gate below do the narrowing; the LLM is the final judge.
        cands = (db.query(MemoryRecord)
                 .filter(MemoryRecord.owner_agent_name == rec.owner_agent_name,
                         MemoryRecord.subject_scope == rec.subject_scope,
                         MemoryRecord.memory_type == rec.memory_type,
                         MemoryRecord.status == "active",
                         MemoryRecord.id != rec.id)
                 .order_by(MemoryRecord.created_at.desc())
                 .limit(_MAX_CANDIDATES_SCAN).all())
        if not cands:
            return []
        # Off the event loop: the gate does CPU-bound embedding / dense lookups.
        # schedule_resolve_conflicts runs this as a loop task, so a synchronous
        # embed here would freeze the WHOLE loop (every embed releases the GIL for
        # C++ compute but the coroutine never yields), stalling the live chat turn's
        # SSE until it finished. to_thread keeps the loop responsive.
        gated = await asyncio.to_thread(_gate, rec.content, cands, embed_fn)
        if not gated:
            return []
        idxs = await _judge(rec.content, gated, generate_fn)
        if not idxs:
            return []
        from services.memory.memory_service import MemoryService
        svc = MemoryService(db)
        superseded: list[str] = []
        for i in idxs:
            cid = gated[i - 1].id
            try:
                svc.supersede_memory(cid, owner_agent_name=rec.owner_agent_name, now=now)
                superseded.append(cid)
            except Exception as exc:  # noqa: BLE001 — one bad supersede never drops the rest
                logger.warning("[MEMORY] conflict supersede(%s) failed: %s", cid, exc)
        if superseded:
            logger.info("[MEMORY] conflict resolved: new=%s superseded=%s",
                        rec.id, superseded)
        return superseded
    except Exception as exc:  # noqa: BLE001 — best-effort, never break a capture
        logger.warning("[MEMORY] resolve_conflicts(%s) failed: %s", record_id, exc)
        return []
    finally:
        db.close()


def schedule_resolve_conflicts(record_id: str) -> None:
    """Fire-and-forget ``resolve_conflicts`` on the running loop, if any. Safe to
    call from sync persistence code: in a request/worker context it runs the
    (rare) LLM judgement off the hot path; in tests / sync migrations (no loop)
    it's a no-op."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(resolve_conflicts(record_id))
