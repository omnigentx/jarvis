"""Recall e2e through the REAL orchestrator: hook → _retrieve →
RetrievalOrchestrator → (LadybugProvider dense + SQLite FTS) → RRF → inject.

The ONLY stub is the embedding (deterministic fake); the orchestrator, both
providers, fusion, the LadybugDB graph, and the injection hook are all
production code. Isolated SQLite + temp LadybugDB.
"""
import tempfile
import types

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

pytest.importorskip("ladybug")
from core.database import Base, MemoryRecord  # noqa: E402
from services.indexing import fts_index  # noqa: E402
from services.indexing.ladybug_store import EMBED_DIM, LadybugStore  # noqa: E402
from services.memory import retrieval_hook as rh  # noqa: E402
from services.retrieval.orchestrator import _CACHE  # noqa: E402


class _FakeEmb:
    def is_available(self): return True
    def dim(self): return EMBED_DIM
    def revision(self): return "fake"
    def _v(self, t):
        v = [0.0] * EMBED_DIM
        v[0 if "fpt" in (t or "").lower() else 1] = 1.0
        return v
    def embed_documents(self, texts): return [self._v(t) for t in texts]
    def embed_query(self, q): return self._v(q)


def _cfg():
    return types.SimpleNamespace(
        enabled=True, auto_capture_preferences=False, mode="balanced",
        embedding_model="BAAI/bge-m3", embedding_revision="",
        vector_backend="ladybug", ladybug_path="unused",
        evidence_token_budget=2500, trigger_lexicon_overrides={},
        quality_gate_thresholds={}, approval_policy="manual", pinned_token_budget=1500)


class _Agent:
    def __init__(self): self.name = "Jarvis"; self.message_history = []
    def load_message_history(self, m): self.message_history = list(m)


class _Runner:
    def __init__(self, a): self._agent = a


def _user(t):
    from fast_agent.mcp.helpers.content_helpers import text_content
    from fast_agent.mcp.prompt_message_extended import PromptMessageExtended
    return PromptMessageExtended(role="user", content=[text_content(t)])


@pytest.fixture()
def wired(monkeypatch):
    _CACHE.clear()
    eng = create_engine("sqlite:///:memory:",
                        connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    with eng.connect() as c:
        c.execute(text("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5("
                       "doc_kind UNINDEXED, doc_id UNINDEXED, owner_agent_name UNINDEXED, content)"))
        c.commit()
    F = sessionmaker(bind=eng)
    store = LadybugStore(f"{tempfile.mkdtemp()}/g")
    emb = _FakeEmb()

    # Seed one Jarvis memory + one Riley memory in BOTH the SoT(+FTS) and the graph.
    seed = F()
    for rid, owner, content in [("m1", "Jarvis", "user works at fpt"),
                                ("r1", "Riley [SA]", "riley works at fpt")]:
        seed.add(MemoryRecord(id=rid, owner_agent_name=owner, memory_type="semantic",
                              subject_scope="user", content=content, normalized_content=content,
                              status="active", authority="user_confirmed", confidence=0.9,
                              current_version=1, created_at=1.0))
        fts_index.fts_upsert(seed, doc_kind=fts_index.KIND_MEMORY, doc_id=rid,
                             owner_agent_name=owner, content=content)
        store.upsert_memory(record_id=rid, owner=owner, memory_type="semantic",
                            subject_scope="user", content=content, embedding=emb._v(content),
                            authority="user_confirmed", confidence=0.9, created_at=1.0, valid_from=1.0)
    seed.commit(); seed.close()

    import core.database as cd
    import services.indexing.ladybug_store as lbs
    import services.memory.settings as ms
    import services.retrieval.orchestrator as orch
    monkeypatch.setattr(cd, "get_db_session", lambda: F())
    monkeypatch.setattr(orch, "get_shared_embedding_provider", lambda *a, **k: emb)
    monkeypatch.setattr(lbs, "get_ladybug_store", lambda path: store)
    monkeypatch.setattr(ms, "get_memory_settings", lambda: _cfg())
    yield types.SimpleNamespace(agent=_Agent())
    store.close()


async def test_recall_injects_real_orchestrator_result(wired):
    agent = wired.agent
    hooks = rh.create_memory_retrieval_hooks()
    await hooks.before_llm_call(_Runner(agent), [_user("where is the fpt office")])
    injected = [m for m in agent.message_history if rh.is_injected_memory(m)]
    assert len(injected) == 1
    blk = rh._msg_text(injected[0])
    assert "fpt" in blk.lower()                      # the seeded memory surfaced
    # cross-agent isolation: Riley's memory must NOT be in Jarvis's recall.
    assert "riley" not in blk.lower()
