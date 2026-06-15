"""Token attribution e2e: the "silent" extractor LLM call is recorded under a
MEMORY agent name so the Token usage view can filter memory spend separately.

Drives the REAL build_extractor_generate_fn → real token-persistence path
(services.sse_progress) into an isolated DB. The LLM + its usage are stubbed.
"""
import types

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, TokenUsageRecord
from services.memory import fast_extractor as fx


class _FakeLLM:
    """Reports a deterministic per-call usage so token attribution has something
    to persist (a real provider's usage_accumulator behaves the same)."""
    def __init__(self):
        turn = types.SimpleNamespace(input_tokens=12, output_tokens=8, total_tokens=20,
                                     model="test-model", cache_usage=None, reasoning_tokens=0)
        self.usage_accumulator = types.SimpleNamespace(turns=[turn])

    async def generate(self, messages, request_params=None, tools=None):
        from fast_agent.mcp.helpers.content_helpers import text_content
        from fast_agent.mcp.prompt_message_extended import PromptMessageExtended
        return PromptMessageExtended(role="assistant", content=[text_content("[]")])


async def test_extractor_tokens_recorded_under_memory_category(monkeypatch):
    eng = create_engine("sqlite:///:memory:",
                        connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    F = sessionmaker(bind=eng)

    # extractor inherits the "main" LLM (our fake with usage).
    agent = types.SimpleNamespace(name="Jarvis", _llm=_FakeLLM(), _context=None, context=None)
    app = types.SimpleNamespace(_agents={"Jarvis": agent})
    import services.memory.settings as ms
    import services.pricing as pricing
    import services.shared_state as st
    import core.database as _cd
    monkeypatch.setattr(st, "agent_app", app, raising=False)
    monkeypatch.setattr(ms, "get_memory_settings",
                        lambda: types.SimpleNamespace(curator_provider="", curator_model="",
                                                      curator_base_url=""))
    monkeypatch.setattr(ms, "get_curator_api_key", lambda: "", raising=False)
    monkeypatch.setattr(pricing, "estimate_cost", lambda **k: 0.0)
    monkeypatch.setattr(_cd, "get_db", lambda: iter([F()]))   # token persistence target

    gen = fx.build_extractor_generate_fn("memory:fast")
    assert gen is not None
    await gen("extract durable facts from this")

    db = F()
    recs = db.query(TokenUsageRecord).filter_by(category="memory:fast").all()
    assert len(recs) == 1                                     # tagged separately from real agents
    assert recs[0].total_tokens == 20 and recs[0].model == "test-model"
    db.close()


async def test_slow_lane_tagged_memory_slow(monkeypatch):
    eng = create_engine("sqlite:///:memory:",
                        connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    F = sessionmaker(bind=eng)
    agent = types.SimpleNamespace(name="Jarvis", _llm=_FakeLLM(), _context=None, context=None)
    app = types.SimpleNamespace(_agents={"Jarvis": agent})
    import services.memory.settings as ms
    import services.pricing as pricing
    import services.shared_state as st
    import core.database as _cd
    monkeypatch.setattr(st, "agent_app", app, raising=False)
    monkeypatch.setattr(ms, "get_memory_settings",
                        lambda: types.SimpleNamespace(curator_provider="", curator_model="",
                                                      curator_base_url=""))
    monkeypatch.setattr(ms, "get_curator_api_key", lambda: "", raising=False)
    monkeypatch.setattr(pricing, "estimate_cost", lambda **k: 0.0)
    monkeypatch.setattr(_cd, "get_db", lambda: iter([F()]))

    gen = fx.build_extractor_generate_fn("memory:slow")
    await gen("synthesize")
    db = F()
    assert db.query(TokenUsageRecord).filter_by(category="memory:slow").count() == 1
    db.close()
