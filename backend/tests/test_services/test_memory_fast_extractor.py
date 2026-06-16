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
