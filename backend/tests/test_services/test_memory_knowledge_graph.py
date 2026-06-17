"""KG triple extraction + backfill (memory v2 relations layer)."""
import json

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, MemoryIndexOutbox, MemoryRecord
from services.memory import knowledge_graph as kg


def test_parse_triples_tolerant():
    raw = '[{"s":"user","p":"likes","o":"pho"},{"s":"user","p":"works at","o":"Acme"}]'
    assert kg.parse_triples(raw) == [
        {"s": "user", "p": "likes", "o": "pho"},
        {"s": "user", "p": "works at", "o": "Acme"}]
    assert kg.parse_triples("```json\n[{\"s\":\"a\",\"p\":\"b\",\"o\":\"c\"}]\n```") == [
        {"s": "a", "p": "b", "o": "c"}]
    assert kg.parse_triples("not json") == []
    assert kg.parse_triples('[{"s":"a","p":"","o":"c"}]') == []   # missing predicate dropped


def _fixed_fn(payload):
    async def gen(_prompt):
        return payload
    return gen


def test_entities_from_triples_excludes_user_hub():
    triples = [{"s": "user", "p": "works at", "o": "Acme"},
               {"s": "user", "p": "likes", "o": "pho"},
               {"s": "Acme", "p": "located in", "o": "Hanoi"}]
    names = {e["name"] for e in kg._entities_from_triples(triples)}
    # objects + the non-generic subject "Acme"; the user super-node is excluded
    # so two memories link only when they share a REAL entity.
    assert names == {"Acme", "pho", "Hanoi"}


@pytest.mark.asyncio
async def test_extract_triples_uses_llm():
    out = await kg.extract_triples(
        "user likes pho",
        generate_fn=_fixed_fn('[{"s":"user","p":"likes","o":"pho"}]'))
    assert out == [{"s": "user", "p": "likes", "o": "pho"}]


@pytest.fixture()
def test_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with engine.connect() as c:
        c.execute(text("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5("
                       "doc_kind UNINDEXED, doc_id UNINDEXED, owner_agent_name UNINDEXED, content)"))
        c.commit()
    Factory = sessionmaker(bind=engine)
    import core.database as cd
    monkeypatch.setattr(cd, "get_db_session", lambda: Factory())
    return Factory


@pytest.mark.asyncio
async def test_backfill_populates_relations_and_reindexes(test_db):
    db = test_db()
    db.add(MemoryRecord(id="m1", owner_agent_name="Jarvis", memory_type="semantic",
                        subject_scope="user", content="user likes pho",
                        normalized_content="user likes pho", status="active",
                        authority="agent_observed", confidence=0.9, current_version=1,
                        created_at=1.0, updated_at=1.0))
    db.commit(); db.close()

    n = await kg.backfill_relations(
        "Jarvis", generate_fn=_fixed_fn('[{"s":"user","p":"likes","o":"pho"}]'))
    assert n == 1
    db = test_db()
    rec = db.query(MemoryRecord).one()
    assert json.loads(rec.relations_json) == [{"s": "user", "p": "likes", "o": "pho"}]
    # entities (for MENTIONS) are derived from the triples — the object "pho",
    # with the user subject excluded.
    assert json.loads(rec.entities_json) == [{"name": "pho", "etype": "topic"}]
    # a re-index intent was enqueued (force) so the worker projects RELATES + MENTIONS.
    assert db.query(MemoryIndexOutbox).filter_by(aggregate_id="m1").count() == 1
    db.close()


@pytest.mark.asyncio
async def test_extract_and_store_single_writer(test_db):
    # Per-memory graph projection (the steady-state path): one extraction writes
    # BOTH relations_json (triples) and entities_json (DERIVED from triples) and
    # enqueues a forced re-index. Single source, single writer — no dual-source.
    db = test_db()
    db.add(MemoryRecord(id="m1", owner_agent_name="Jarvis", memory_type="semantic",
                        subject_scope="user", content="user has a son",
                        normalized_content="user has a son", status="active",
                        authority="agent_observed", confidence=0.9, current_version=1,
                        created_at=1.0, updated_at=1.0))
    db.commit(); db.close()

    stored = await kg.extract_and_store(
        "m1", generate_fn=_fixed_fn('[{"s":"user","p":"has","o":"son"}]'))
    assert stored is True
    db = test_db()
    rec = db.query(MemoryRecord).one()
    assert json.loads(rec.relations_json) == [{"s": "user", "p": "has", "o": "son"}]
    assert json.loads(rec.entities_json) == [{"name": "son", "etype": "topic"}]
    assert db.query(MemoryIndexOutbox).filter_by(aggregate_id="m1").count() == 1
    db.close()


@pytest.mark.asyncio
async def test_extract_and_store_skips_missing(test_db):
    assert await kg.extract_and_store("nope", generate_fn=_fixed_fn("[]")) is False


@pytest.mark.asyncio
async def test_backfill_skips_already_filled_unless_forced(test_db):
    db = test_db()
    db.add(MemoryRecord(id="m1", owner_agent_name="Jarvis", memory_type="semantic",
                        subject_scope="user", content="x", normalized_content="x",
                        status="active", authority="agent_observed", confidence=0.9,
                        current_version=1, created_at=1.0, updated_at=1.0,
                        relations_json="[]"))
    db.commit(); db.close()
    assert await kg.backfill_relations("Jarvis", generate_fn=_fixed_fn("[]")) == 0   # already filled
    assert await kg.backfill_relations("Jarvis", force=True, generate_fn=_fixed_fn("[]")) == 1
