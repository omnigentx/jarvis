"""WS03 deterministic core: chunker, FTS5 maintenance/search, projector."""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.database import Base, EpisodicDocument, MemoryIndexOutbox
from services.indexing import chunker, fts_index, projector
from services.indexing import outbox_service as ob


# ── chunker (pure) ──

def test_chunk_atomic_kinds_single_chunk():
    for kind in (chunker.DOC_MESSAGE, chunker.DOC_EMAIL, chunker.DOC_FACT, chunker.DOC_MEETING):
        assert chunker.chunk_document(kind, "hello world") == ["hello world"]


def test_chunk_empty_is_empty():
    assert chunker.chunk_document(chunker.DOC_MESSAGE, "   ") == []


def test_chunk_prose_splits_with_overlap():
    # ~250 tokens/paragraph × 6 ≈ 1500 tokens → forced to split (target 600).
    paras = [f"Paragraph number {i} " + ("word " * 200) for i in range(6)]
    text_in = "\n\n".join(paras)
    chunks = chunker.chunk_document(chunker.DOC_PROSE, text_in)
    assert len(chunks) >= 2                       # split into multiple
    assert all(chunker.estimate_tokens(c) <= 800 for c in chunks)


def test_chunk_by_headings():
    md = "intro text\n# Title\nbody a\n## Sub\nbody b"
    chunks = chunker.chunk_document(chunker.DOC_RUNBOOK, md)
    assert chunks[0] == "intro text"
    assert any(c.startswith("# Title") for c in chunks)
    assert any(c.startswith("## Sub") for c in chunks)


def test_tool_output_truncated_with_artifact_ref():
    big = "x" * 5000
    [chunk] = chunker.chunk_document(chunker.DOC_TOOL_OUTPUT, big, artifact_ref="run/42.json")
    assert "[truncated]" in chunk
    assert "run/42.json" in chunk
    assert len(chunk) < 5000


# ── fts + projector (in-memory DB with FTS5) ──

@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.connect() as c:
        c.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5("
            "doc_kind UNINDEXED, doc_id UNINDEXED, owner_agent_name UNINDEXED, content)"
        ))
        c.commit()
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def test_fts_upsert_search_owner_scoped(db):
    fts_index.fts_upsert(db, doc_kind="episodic", doc_id="d1",
                         owner_agent_name="Jarvis", content="deploy verification runbook")
    fts_index.fts_upsert(db, doc_kind="episodic", doc_id="d2",
                         owner_agent_name="Riley [SA]", content="deploy verification runbook")
    db.commit()
    res = fts_index.fts_search(db, owner_agent_name="Jarvis", query="deploy runbook")
    assert [r["doc_id"] for r in res] == ["d1"]   # owner filter excludes Riley's doc


def test_fts_special_chars_dont_crash(db):
    fts_index.fts_upsert(db, doc_kind="memory", doc_id="m1",
                         owner_agent_name="Jarvis", content="error: NULL pointer at foo.py:42")
    db.commit()
    res = fts_index.fts_search(db, owner_agent_name="Jarvis", query='foo.py:42 (NULL)')
    assert res and res[0]["doc_id"] == "m1"


def test_fts_upsert_replaces(db):
    fts_index.fts_upsert(db, doc_kind="episodic", doc_id="d1", owner_agent_name="J", content="old text")
    fts_index.fts_upsert(db, doc_kind="episodic", doc_id="d1", owner_agent_name="J", content="new text")
    db.commit()
    assert fts_index.fts_search(db, owner_agent_name="J", query="old") == []
    assert fts_index.fts_search(db, owner_agent_name="J", query="new")


def test_projector_dedupes_identical_content(db):
    d1 = projector.project_episodic(db, owner_agent_name="Jarvis",
                                    document_type="message", source_id="s:1",
                                    content="we decided to use a dedicated compactor", now=100.0)
    db.commit()
    assert d1 is not None
    d2 = projector.project_episodic(db, owner_agent_name="Jarvis",
                                    document_type="message", source_id="s:2",
                                    content="we decided   to use a dedicated   compactor", now=200.0)
    assert d2 is None                              # whitespace-normalized dup
    assert db.query(EpisodicDocument).count() == 1


def test_project_and_enqueue_atomic(db):
    doc = projector.project_and_enqueue(db, owner_agent_name="Jarvis",
                                        document_type="message", source_id="s:1",
                                        content="hello decision", now=100.0)
    db.commit()
    assert doc is not None
    row = db.query(MemoryIndexOutbox).one()
    assert row.event_type == ob.EVENT_EPISODIC_UPSERT
    assert row.aggregate_id == doc.id
    assert row.aggregate_revision == 1
