"""Fast-lane extractor — unit parse + TRUE LLM-playback e2e.

The e2e drives the REAL extractor (run_fast_extraction → real parse → real
candidate_service write) with a scripted fast-agent PassthroughLLM standing in
for the extractor model. Data lands in an isolated in-memory DB, auto-cleaned.
Covers: single fact, instruction→pinned, multi-fact, entities, nothing-durable,
malformed JSON, code-fenced JSON.
"""
import json
import types

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, MemoryCandidate
from services.memory import fast_extractor as fx


# ── unit: tolerant parser ────────────────────────────────────────────────
def test_parse_single_fact():
    out = fx.parse_extraction('[{"kind":"fact","content":"user is a software engineer"}]')
    assert len(out) == 1 and out[0].memory_type == "semantic"
    assert out[0].content == "user is a software engineer"


def test_parse_instruction_maps_to_pinned():
    out = fx.parse_extraction('[{"kind":"instruction","content":"always answer concisely"}]')
    assert out[0].memory_type == "pinned"


def test_parse_multi_and_entities():
    out = fx.parse_extraction(
        '[{"kind":"fact","content":"works at FPT","entities":[{"name":"FPT","etype":"org"}]},'
        '{"kind":"preference","content":"likes pho"}]')
    assert [m.memory_type for m in out] == ["semantic", "semantic"]
    assert out[0].entities == [{"name": "FPT", "etype": "org"}]


@pytest.mark.parametrize("raw", ["[]", "", "not json at all", "{}", '[{"kind":"fact"}]'])
def test_parse_empty_or_malformed_yields_nothing(raw):
    assert fx.parse_extraction(raw) == []


def test_parse_strips_code_fence():
    out = fx.parse_extraction('```json\n[{"kind":"fact","content":"lives in Hanoi"}]\n```')
    assert len(out) == 1 and "Hanoi" in out[0].content


# ── playback e2e ─────────────────────────────────────────────────────────
def _playback_generate_fn(scripted: str):
    """An async str->str that returns ``scripted`` via a real PassthroughLLM —
    exercises the same generate() path the production extractor uses."""
    from fast_agent.core.prompt import Prompt
    from fast_agent.llm.internal.passthrough import PassthroughLLM
    from fast_agent.mcp.helpers.content_helpers import text_content
    from fast_agent.mcp.prompt_message_extended import PromptMessageExtended

    class _Scripted(PassthroughLLM):
        async def _apply_prompt_provider_specific(self, multipart_messages,
                                                  request_params=None, tools=None, is_template=False):
            return PromptMessageExtended(role="assistant", content=[text_content(scripted)])

    lm = _Scripted()

    async def gen(prompt: str) -> str:
        resp = await lm.generate([Prompt.user(prompt)], request_params=None, tools=None)
        return "\n".join(getattr(b, "text", "") or "" for b in (resp.content or []))
    return gen


@pytest.fixture()
def test_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with engine.connect() as c:
        c.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5("
            "doc_kind UNINDEXED, doc_id UNINDEXED, owner_agent_name UNINDEXED, content)"))
        c.commit()
    Factory = sessionmaker(bind=engine)
    import core.database as cd
    monkeypatch.setattr(cd, "get_db_session", lambda: Factory())
    # keep the extractor's candidate write isolated from the approvals inbox.
    import services.approval_service as asvc
    monkeypatch.setattr(asvc.approval_service, "create_approval", lambda data: {})
    return Factory


_CFG = types.SimpleNamespace(approval_policy="manual", pinned_token_budget=1500)


async def _extract(scripted: str) -> list[str]:
    return await fx.run_fast_extraction(
        "Jarvis", "user: I work as a software engineer at FPT and I love pho",
        _CFG, generate_fn=_playback_generate_fn(scripted))


def _cands(Factory):
    db = Factory()
    try:
        return db.query(MemoryCandidate).filter_by(owner_agent_name="Jarvis").all()
    finally:
        db.close()


async def test_e2e_single_fact_creates_candidate(test_db):
    ids = await _extract('[{"kind":"fact","content":"user works at FPT","entities":[{"name":"FPT","etype":"org"}]}]')
    assert len(ids) == 1
    rows = _cands(test_db)
    assert len(rows) == 1
    payload = json.loads(rows[0].payload_json)
    assert payload["content"] == "user works at FPT"
    assert payload["memory_type"] == "semantic"
    assert payload["entities"] == [{"name": "FPT", "etype": "org"}]   # entities flow to the graph


async def test_e2e_instruction_is_pinned(test_db):
    await _extract('[{"kind":"instruction","content":"from now on answer concisely"}]')
    payload = json.loads(_cands(test_db)[0].payload_json)
    assert payload["memory_type"] == "pinned"


async def test_e2e_multi_fact(test_db):
    ids = await _extract('[{"kind":"fact","content":"user is a software engineer"},'
                         '{"kind":"preference","content":"user likes pho"}]')
    assert len(ids) == 2 and len(_cands(test_db)) == 2


def _seed_memory(Factory, content):
    from core.database import MemoryRecord
    db = Factory()
    db.add(MemoryRecord(id="m1", owner_agent_name="Jarvis", memory_type="semantic",
                        subject_scope="user", content=content,
                        normalized_content=content.lower(), status="active",
                        authority="agent_observed", confidence=0.9,
                        current_version=1, created_at=1.0, updated_at=1.0))
    db.commit(); db.close()


async def test_known_facts_injected_into_extractor_prompt(test_db):
    # #3 capture-side dedup: already-stored memories are listed in the prompt so
    # the LLM doesn't re-propose them (or paraphrases) — no brittle threshold.
    _seed_memory(test_db, "User already likes pho")
    captured = {}

    async def capturing_gen(prompt):
        captured["prompt"] = prompt
        return "[]"
    await fx.run_fast_extraction("Jarvis", "user: hi again", _CFG, generate_fn=capturing_gen)
    assert "ALREADY KNOWN" in captured["prompt"]
    assert "User already likes pho" in captured["prompt"]


async def test_known_facts_includes_pending_candidates(test_db):
    # RC1/RC2: a fact already PROPOSED (the agent's `remember` tool or a prior
    # extractor run, still pending approval) must also count as "known", so the
    # extractor doesn't re-propose it as a second card before it's approved.
    import json as _json

    from core.database import MemoryCandidate
    db = test_db()
    db.add(MemoryCandidate(id="c1", owner_agent_name="Jarvis",
                           candidate_type="agent_remember",
                           payload_json=_json.dumps({"content": "user commutes at 6:50"}),
                           source_refs_json="[]", status="pending", confidence=0.9,
                           requires_curator=0, requires_approval=1, dedupe_key="k1",
                           created_at=1.0))
    db.commit(); db.close()
    captured = {}

    async def capturing_gen(prompt):
        captured["prompt"] = prompt
        return "[]"
    await fx.run_fast_extraction("Jarvis", "user: morning", _CFG, generate_fn=capturing_gen)
    assert "ALREADY KNOWN" in captured["prompt"]
    assert "user commutes at 6:50" in captured["prompt"]


async def test_no_known_facts_keeps_base_prompt(test_db):
    captured = {}

    async def capturing_gen(prompt):
        captured["prompt"] = prompt
        return "[]"
    await fx.run_fast_extraction("Jarvis", "user: hi", _CFG, generate_fn=capturing_gen)
    assert "ALREADY KNOWN" not in captured["prompt"]      # nothing stored yet


async def test_e2e_nothing_durable_no_candidates(test_db):
    ids = await _extract("[]")
    assert ids == [] and _cands(test_db) == []


async def test_e2e_malformed_llm_output_no_crash_no_candidates(test_db):
    ids = await _extract("sorry, I cannot help with that")   # LLM didn't return JSON
    assert ids == [] and _cands(test_db) == []


# ── slow lane ────────────────────────────────────────────────────────────
def test_slow_kinds_map_to_memory_types():
    out = fx.parse_extraction(
        '[{"kind":"workflow","content":"a"},{"kind":"episodic","content":"b"},'
        '{"kind":"decision","content":"c"}]')
    assert [m.memory_type for m in out] == ["procedural", "episodic", "semantic"]


async def test_e2e_slow_workflow_extraction(test_db):
    gen = _playback_generate_fn('[{"kind":"workflow","content":"deploy via staging gate first"}]')
    ids = await fx.run_slow_extraction("Jarvis", "a long deployment discussion", _CFG, generate_fn=gen)
    assert len(ids) == 1
    rows = _cands(test_db)
    assert rows[0].candidate_type == "extracted_slow"
    assert json.loads(rows[0].payload_json)["memory_type"] == "procedural"


async def test_fire_slow_extraction_gated_by_settings(monkeypatch):
    import asyncio

    import services.memory.settings as ms
    fired = []

    async def fake_slow(owner, snippet, cfg, **kw):
        fired.append(owner)
    monkeypatch.setattr(fx, "run_slow_extraction", fake_slow)

    class _M:  # minimal history message
        def __init__(self, t): self.role = "user"; self.content = [types.SimpleNamespace(text=t)]

    # disabled → never fires
    monkeypatch.setattr(ms, "get_memory_settings",
                        lambda: types.SimpleNamespace(enabled=False, auto_capture_preferences=True))
    fx.fire_slow_extraction_from_history("Jarvis", [_M("we decided to deploy via staging")])
    await asyncio.sleep(0)
    assert fired == []
    # enabled + auto_capture → fires
    monkeypatch.setattr(ms, "get_memory_settings",
                        lambda: types.SimpleNamespace(enabled=True, auto_capture_preferences=True))
    fx.fire_slow_extraction_from_history("Jarvis", [_M("we decided to deploy via staging")])
    await asyncio.sleep(0)
    assert fired == ["Jarvis"]


# ── evidence-grounded confidence (decision: backend verifies, not LLM number) ──

_CFG_AUTO = types.SimpleNamespace(approval_policy="auto_low_risk", pinned_token_budget=1500)
_SNIPPET = "user: I work as a software engineer at FPT and I love pho"


async def _extract_auto(scripted: str):
    return await fx.run_fast_extraction("Jarvis", _SNIPPET, _CFG_AUTO,
                                        generate_fn=_playback_generate_fn(scripted))


async def test_e2e_verified_evidence_auto_saves_with_source(test_db):
    """Evidence quote present in the snippet → verified → confidence by reasoning_type,
    auto-saves, and the verbatim quote is stored as provenance in memory_sources."""
    await _extract_auto('[{"kind":"fact","content":"user works at FPT",'
                        '"evidence_excerpt":"software engineer at FPT","reasoning_type":"direct"}]')
    rows = _cands(test_db)
    assert len(rows) == 1
    c = rows[0]
    assert c.confidence == 0.9 and c.status == "auto_approved"   # direct + verified → high, saved
    payload = json.loads(c.payload_json)
    assert payload["excerpt_ok"] is True
    assert payload["confidence_method"] == "evidence_alignment_v1:direct"
    # provenance reaches memory_sources after auto-persist (was empty before this work)
    from core.database import MemoryRecord, MemorySource
    db = test_db()
    try:
        rec = db.query(MemoryRecord).filter_by(owner_agent_name="Jarvis").one()
        srcs = db.query(MemorySource).filter_by(memory_id=rec.id).all()
        assert any("software engineer at FPT" in (s.source_excerpt or "") for s in srcs)
    finally:
        db.close()


async def test_e2e_fabricated_evidence_blocks_autosave(test_db):
    """Evidence quote NOT in the snippet → fabricated → never auto-saves (routes to
    approval), low confidence, no memory persisted."""
    await _extract_auto('[{"kind":"fact","content":"user works at Google",'
                        '"evidence_excerpt":"I work at Google","reasoning_type":"direct"}]')
    rows = _cands(test_db)
    assert len(rows) == 1
    c = rows[0]
    assert c.status == "pending" and c.confidence == 0.4         # blocked, distrusted
    assert json.loads(c.payload_json)["excerpt_ok"] is False
    from core.database import MemoryRecord
    db = test_db()
    try:
        assert db.query(MemoryRecord).filter_by(owner_agent_name="Jarvis").count() == 0
    finally:
        db.close()
