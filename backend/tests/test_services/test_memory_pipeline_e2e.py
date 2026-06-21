"""FULL-PIPELINE e2e (memory v2): capture → index → LadybugDB → retrieve.

Chains the REAL code end-to-end — fast extractor → candidate_service →
create_memory (with entities) → outbox → MemoryIndexWorker → LadybugIndexer →
LadybugStore (node + entity edges) → LadybugProvider retrieval. Only the LLM
(fast-agent PassthroughLLM playback) and the embedding (deterministic fake) are
stubbed; everything else is production code. Isolated in-memory SQLite + a temp
LadybugDB, auto-cleaned.
"""
import tempfile
import types

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

pytest.importorskip("ladybug")
from core.database import Base, MemoryRecord  # noqa: E402
from services.indexing.ladybug_store import EMBED_DIM, LadybugIndexer, LadybugStore  # noqa: E402
from services.indexing.memory_index_worker import MemoryIndexWorker  # noqa: E402
from services.memory import candidate_service as cnd  # noqa: E402
from services.memory import fast_extractor as fx  # noqa: E402
from services.retrieval.contracts import RetrievalRequest  # noqa: E402
from services.retrieval.providers.ladybug_provider import LadybugProvider  # noqa: E402


class _FakeEmb:
    """Deterministic: same keyword → same axis, so the worker (index) and the
    provider (query) agree without loading BGE."""
    def is_available(self): return True
    def dim(self): return EMBED_DIM
    def revision(self): return "fake"
    def _v(self, t):
        v = [0.0] * EMBED_DIM
        tl = (t or "").lower()
        if "fpt" in tl or "engineer" in tl or "work" in tl:
            v[0] = 1.0
        elif "pho" in tl:
            v[5] = 1.0
        else:
            v[1] = 1.0
        return v
    def embed_documents(self, texts): return [self._v(t) for t in texts]
    def embed_query(self, q): return self._v(q)


def _playback(scripted):
    from fast_agent.core.prompt import Prompt
    from fast_agent.llm.internal.passthrough import PassthroughLLM
    from fast_agent.mcp.helpers.content_helpers import text_content
    from fast_agent.mcp.prompt_message_extended import PromptMessageExtended

    class _LM(PassthroughLLM):
        async def _apply_prompt_provider_specific(self, m, request_params=None, tools=None, is_template=False):
            return PromptMessageExtended(role="assistant", content=[text_content(scripted)])
    lm = _LM()

    async def gen(prompt):
        r = await lm.generate([Prompt.user(prompt)], request_params=None, tools=None)
        return "\n".join(getattr(b, "text", "") or "" for b in (r.content or []))
    return gen


_CFG = types.SimpleNamespace(approval_policy="manual", pinned_token_budget=1500)


@pytest.fixture()
def pipeline(monkeypatch):
    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with engine.connect() as c:
        c.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5("
            "doc_kind UNINDEXED, doc_id UNINDEXED, owner_agent_name UNINDEXED, content)"))
        c.commit()
    Factory = sessionmaker(bind=engine)
    store = LadybugStore(f"{tempfile.mkdtemp()}/g")
    emb = _FakeEmb()

    import core.database as cd
    import services.approval_service as asvc
    import services.indexing.memory_index_worker as wmod
    monkeypatch.setattr(cd, "get_db_session", lambda: Factory())
    monkeypatch.setattr(wmod, "SessionLocal", Factory)
    # isolate from the approvals inbox + its own DB
    monkeypatch.setattr(asvc.approval_service, "create_approval", lambda data: {})
    monkeypatch.setattr(asvc.approval_service, "resolve_memory_candidate_card",
                        lambda cid, decision: None)

    worker = MemoryIndexWorker()
    monkeypatch.setattr(worker, "_dense", lambda: LadybugIndexer(store))
    monkeypatch.setattr(worker, "_emb", lambda: emb)
    yield types.SimpleNamespace(Factory=Factory, store=store, emb=emb, worker=worker)
    store.close()


async def test_capture_to_retrieve_full_pipeline(pipeline):
    p = pipeline
    # 1) fast extractor (playback) → candidate with an entity.
    ids = await fx.run_fast_extraction(
        "Jarvis", "user: I work at FPT as a software engineer", _CFG,
        generate_fn=_playback('[{"kind":"fact","content":"user works at FPT",'
                              '"entities":[{"name":"FPT","etype":"org"}]}]'))
    assert len(ids) == 1

    # 2) approve → create_memory (entities_json) → outbox enqueue.
    db = p.Factory()
    cnd.approve_candidate(db, ids[0])
    rec = db.query(MemoryRecord).filter_by(owner_agent_name="Jarvis").one()
    assert "FPT" in rec.content and rec.entities_json and "FPT" in rec.entities_json
    db.close()

    # 3) real worker drains the outbox → LadybugDB node + entity edge.
    stats = await p.worker.process_pending(now=__import__("time").time() + 100)
    assert stats["done"] >= 1
    assert p.store.count("Jarvis") == 1

    # 4) LadybugProvider retrieves it (dense) — full round trip.
    prov = LadybugProvider(p.store, p.emb)
    ev = await prov.search(
        RetrievalRequest(owner_agent_name="Jarvis", query="where do I work", types=[]), limit=5)
    assert ev and ev[0].record_id == rec.id and "fpt" in ev[0].excerpt.lower()
    # cross-agent isolation holds end-to-end
    other = await prov.search(
        RetrievalRequest(owner_agent_name="Riley [SA]", query="where do I work", types=[]), limit=5)
    assert other == []


async def test_entity_link_survives_pipeline_multi_hop(pipeline):
    p = pipeline
    # two memories mentioning the same entity (FPT) → linked in the graph e2e.
    for content in ("user works at FPT", "FPT headquarters is in Hanoi"):
        ids = await fx.run_fast_extraction(
            "Jarvis", content, _CFG,
            generate_fn=_playback(f'[{{"kind":"fact","content":"{content}",'
                                 f'"entities":[{{"name":"FPT","etype":"org"}}]}}]'))
        db = p.Factory()
        cnd.approve_candidate(db, ids[0])
        db.close()
    await p.worker.process_pending(now=__import__("time").time() + 100)
    assert p.store.count("Jarvis") == 2

    prov = LadybugProvider(p.store, p.emb)
    ev = await prov.search(
        RetrievalRequest(owner_agent_name="Jarvis", query="work at FPT", types=[]), limit=5)
    # both surface — one by vector, the other pulled via the shared FPT entity.
    assert len({e.record_id for e in ev}) == 2
