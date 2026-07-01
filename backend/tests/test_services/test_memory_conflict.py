"""LLM-judged conflict resolution at save time (services.memory.conflict).

Real DB (in-memory SQLite), real MemoryService writes/supersede, real gate +
parse. Only the two injected seams are scripted: the LLM judge (generate_fn) and
the embedder (embed_fn) — so the test is deterministic with no network/model.
The subsystem under test (gate → judge → supersede) runs for real.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, MemoryRecord
from services.memory import conflict
from services.memory.memory_service import MemoryService


@pytest.fixture()
def Session(monkeypatch):
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    # resolve_conflicts / _created_dates open their OWN session via get_db_session
    # — point it at the same in-memory engine (StaticPool → one shared connection).
    import core.database as cd
    monkeypatch.setattr(cd, "get_db_session", lambda: factory())
    # Silence the live SSE emit so create_memory doesn't reach the activity stream.
    import services.activity_stream as asm
    monkeypatch.setattr(asm.activity_stream_manager, "broadcast", lambda ev: None)
    return factory


def _mk(Session, content, *, created, mtype="semantic", scope="user", owner="Jarvis"):
    db = Session()
    try:
        rec = MemoryService(db).create_memory(
            owner_agent_name=owner, memory_type=mtype, content=content,
            subject_scope=scope, authority="user_confirmed", confidence=0.6, now=created)
        return rec.id
    finally:
        db.close()


def _status(Session, rec_id):
    db = Session()
    try:
        return db.get(MemoryRecord, rec_id).status
    finally:
        db.close()


def _gen(ret):
    async def f(_prompt):
        f.calls.append(_prompt)
        return ret
    f.calls = []
    return f


def _embed_by_keyword(texts):
    """'work' → [1,0], else [0,1]: lets a test force the gate to pass/fail."""
    return [[1.0, 0.0] if "work" in t.lower() else [0.0, 1.0] for t in texts]


def _embed_all_same(texts):
    return [[1.0, 0.0] for _ in texts]


# ── the happy path: a real same-slot contradiction supersedes the older fact ──
async def test_real_conflict_supersedes_older(Session):
    old = _mk(Session, "user works at Techcombank", created=100.0)
    new = _mk(Session, "user works at NovaCorp", created=200.0)
    gen = _gen('{"superseded": [1]}')

    out = await conflict.resolve_conflicts(new, generate_fn=gen, embed_fn=_embed_by_keyword)

    assert out == [old]
    assert _status(Session, old) == "superseded"   # stale one retired
    assert _status(Session, new) == "active"        # newest kept
    assert len(gen.calls) == 1                       # the LLM was consulted once


# ── coexisting facts: gate passes, but the LLM says "not a conflict" → keep both ──
async def test_non_conflict_keeps_both(Session):
    a = _mk(Session, "user likes tea", created=100.0)
    b = _mk(Session, "user likes coffee", created=200.0)
    gen = _gen('{"superseded": []}')

    out = await conflict.resolve_conflicts(b, generate_fn=gen, embed_fn=_embed_all_same)

    assert out == []
    assert _status(Session, a) == "active"
    assert _status(Session, b) == "active"
    assert len(gen.calls) == 1                       # asked, said no → nothing dropped


# ── no same-slot sibling → never even calls the LLM (cost stays zero) ──
async def test_no_candidates_skips_llm(Session):
    only = _mk(Session, "user works at NovaCorp", created=200.0)
    gen = _gen('{"superseded": [1]}')

    out = await conflict.resolve_conflicts(only, generate_fn=gen, embed_fn=_embed_by_keyword)

    assert out == []
    assert gen.calls == []                           # gate had nothing → no LLM
    assert _status(Session, only) == "active"


# ── a sibling exists but isn't semantically close → gate filters, no LLM ──
async def test_gate_filters_unrelated_sibling(Session):
    coffee = _mk(Session, "user likes coffee", created=100.0)
    work = _mk(Session, "user works at NovaCorp", created=200.0)
    gen = _gen('{"superseded": [1]}')

    out = await conflict.resolve_conflicts(work, generate_fn=gen, embed_fn=_embed_by_keyword)

    assert out == []
    assert gen.calls == []                           # cosine below gate → skip LLM
    assert _status(Session, coffee) == "active"
    assert _status(Session, work) == "active"


# ── LLM blows up mid-judgement → ADD-only fallback, nothing superseded, no raise ──
async def test_llm_failure_is_safe(Session):
    old = _mk(Session, "user works at Techcombank", created=100.0)
    new = _mk(Session, "user works at NovaCorp", created=200.0)

    async def boom(_prompt):
        raise RuntimeError("curator LLM down")

    out = await conflict.resolve_conflicts(new, generate_fn=boom, embed_fn=_embed_by_keyword)

    assert out == []                                 # swallowed, best-effort
    assert _status(Session, old) == "active"         # both survive
    assert _status(Session, new) == "active"


# ── no generate_fn available (boot/tests with no agent) → no-op ──
async def test_no_generate_fn_noop(Session, monkeypatch):
    _mk(Session, "user works at Techcombank", created=100.0)
    new = _mk(Session, "user works at NovaCorp", created=200.0)
    monkeypatch.setattr("services.memory.fast_extractor.build_extractor_generate_fn",
                        lambda *_a, **_k: None)

    out = await conflict.resolve_conflicts(new, embed_fn=_embed_by_keyword)
    assert out == []


# ── scheduling from sync code with no running loop is a safe no-op ──
def test_schedule_no_loop_noop():
    # No running event loop here → must not raise, must not create a task.
    conflict.schedule_resolve_conflicts("nonexistent-id")


# ── tolerant verdict parsing ──
def test_parse_superseded_variants():
    assert conflict._parse_superseded('{"superseded": [1, 2]}', 3) == [1, 2]
    assert conflict._parse_superseded('```json\n{"superseded":[2]}\n```', 3) == [2]
    assert conflict._parse_superseded('Here you go: {"superseded": [1]} done', 2) == [1]
    assert conflict._parse_superseded('{"superseded": [5]}', 2) == []      # out of range
    assert conflict._parse_superseded('{"superseded": []}', 3) == []
    assert conflict._parse_superseded("not json at all", 3) == []
    assert conflict._parse_superseded("", 3) == []
    assert conflict._parse_superseded('{"superseded": [1, 1, 2]}', 3) == [1, 2]  # de-duped


def test_shared_embed_uses_configured_model(monkeypatch):
    # Regression (PR #120 review): the gate's embedder MUST be the CONFIGURED model,
    # not the shared provider's bge-m3 default — else its vectors live in a different
    # space than the indexed memories and every cosine is noise, silently disabling
    # conflict resolution on the default Qwen3 config.
    import types
    seen = {}

    class _Prov:
        def embed_documents(self, texts):
            return [[1.0] for _ in texts]

    monkeypatch.setattr("services.indexing.embedding_provider.get_shared_embedding_provider",
                        lambda model, revision: (seen.update(model=model, revision=revision) or _Prov()))
    monkeypatch.setattr("services.memory.settings.get_memory_settings",
                        lambda: types.SimpleNamespace(embedding_model="Qwen/Qwen3-Embedding-0.6B",
                                                      embedding_revision="rev-1"))
    conflict._shared_embed(["hi"])
    assert seen == {"model": "Qwen/Qwen3-Embedding-0.6B", "revision": "rev-1"}


def test_cosine_basics():
    assert conflict._cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert conflict._cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert conflict._cosine([0.0, 0.0], [1.0, 1.0]) == 0.0    # zero-norm guard
    assert conflict._cosine([1.0], [1.0, 0.0]) == 0.0          # length mismatch guard


# ── the capture chokepoint actually schedules conflict resolution ──
def test_persist_wires_conflict_resolution(monkeypatch):
    from services.memory import candidate_service as cnd

    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    import services.activity_stream as asm
    monkeypatch.setattr(asm.activity_stream_manager, "broadcast", lambda ev: None)
    calls = []
    monkeypatch.setattr("services.memory.conflict.schedule_resolve_conflicts",
                        lambda rid: calls.append(rid))

    cnd.create_candidate(db, owner_agent_name="Jarvis", candidate_type="preference",
                         payload=dict(memory_type="semantic", content="user works at NovaCorp",
                                      subject_scope="user", authority="user_confirmed"),
                         now=100.0)
    db.close()
    assert len(calls) == 1 and calls[0]               # fired once, with a record id


# ── Layer 2: the recall block the LLM reads carries each memory's saved date ──
def test_render_block_stamps_created_date(Session):
    import types

    from services.memory import retrieval_hook as rh
    rid = _mk(Session, "user works at NovaCorp", created=1_751_241_600.0)  # 2025-06-30 UTC
    ev = types.SimpleNamespace(record_id=rid, memory_type="semantic",
                               excerpt="user works at NovaCorp")

    block = rh._render_block([ev])

    assert "(saved 2025-06-30)" in block
    assert "user works at NovaCorp" in block
    assert "newer saved date wins" in block           # the tiebreak instruction
