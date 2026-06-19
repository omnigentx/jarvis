"""Knowledge-graph triple extraction (memory v2 relations layer).

Turns a memory statement into (subject, predicate, object) triples — e.g.
"người dùng thích ăn phở" → {"s":"Người dùng","p":"thích","o":"phở"} — stored on
``MemoryRecord.relations_json`` (SQLite = SoT) and projected to LadybugDB as
RELATES edges so the graph view is a real knowledge graph, not opaque blobs.

Best-effort: a missing/failed LLM yields no triples; never raises into callers.
"""
from __future__ import annotations

import asyncio
import json
import logging

from services.memory.fast_extractor import build_extractor_generate_fn

logger = logging.getLogger("memory.kg")

TRIPLE_PROMPT = """You extract KNOWLEDGE-GRAPH triples from one statement about a user, for a personal assistant.

Return ONLY a JSON array (no prose, no code fence). Each item:
{"s": "<subject>", "p": "<short predicate>", "o": "<object>"}

Rules:
- Subject ``s`` is almost always "Người dùng" (the user). Use a named entity only when the statement is really about that entity (e.g. an employer's address).
- Predicate ``p`` is a SHORT relationship phrase: thích / không thích / làm việc tại / sống ở / có / học / chơi / dùng / quan tâm đến …
- Object ``o`` is a CONCISE noun/entity (phở, Techcombank, Gia Lâm, con trai, guitar). Strip explanatory clauses — keep just the entity.
- Split a statement with several facts into several triples.
- Keep the statement's language.

If nothing extractable, return exactly: []
"""


def parse_triples(raw: str) -> list[dict]:
    """Tolerant parse of the LLM's JSON array → list of {s,p,o} (strings only)."""
    if not raw:
        return []
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        items = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return []
    out: list[dict] = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        s, p, o = (str(it.get(k, "")).strip() for k in ("s", "p", "o"))
        if s and p and o:
            out.append({"s": s, "p": p, "o": o})
    return out


# The user is the implicit subject of almost every personal memory — a super-node.
# Excluding it from MENTIONS keeps co-occurrence meaningful: two memories are
# "related" because they share a real entity (Acme, pho), not because both are
# about the user (which would link the entire graph into one noisy cluster).
_GENERIC_SUBJECTS = {"user", "người dùng", "nguoi dung", "the user"}


def _entities_from_triples(triples: list[dict]) -> list[dict]:
    """Entities a memory MENTIONS, derived from its triples: the objects + any
    non-generic subject (the user super-node is excluded — see above)."""
    seen: set[str] = set()
    out: list[dict] = []
    for t in triples or []:
        for v in (t.get("o"), t.get("s")):
            v = (v or "").strip()
            if not v or v.lower() in _GENERIC_SUBJECTS:
                continue
            k = v.lower()
            if k not in seen:
                seen.add(k)
                out.append({"name": v, "etype": "topic"})
    return out


async def extract_triples(content: str, *, generate_fn=None) -> list[dict]:
    """Triples for ONE memory statement (best-effort)."""
    if not (content or "").strip():
        return []
    generate_fn = generate_fn or build_extractor_generate_fn("memory:kg")
    if generate_fn is None:
        return []
    try:
        raw = await generate_fn(TRIPLE_PROMPT + "\n\nStatement:\n" + content)
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("[MEMORY] KG triple extraction failed: %s", exc)
        return []
    return parse_triples(raw)


async def extract_and_store(record_id: str, *, generate_fn=None) -> bool:
    """AUTHORITATIVE projection of a memory's graph: extract its triples and write
    BOTH ``relations_json`` (the triples) and ``entities_json`` (entities DERIVED
    from those triples), then force a re-index so the worker rebuilds RELATES +
    MENTIONS. This is the single deterministic writer — it ALWAYS overwrites with
    the triple-derived value, so even though the capture lane may seed
    ``entities_json`` first, the final state is one well-defined source (no
    conditional clobber → RC3 fixed). The capture seed survives only as a
    fallback if extraction fails.

    Called fire-and-forget right after a memory is persisted (every lane,
    including the agent's free-text `remember`), so every memory enters the
    knowledge graph deterministically — NOT via a debounced owner-wide rescan.
    Best-effort: a missing/failed LLM leaves the memory un-graphed (retried by
    the startup migration); never raises into the caller. Returns True if
    triples were stored."""
    import time

    from core.database import MemoryRecord, get_db_session
    from services.indexing import outbox_service as ob

    generate_fn = generate_fn or build_extractor_generate_fn("memory:kg")
    if generate_fn is None:
        return False
    db = get_db_session()
    try:
        rec = db.get(MemoryRecord, record_id)
        if rec is None or rec.status != "active":
            return False
        triples = await extract_triples(rec.content, generate_fn=generate_fn)
        # relations_json: "[]" is a TERMINAL "attempted, no triples" state (not
        # re-extracted) — distinct from NULL "never attempted" (a failed LLM
        # returns early below WITHOUT committing, so NULL is preserved → retried).
        rec.relations_json = json.dumps(triples, ensure_ascii=False)
        # entities_json: only OVERWRITE when extraction actually found something —
        # an empty result must NOT clobber the entity list seeded at capture time
        # (the documented fallback). M4.
        if triples:
            rec.entities_json = json.dumps(_entities_from_triples(triples), ensure_ascii=False)
        ob.enqueue(db, event_type=ob.EVENT_MEMORY_UPSERT, aggregate_id=rec.id,
                   aggregate_revision=rec.current_version, now=time.time(), force=True)
        db.commit()
        return bool(triples)
    except Exception as exc:  # noqa: BLE001 — best-effort graph projection
        logger.warning("[MEMORY] KG extract_and_store(%s) failed: %s", record_id, exc)
        return False
    finally:
        db.close()


def schedule_extract_and_store(record_id: str) -> None:
    """Fire-and-forget ``extract_and_store`` on the running loop, if any. Safe to
    call from sync persistence code: in a request/worker context it schedules the
    extraction off the hot path; in tests / sync migrations (no loop) it's a
    no-op (the startup migration backfills those)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(extract_and_store(record_id))


async def backfill_relations(owner: str | None = None, *, force: bool = False,
                             generate_fn=None, concurrency: int = 5) -> int:
    """MIGRATION / repair only (startup + manual rebuild): (re)extract triples for
    active memories that lack them. Steady-state graphing is per-memory via
    ``extract_and_store`` at persist time, so this is NOT on the hot path. By
    default only fills memories with no ``relations_json`` yet; ``force=True``
    re-extracts everything. Concurrency-bounded. Returns the count (re)processed."""
    import time

    from core.database import MemoryRecord, get_db_session
    from services.indexing import outbox_service as ob

    generate_fn = generate_fn or build_extractor_generate_fn("memory:kg")
    if generate_fn is None:
        return 0

    db = get_db_session()
    try:
        q = db.query(MemoryRecord).filter(MemoryRecord.status == "active")
        if owner:
            q = q.filter(MemoryRecord.owner_agent_name == owner)
        if not force:
            q = q.filter(MemoryRecord.relations_json.is_(None))
        rows = q.all()
        if not rows:
            return 0

        sem = asyncio.Semaphore(max(1, concurrency))

        async def _one(rec):
            async with sem:
                triples = await extract_triples(rec.content, generate_fn=generate_fn)
            return rec, triples

        results = await asyncio.gather(*[_one(r) for r in rows])
        now = time.time()
        for rec, triples in results:
            # SAME single-source rule as extract_and_store: triples → relations_json;
            # entities DERIVED from triples → entities_json, but only overwrite when
            # non-empty so an empty re-extract doesn't clobber a capture seed (M4).
            rec.relations_json = json.dumps(triples, ensure_ascii=False)
            if triples:
                rec.entities_json = json.dumps(_entities_from_triples(triples), ensure_ascii=False)
            ob.enqueue(db, event_type=ob.EVENT_MEMORY_UPSERT, aggregate_id=rec.id,
                       aggregate_revision=rec.current_version, now=now, force=True)
        db.commit()
        logger.info("[MEMORY] KG backfill: %d memories (owner=%s, force=%s)",
                    len(rows), owner or "*", force)
        return len(rows)
    finally:
        db.close()
