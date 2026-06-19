"""SQLite FTS5 maintenance + search (degraded fallback / admin / consistency
reference — never the production path, which is Qdrant).

One virtual table ``memory_fts`` (created in core.database.init_db) fed from
both episodic_documents and memory_records. FTS5 has no key-based UPDATE, so
upsert = delete-then-insert on (doc_kind, doc_id).
"""
from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.orm import Session

KIND_EPISODIC = "episodic"
KIND_MEMORY = "memory"


def fts_upsert(db: Session, *, doc_kind: str, doc_id: str,
               owner_agent_name: str, content: str) -> None:
    db.execute(
        text("DELETE FROM memory_fts WHERE doc_kind = :k AND doc_id = :i"),
        {"k": doc_kind, "i": doc_id},
    )
    db.execute(
        text(
            "INSERT INTO memory_fts (doc_kind, doc_id, owner_agent_name, content) "
            "VALUES (:k, :i, :o, :c)"
        ),
        {"k": doc_kind, "i": doc_id, "o": owner_agent_name, "c": content},
    )


def fts_delete(db: Session, *, doc_kind: str, doc_id: str) -> None:
    db.execute(
        text("DELETE FROM memory_fts WHERE doc_kind = :k AND doc_id = :i"),
        {"k": doc_kind, "i": doc_id},
    )


# Function words carry no retrieval signal but survive the len>1 filter and, in
# an OR-match, fire on any memory containing them — injecting off-topic hits.
# Dropping them is the FTS-side half of the off-topic relevance guarantee: it
# holds even when the dense lane is DOWN (so the orchestrator gate can't run).
# Conservative VI + EN particles/pronouns/articles only — never content words.
_STOPWORDS = frozenset({
    # Vietnamese
    "gì", "là", "của", "và", "có", "thì", "mà", "ở", "cho", "với", "các",
    "những", "này", "đó", "ạ", "ừ", "à", "ơi", "nhé", "vậy", "thế", "rồi", "đi",
    "được", "bị", "khi", "nếu", "hay", "hoặc", "cũng", "đã", "sẽ", "đang",
    # English
    "the", "a", "an", "is", "are", "was", "were", "be", "to", "of", "and", "or",
    "in", "on", "at", "for", "with", "this", "that", "it", "as", "do", "does",
})


def _safe_match_expr(query: str) -> str | None:
    """Build a safe FTS5 MATCH expression from arbitrary user text. Raw text
    can contain FTS5 operators that raise a syntax error; we extract word
    tokens and OR them as quoted phrases (recall-friendly, never crashes).

    Drop pure-number, single-character, and stopword (function-word) tokens: in
    an OR-match they fire on incidental matches in memories (query "2 cộng 2" →
    token "2" → matches "...2km..."; "thời tiết gì" → "gì" → any memory) and
    inject off-topic hits. They carry no retrieval signal; the dense lane carries
    semantic relevance. If nothing meaningful remains, FTS returns nothing."""
    tokens = [t for t in re.findall(r"\w+", query, flags=re.UNICODE)
              if len(t) > 1 and not t.isdigit() and t.lower() not in _STOPWORDS]
    if not tokens:
        return None
    return " OR ".join(f'"{t}"' for t in tokens)


def fts_search(db: Session, *, owner_agent_name: str, query: str,
               limit: int = 30) -> list[dict]:
    """Owner-scoped FTS5 search. Returns rows ordered by bm25 rank
    (best first). The owner filter is mandatory and applied in SQL."""
    match_expr = _safe_match_expr(query)
    if match_expr is None:
        return []
    rows = db.execute(
        text(
            "SELECT doc_kind, doc_id, content, rank FROM memory_fts "
            "WHERE memory_fts MATCH :q AND owner_agent_name = :o "
            "ORDER BY rank LIMIT :n"
        ),
        {"q": match_expr, "o": owner_agent_name, "n": limit},
    ).fetchall()
    return [
        {"doc_kind": r[0], "doc_id": r[1], "content": r[2], "rank": r[3]}
        for r in rows
    ]
