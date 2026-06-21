"""Migration e2e: LadybugDB empty + SQLite (SoT) has memories → rebuild →
worker repopulates the graph. This is the path server.py runs on first boot
after switching the vector backend to LadybugDB.
"""
import tempfile
import types

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

pytest.importorskip("ladybug")
from core.database import Base, MemoryRecord  # noqa: E402
from services.indexing import consistency_service as cs  # noqa: E402
from services.indexing.ladybug_store import EMBED_DIM, LadybugIndexer, LadybugStore  # noqa: E402
from services.indexing.memory_index_worker import MemoryIndexWorker  # noqa: E402


class _FakeEmb:
    def is_available(self): return True
    def dim(self): return EMBED_DIM
    def revision(self): return "fake"
    def embed_documents(self, texts):
        return [[1.0] + [0.0] * (EMBED_DIM - 1) for _ in texts]


async def test_rebuild_repopulates_ladybug_from_sqlite(monkeypatch):
    eng = create_engine("sqlite:///:memory:",
                        connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    with eng.connect() as c:
        c.execute(text("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5("
                       "doc_kind UNINDEXED, doc_id UNINDEXED, owner_agent_name UNINDEXED, content)"))
        c.commit()
    F = sessionmaker(bind=eng)
    store = LadybugStore(f"{tempfile.mkdtemp()}/g")

    # SoT has 2 active memories; the graph is EMPTY (the migration precondition).
    db = F()
    for rid in ("m1", "m2"):
        db.add(MemoryRecord(id=rid, owner_agent_name="Jarvis", memory_type="semantic",
                            subject_scope="user", content=f"fact {rid}", normalized_content=f"fact {rid}",
                            status="active", authority="user_confirmed", confidence=0.9,
                            current_version=1, created_at=1.0))
    db.commit()
    db.close()
    assert store.count() == 0

    # Migration: rebuild enqueues all → the LadybugDB-backed worker drains them.
    db = F()
    n = cs.rebuild(db, now=10.0)
    db.commit(); db.close()
    assert n == 2

    import services.indexing.memory_index_worker as wmod
    monkeypatch.setattr(wmod, "SessionLocal", F)
    worker = MemoryIndexWorker()
    monkeypatch.setattr(worker, "_dense", lambda: LadybugIndexer(store))
    monkeypatch.setattr(worker, "_emb", lambda: _FakeEmb())
    stats = await worker.process_pending(now=20.0)

    assert stats["done"] == 2
    assert store.count("Jarvis") == 2                        # graph repopulated from SoT
    store.close()
