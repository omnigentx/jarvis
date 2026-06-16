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


async def backfill_relations(owner: str | None = None, *, force: bool = False,
                             generate_fn=None, concurrency: int = 5) -> int:
    """Extract + store triples for active memories, then re-index so the worker
    projects RELATES edges. By default only fills memories that have no
    ``relations_json`` yet (so it's cheap to call repeatedly / on every new
    memory); ``force=True`` re-extracts everything. Returns the number of
    memories (re)processed.

    Concurrency-bounded LLM calls so a backfill of N memories takes ~ceil(N/k)
    round-trips, not N sequential ones.
    """
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
            rec.relations_json = json.dumps(triples, ensure_ascii=False)
            # force re-index so the worker re-projects this memory (writing the
            # RELATES edges) even though its outbox row is already 'done'.
            ob.enqueue(db, event_type=ob.EVENT_MEMORY_UPSERT, aggregate_id=rec.id,
                       aggregate_revision=rec.current_version, now=now, force=True)
        db.commit()
        logger.info("[MEMORY] KG backfill: %d memories (owner=%s, force=%s)",
                    len(rows), owner or "*", force)
        return len(rows)
    finally:
        db.close()
