"""LadybugProvider — dense + GraphRAG multi-hop retrieval, owner-scoped.

Real LadybugDB store (temp), a fake embedding provider for deterministic
vectors, so the provider logic (ranking, owner isolation, type filter, graph
boost, degrade-when-unavailable) is asserted without the BGE model.
"""
import tempfile
import types

import pytest

pytest.importorskip("ladybug")
from services.indexing.ladybug_store import EMBED_DIM, LadybugStore  # noqa: E402
from services.retrieval.contracts import RetrievalRequest  # noqa: E402
from services.retrieval.providers.ladybug_provider import LadybugProvider  # noqa: E402


def _vec(axis, lead=1.0):
    v = [0.0] * EMBED_DIM
    v[axis % EMBED_DIM] = lead
    return v


class _Emb:
    def __init__(self, available=True, axis=0):
        self._a = available
        self._axis = axis
    def is_available(self):
        return self._a
    def embed_query(self, q):
        return _vec(self._axis)


@pytest.fixture()
def store():
    s = LadybugStore(f"{tempfile.mkdtemp()}/g")
    s.upsert_memory(record_id="m1", owner="Jarvis", memory_type="semantic", subject_scope="user",
                    content="user is a software engineer", embedding=_vec(0),
                    authority="user_confirmed", confidence=0.9, created_at=1.0, valid_from=1.0)
    s.upsert_memory(record_id="m2", owner="Jarvis", memory_type="pinned", subject_scope="user",
                    content="answer concisely", embedding=_vec(5),
                    authority="user_confirmed", confidence=0.8, created_at=2.0, valid_from=2.0)
    s.upsert_memory(record_id="r1", owner="Riley [SA]", memory_type="semantic", subject_scope="user",
                    content="riley note", embedding=_vec(0),
                    authority="user_confirmed", confidence=0.9, created_at=1.0, valid_from=1.0)
    yield s
    s.close()


def _req(owner="Jarvis", query="job", types=None):
    return RetrievalRequest(owner_agent_name=owner, query=query, types=types or [], mode="balanced")


async def test_dense_search_ranked_and_owner_scoped(store):
    p = LadybugProvider(store, _Emb(axis=0))
    ev = await p.search(_req(), limit=5)
    ids = [e.record_id for e in ev]
    assert ids and ids[0] == "m1"                       # nearest to axis-0 query
    assert "r1" not in ids                              # cross-agent isolation
    assert all(e.owner_agent_name == "Jarvis" for e in ev)
    assert ev[0].scores.dense_rank == 1


async def test_type_filter(store):
    p = LadybugProvider(store, _Emb(axis=5))
    ev = await p.search(_req(types=["pinned"]), limit=5)
    assert [e.record_id for e in ev] == ["m2"]          # only pinned


async def test_graph_multi_hop_boost(store):
    # m1 & m2 share entity "user" → searching near m1 also surfaces m2 via the hop.
    store.link_entity(record_id="m1", entity_id="e_user", name="user", etype="person", normalized="user")
    store.link_entity(record_id="m2", entity_id="e_user", name="user", etype="person", normalized="user")
    p = LadybugProvider(store, _Emb(axis=0))
    ev = await p.search(_req(), limit=5)
    ids = [e.record_id for e in ev]
    assert "m1" in ids and "m2" in ids                  # m2 pulled in via entity link


async def test_degrades_when_embedding_unavailable(store):
    p = LadybugProvider(store, _Emb(available=False))
    assert p.is_available() is False
    assert await p.search(_req(), limit=5) == []
