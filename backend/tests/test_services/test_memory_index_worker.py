"""WS03 worker + consistency: drain happy path, degraded defer, rebuild,
retention. Real SQLite (in-memory shared) + FTS5; dense/embeddings faked."""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import (
    Base, EpisodicDocument, MemoryIndexOutbox, MemoryRecord, MemorySource, RetrievalRun,
)
from services.indexing import consistency_service as cs
from services.indexing import fts_index
from services.indexing import outbox_service as ob
from services.indexing import memory_index_worker as wmod
from services.indexing.memory_index_worker import MemoryIndexWorker


@pytest.fixture()
def Session(monkeypatch):
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with engine.connect() as c:
        c.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5("
            "doc_kind UNINDEXED, doc_id UNINDEXED, owner_agent_name UNINDEXED, content)"))
        c.commit()
    SessionFactory = sessionmaker(bind=engine)
    monkeypatch.setattr(wmod, "SessionLocal", SessionFactory)
    return SessionFactory


class _FakeDenseIndexer:
    def __init__(self, available=True):
        self._available = available
        self.points = []
        self.deleted = []
    def is_available(self): return self._available
    def ensure_collection(self): pass
    def upsert_points(self, points): self.points.extend(points)
    def delete_by_record(self, rid): self.deleted.append(rid)


class _FakeEmbedding:
    def is_available(self): return True
    def dim(self): return 4
    def revision(self): return "bge-rev"
    def embed_documents(self, texts): return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


def _seed_episodic(Session, now=100.0):
    db = Session()
    doc = EpisodicDocument(
        id="doc-1", owner_agent_name="Jarvis", document_type="message",
        source_id="s:1", content="we decided to use a dedicated compactor agent",
        metadata_json="{}", content_hash="h1", created_at=now, indexed_revision=0)
    db.add(doc)
    ob.enqueue(db, event_type=ob.EVENT_EPISODIC_UPSERT, aggregate_id="doc-1",
               aggregate_revision=1, now=now)
    db.commit()
    db.close()


async def test_worker_happy_path_indexes_dense_and_fts(Session, monkeypatch):
    _seed_episodic(Session)
    worker = MemoryIndexWorker()
    fake_dense = _FakeDenseIndexer(available=True)
    monkeypatch.setattr(worker, "_dense", lambda: fake_dense)
    monkeypatch.setattr(worker, "_emb", lambda: _FakeEmbedding())

    stats = await worker.process_pending(now=200.0)
    assert stats == {"done": 1, "deferred": 0, "failed": 0}

    db = Session()
    # outbox done; episodic indexed_revision bumped
    assert db.query(MemoryIndexOutbox).one().status == ob.DONE
    assert db.get(EpisodicDocument, "doc-1").indexed_revision == 1
    # dense point upserted with correct payload
    assert len(fake_dense.points) == 1
    p = fake_dense.points[0]
    assert p["payload"]["owner_agent_name"] == "Jarvis"
    assert p["payload"]["memory_type"] == "episodic"
    # FTS searchable
    assert fts_index.fts_search(db, owner_agent_name="Jarvis", query="compactor")
    db.close()


async def test_worker_degraded_defers_dense_keeps_fts(Session, monkeypatch):
    _seed_episodic(Session)
    worker = MemoryIndexWorker()
    monkeypatch.setattr(worker, "_dense", lambda: _FakeDenseIndexer(available=False))
    monkeypatch.setattr(worker, "_emb", lambda: _FakeEmbedding())

    stats = await worker.process_pending(now=200.0)
    assert stats == {"done": 0, "deferred": 1, "failed": 0}

    db = Session()
    row = db.query(MemoryIndexOutbox).one()
    assert row.status == ob.PENDING                  # deferred, not failed
    assert row.attempt_count == 0                    # defer must NOT count as failure
    assert row.next_attempt_at == 200.0 + MemoryIndexWorker.DEGRADED_DEFER_S
    # FTS still updated for degraded search
    assert fts_index.fts_search(db, owner_agent_name="Jarvis", query="compactor")
    db.close()


def test_rebuild_enqueues_all(Session):
    db = Session()
    for i in range(2):
        db.add(MemoryRecord(id=f"m{i}", owner_agent_name="Jarvis", memory_type="semantic",
                            subject_scope="project:jarvis", content="c", normalized_content="c",
                            status="active", authority="user_confirmed", current_version=1))
    db.add(EpisodicDocument(id="e1", owner_agent_name="Jarvis", document_type="message",
                            source_id="s", content="c", content_hash="h", created_at=1.0))
    db.commit()
    n = cs.rebuild(db, now=10.0)
    assert n == 3
    assert db.query(MemoryIndexOutbox).count() == 3
    # A rebuild FORCE-requeues (2026-06-16 migration fix): a second rebuild
    # resets the existing rows to pending so a backend switch re-projects
    # already-``done`` records — it re-queues the same 3 in place, never
    # duplicating them. (Previously this returned 0, which is exactly why
    # pre-LadybugDB memories never reached the graph.)
    assert cs.rebuild(db, now=11.0) == 3
    assert db.query(MemoryIndexOutbox).count() == 3
    assert {r.next_attempt_at for r in db.query(MemoryIndexOutbox)} == {11.0}
    db.close()


def test_retention_prunes_runs_and_unreferenced_episodic(Session):
    db = Session()
    db.add(RetrievalRun(id="r-old", owner_agent_name="J", query_hash="q", mode="balanced",
                        total_ms=1, evidence_tokens=0, created_at=10.0))
    db.add(RetrievalRun(id="r-new", owner_agent_name="J", query_hash="q", mode="balanced",
                        total_ms=1, evidence_tokens=0, created_at=1000.0))
    # one stale episodic referenced by a memory source (keep), one not (prune)
    db.add(EpisodicDocument(id="keep", owner_agent_name="J", document_type="message",
                            source_id="src-keep", content="c", content_hash="h1", created_at=10.0))
    db.add(EpisodicDocument(id="drop", owner_agent_name="J", document_type="message",
                            source_id="src-drop", content="c", content_hash="h2", created_at=10.0))
    db.add(MemorySource(memory_id="m1", memory_version=1, source_type="episodic",
                        source_id="src-keep", authority="user_confirmed", created_at=10.0))
    db.commit()

    assert cs.prune_retrieval_runs(db, older_than=500.0) == 1
    assert {r.id for r in db.query(RetrievalRun).all()} == {"r-new"}

    removed = cs.prune_episodic(db, older_than=500.0, now=600.0)
    assert removed == 1
    assert {d.id for d in db.query(EpisodicDocument).all()} == {"keep"}
    # prune enqueued an index-removal for the dropped doc
    assert db.query(MemoryIndexOutbox).filter_by(event_type=ob.EVENT_EPISODIC_PRUNE).count() == 1
    db.close()


async def test_worker_episodic_prune_removes_dense_and_fts(Session, monkeypatch):
    """B4 regression: draining EVENT_EPISODIC_PRUNE must remove the DENSE node,
    not just FTS. _index_episodic writes a dense point, so a prune that only
    deletes FTS leaves the doc permanently dense-searchable (read-after-prune
    SSoT leak). The retention test above only asserts the outbox row is
    enqueued — it never drained the worker, which is why this slipped through.
    """
    _seed_episodic(Session)
    worker = MemoryIndexWorker()
    fake_dense = _FakeDenseIndexer(available=True)
    monkeypatch.setattr(worker, "_dense", lambda: fake_dense)
    monkeypatch.setattr(worker, "_emb", lambda: _FakeEmbedding())

    # Index first: a dense point + FTS row now exist.
    await worker.process_pending(now=200.0)
    assert len(fake_dense.points) == 1
    assert fts_index.fts_search(Session(), owner_agent_name="Jarvis", query="compactor")

    # Prune it, then drain the worker.
    db = Session()
    ob.enqueue(db, event_type=ob.EVENT_EPISODIC_PRUNE, aggregate_id="doc-1",
               aggregate_revision=2, now=300.0)
    db.commit()
    db.close()
    await worker.process_pending(now=400.0)

    assert "doc-1" in fake_dense.deleted        # dense removed (the B4 fix)
    assert not fts_index.fts_search(Session(), owner_agent_name="Jarvis", query="compactor")
    db.close()


# ── event-driven loop (no fixed polling) ──────────────────────────────────────

async def test_worker_is_event_driven_not_polling(monkeypatch):
    """A huge sleep delay means the ONLY thing that can trigger a second drain is
    a notify — proving the loop is event-driven, not a 2s poll."""
    import asyncio
    w = MemoryIndexWorker()
    calls = []

    async def fake_pp(*, now, limit):
        calls.append(now)
        return {"done": 0, "deferred": 0, "failed": 0}
    monkeypatch.setattr(w, "process_pending", fake_pp)
    monkeypatch.setattr(w, "_sleep_delay", lambda *, now: 9999.0)

    task = asyncio.create_task(w.run_loop())
    await asyncio.sleep(0.05)
    assert len(calls) == 1                      # startup drain, then it WAITS (no poll)
    ob.notify()                                 # post-commit wake
    await asyncio.sleep(0.05)
    assert len(calls) >= 2                      # woke within ms — not after 9999s
    w.stop()
    await asyncio.wait_for(task, timeout=1.0)


async def test_worker_drains_backlog_continuously(monkeypatch):
    """Full batches mean more backlog → the worker keeps draining back-to-back
    WITHOUT waiting for the next notify."""
    import asyncio
    w = MemoryIndexWorker()
    seq = [w.BATCH_LIMIT, w.BATCH_LIMIT, 0]     # two full batches, then drained
    calls = []

    async def fake_pp(*, now, limit):
        n = seq[min(len(calls), len(seq) - 1)]
        calls.append(n)
        return {"done": n, "deferred": 0, "failed": 0}
    monkeypatch.setattr(w, "process_pending", fake_pp)
    monkeypatch.setattr(w, "_sleep_delay", lambda *, now: 9999.0)

    task = asyncio.create_task(w.run_loop())
    await asyncio.sleep(0.05)
    assert len(calls) >= 3                       # 2 full batches drained without a wait
    w.stop()
    await asyncio.wait_for(task, timeout=1.0)


async def test_worker_wakes_at_retry_deadline_when_no_notify(monkeypatch):
    """A deferred/retry row due soon wakes the worker via the deadline path
    (not a notify) — recovers even if a notify were ever lost."""
    import asyncio
    w = MemoryIndexWorker()
    calls = []

    async def fake_pp(*, now, limit):
        calls.append(now)
        return {"done": 0, "deferred": 0, "failed": 0}
    monkeypatch.setattr(w, "process_pending", fake_pp)
    # nearest deadline ~80ms out, no notify will be sent
    import time as _t
    monkeypatch.setattr(w, "_sleep_delay", lambda *, now: 0.08)

    task = asyncio.create_task(w.run_loop())
    await asyncio.sleep(0.25)                    # > one deadline window
    assert len(calls) >= 2                        # re-drained at the deadline, no notify
    w.stop()
    await asyncio.wait_for(task, timeout=1.0)


# ── Fail-loud: a missing/down dense leg must NOT defer silently ──────────────

class _Avail:
    def __init__(self, ok): self._ok = ok
    def is_available(self): return self._ok


def test_note_dense_state_logs_once_then_recovery(caplog):
    import logging
    w = MemoryIndexWorker()
    down_emb, up = _Avail(False), _Avail(True)
    with caplog.at_level(logging.WARNING, logger="memory.index_worker"):
        assert w._note_dense_state(up, down_emb) is False        # embeddings down
        assert w._note_dense_state(up, down_emb) is False        # still down
    warns = [r for r in caplog.records if "DEFERRED" in r.message]
    assert len(warns) == 1                                       # logged ONCE, not silent, not spammy
    assert "embeddings unavailable" in warns[0].message
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="memory.index_worker"):
        assert w._note_dense_state(up, up) is True               # recovered
    assert any("recovered" in r.message for r in caplog.records)


def test_embedding_provider_missing_deps_is_loud(monkeypatch, caplog):
    import logging

    from services.indexing import embedding_provider as ep
    monkeypatch.setattr(ep, "_deps_available", lambda: False)
    monkeypatch.setattr(ep, "_WARNED_MISSING_DEPS", False)
    with caplog.at_level(logging.ERROR, logger="memory.embedding"):
        prov = ep.get_embedding_provider()
    assert prov.is_available() is False
    assert any("FlagEmbedding is NOT installed" in r.message for r in caplog.records)
