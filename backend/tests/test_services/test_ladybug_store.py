"""LadybugDB store — real graph+vector engine (no mocks), temp DB, auto-cleaned.

Verifies the projection store our outbox worker will write to: schema bring-up,
ADD-only upsert, owner-scoped k-NN, entity-linking multi-hop, idempotent re-open.
"""
import tempfile

import pytest

pytest.importorskip("ladybug")
from services.indexing.ladybug_store import (  # noqa: E402
    EMBED_DIM, LadybugIndexer, LadybugStore)


def _vec(axis: int, lead: float = 1.0) -> list[float]:
    v = [0.0] * EMBED_DIM
    v[axis % EMBED_DIM] = lead
    return v


@pytest.fixture()
def store():
    d = tempfile.mkdtemp()
    s = LadybugStore(f"{d}/graph")
    yield s
    s.close()


def _seed(store):
    store.upsert_memory(record_id="m1", owner="Jarvis", memory_type="semantic",
                        subject_scope="user", content="user is a software engineer",
                        embedding=_vec(0), authority="user_confirmed", confidence=0.9,
                        created_at=1.0, valid_from=1.0)
    store.upsert_memory(record_id="m2", owner="Jarvis", memory_type="semantic",
                        subject_scope="user", content="user likes pho",
                        embedding=_vec(5), authority="user_confirmed", confidence=0.9,
                        created_at=2.0, valid_from=2.0)
    store.upsert_memory(record_id="r1", owner="Riley [SA]", memory_type="semantic",
                        subject_scope="user", content="riley private note",
                        embedding=_vec(0), authority="user_confirmed", confidence=0.9,
                        created_at=1.0, valid_from=1.0)


def test_open_self_heals_corrupt_wal(monkeypatch, tmp_path):
    # Regression (2026-06-16): an ungraceful backend shutdown left a corrupted
    # WAL, so every restart opened FTS-only and dense recall stayed dead. The
    # store (a disposable projection) must wipe + recreate on that specific
    # failure so the startup migration can rebuild from SQLite.
    import ladybug

    from services.indexing import ladybug_store as ls

    path = str(tmp_path / "graph")
    real_db = ladybug.Database
    calls = {"n": 0}
    wiped = {"n": 0}

    def flaky_db(p=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Runtime exception: Corrupted wal file. "
                               "Read out invalid WAL record type.")
        return real_db(p, **kw)

    monkeypatch.setattr(ladybug, "Database", flaky_db)
    real_wipe = ls._wipe_graph_files

    def spy_wipe(p):
        wiped["n"] += 1
        real_wipe(p)

    monkeypatch.setattr(ls, "_wipe_graph_files", spy_wipe)

    store = ls.LadybugStore(path)        # first open throws → wipe → retry → ok
    try:
        assert calls["n"] == 2           # retried exactly once
        assert wiped["n"] == 1           # graph was wiped before the retry
        assert store.count() == 0        # fresh empty graph, usable (no raise)
    finally:
        store.close()


def test_graph_dump_knowledge_graph(store):
    # The graph view is a KNOWLEDGE graph: typed (subject)-[predicate]->(object)
    # edges, owner-scoped, with the user hub flagged as a 'subject' node.
    store.link_relations(record_id="m1", owner="Jarvis", triples=[
        {"s": "Người dùng", "p": "thích", "o": "phở"},
        {"s": "Người dùng", "p": "làm việc tại", "o": "Techcombank"},
    ])
    store.link_relations(record_id="r1", owner="Riley [SA]", triples=[
        {"s": "Riley", "p": "thích", "o": "trà"}])          # other owner — must not leak
    g = store.graph_dump(owner="Jarvis")
    labels = {n["label"] for n in g["nodes"]}
    assert {"Người dùng", "phở", "Techcombank"} <= labels
    assert "trà" not in labels                              # Riley's relation excluded
    preds = {(e["predicate"], e["target"].split(":")[-1]) for e in g["edges"]}
    assert ("thích", "phở") in preds
    assert ("làm việc tại", "techcombank") in preds
    # the user hub is a 'subject' node; leaves are 'object'.
    kinds = {n["label"]: n["kind"] for n in g["nodes"]}
    assert kinds["Người dùng"] == "subject" and kinds["phở"] == "object"


def test_link_relations_replaces_on_reproject(store):
    # Re-projecting a memory replaces exactly ITS relations (no stale edges).
    store.link_relations(record_id="m1", owner="Jarvis",
                         triples=[{"s": "Người dùng", "p": "thích", "o": "phở"}])
    store.link_relations(record_id="m1", owner="Jarvis",
                         triples=[{"s": "Người dùng", "p": "thích", "o": "bún"}])
    labels = {n["label"] for n in store.graph_dump(owner="Jarvis")["nodes"]}
    assert "bún" in labels and "phở" not in labels         # old triple dropped


def test_graph_dump_empty_owner(store):
    assert store.graph_dump(owner="Nobody") == {"nodes": [], "edges": []}


def test_indexer_projects_relations_to_graph(store):
    # The worker adapter writes a memory's relations into the KG on (re)index.
    idx = LadybugIndexer(store)
    idx.upsert_points([{
        "dense": _vec(1),
        "payload": {"record_id": "m1", "owner_agent_name": "Jarvis",
                    "memory_type": "semantic", "subject_scope": "user",
                    "excerpt": "người dùng thích phở", "created_at": 1.0,
                    "relations": [{"s": "Người dùng", "p": "thích", "o": "phở"}]},
    }])
    g = store.graph_dump(owner="Jarvis")
    assert "phở" in {n["label"] for n in g["nodes"]}
    assert any(e["predicate"] == "thích" for e in g["edges"])
    # deleting the memory removes its RELATES triples too.
    store.delete_memory("m1")
    assert store.graph_dump(owner="Jarvis") == {"nodes": [], "edges": []}


def test_open_reraises_non_corruption_error(monkeypatch, tmp_path):
    # Self-heal is scoped to WAL corruption; an unrelated open error must
    # propagate (never silently wipe a graph on, say, a permissions fault).
    import ladybug

    from services.indexing import ladybug_store as ls

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(ladybug, "Database", boom)
    wiped = {"n": 0}
    monkeypatch.setattr(ls, "_wipe_graph_files", lambda p: wiped.__setitem__("n", 1))
    with pytest.raises(RuntimeError, match="disk full"):
        ls.LadybugStore(str(tmp_path / "graph"))
    assert wiped["n"] == 0               # did NOT wipe on a non-corruption error


def test_vector_search_ranks_and_is_owner_scoped(store):
    _seed(store)
    # query near axis 0 → m1 ("software engineer") ranks first; r1 (same vec but
    # Riley's) must NEVER appear for Jarvis.
    hits = store.vector_search(owner="Jarvis", query_embedding=_vec(0, lead=0.95), limit=5)
    ids = [h.record_id for h in hits]
    assert ids and ids[0] == "m1"
    assert "r1" not in ids                      # cross-agent isolation
    assert all(h.owner == "Jarvis" for h in hits)


def test_riley_sees_only_own(store):
    _seed(store)
    hits = store.vector_search(owner="Riley [SA]", query_embedding=_vec(0), limit=5)
    assert [h.record_id for h in hits] == ["r1"]


def test_upsert_is_add_only_replace(store):
    _seed(store)
    store.upsert_memory(record_id="m1", owner="Jarvis", memory_type="semantic",
                        subject_scope="user", content="UPDATED content",
                        embedding=_vec(0), authority="user_confirmed", confidence=0.9,
                        created_at=3.0, valid_from=3.0)
    assert store.count("Jarvis") == 2           # replaced in place, not duplicated
    hits = store.vector_search(owner="Jarvis", query_embedding=_vec(0), limit=1)
    assert hits[0].content == "UPDATED content"


def test_entity_linking_multi_hop(store):
    _seed(store)
    # m1 and m2 both mention entity "user" → linked_memories from m1 returns m2.
    store.link_entity(record_id="m1", entity_id="e_user", name="user", etype="person", normalized="user")
    store.link_entity(record_id="m2", entity_id="e_user", name="user", etype="person", normalized="user")
    linked = store.linked_memories(owner="Jarvis", record_ids=["m1"], limit=5)
    assert "m2" in [h.record_id for h in linked]
    assert "m1" not in [h.record_id for h in linked]   # seed excluded


def test_delete(store):
    _seed(store)
    store.delete_memory("m2")
    assert store.count("Jarvis") == 1


def test_reopen_same_path_idempotent_schema_and_persists():
    # Re-opening the SAME path must (a) not fail on existing schema / vector
    # index (INSTALL/CREATE_VECTOR_INDEX idempotency) and (b) see prior data.
    d = tempfile.mkdtemp()
    path = f"{d}/graph"
    s1 = LadybugStore(path)
    s1.upsert_memory(record_id="x", owner="A", memory_type="semantic", subject_scope="user",
                     content="c", embedding=_vec(1), authority="agent_observed",
                     confidence=0.5, created_at=1.0, valid_from=1.0)
    s1.close()
    s2 = LadybugStore(path)                      # same path → exercises the "exists" branch
    try:
        assert s2.count() == 1                   # data persisted
        hits = s2.vector_search(owner="A", query_embedding=_vec(1), limit=1)
        assert hits and hits[0].record_id == "x"
    finally:
        s2.close()


def test_owner_not_starved_by_busy_owner(store):
    # A busy owner with MANY vectors near the query must not push a sparse
    # owner's single relevant match out of results (grow-k owner-scoping, H1).
    for i in range(40):
        store.upsert_memory(record_id=f"busy{i}", owner="Busy", memory_type="semantic",
                            subject_scope="user", content=f"busy {i}", embedding=_vec(0, lead=1.0),
                            authority="agent_observed", confidence=0.5, created_at=1.0, valid_from=1.0)
    store.upsert_memory(record_id="sparse1", owner="Sparse", memory_type="semantic",
                        subject_scope="user", content="the one relevant memory",
                        embedding=_vec(0, lead=0.99), authority="user_confirmed", confidence=0.9,
                        created_at=1.0, valid_from=1.0)
    hits = store.vector_search(owner="Sparse", query_embedding=_vec(0, lead=0.95), limit=5)
    assert [h.record_id for h in hits] == ["sparse1"]   # not starved by 40 Busy vectors


def test_indexer_adapter_maps_points_to_node(store):
    # Worker-style points (Qdrant shape, multiple chunks for one record) → ONE
    # owner-scoped node, searchable; delete_by_record removes it.
    idx = LadybugIndexer(store)
    assert idx.is_available()
    idx.ensure_collection()
    points = [
        {"dense": _vec(0), "payload": {"record_id": "m9", "owner_agent_name": "Jarvis",
         "memory_type": "semantic", "subject_scope": "user", "authority": "user_confirmed",
         "confidence": 0.9, "created_at": 5.0, "excerpt": "user is a software engineer", "status": "active"}},
        {"dense": _vec(0), "payload": {"record_id": "m9", "excerpt": "chunk 2"}},   # same record → ignored
    ]
    idx.upsert_points(points)
    assert store.count("Jarvis") == 1
    hits = store.vector_search(owner="Jarvis", query_embedding=_vec(0), limit=1)
    assert hits and hits[0].record_id == "m9" and "software engineer" in hits[0].content
    idx.delete_by_record("m9")
    assert store.count("Jarvis") == 0


def test_indexer_links_entities_from_payload(store):
    # Entities in the point payload → MENTIONS edges → multi-hop works end-to-end.
    idx = LadybugIndexer(store)
    idx.upsert_points([
        {"dense": _vec(0), "payload": {"record_id": "a", "owner_agent_name": "Jarvis",
         "memory_type": "semantic", "excerpt": "user works at FPT", "created_at": 1.0,
         "entities": [{"name": "FPT", "etype": "org"}]}},
        {"dense": _vec(1), "payload": {"record_id": "b", "owner_agent_name": "Jarvis",
         "memory_type": "semantic", "excerpt": "FPT office is in Hanoi", "created_at": 2.0,
         "entities": [{"name": "FPT", "etype": "org"}]}},
    ])
    linked = store.linked_memories(owner="Jarvis", record_ids=["a"])
    assert "b" in [h.record_id for h in linked]        # shared entity FPT links a↔b


def test_upsert_drops_edges_contract(store):
    # Documented limitation: re-upsert DELETE+CREATEs the node (Ladybug forbids
    # SET on an indexed prop) → its MENTIONS edges are dropped. The worker must
    # re-link. This test pins the contract so it isn't a silent surprise.
    _seed(store)
    store.link_entity(record_id="m1", entity_id="e_user", name="user", etype="person", normalized="user")
    store.link_entity(record_id="m2", entity_id="e_user", name="user", etype="person", normalized="user")
    assert "m2" in [h.record_id for h in store.linked_memories(owner="Jarvis", record_ids=["m1"])]
    # re-upsert m1 → its MENTIONS edge to e_user is gone
    store.upsert_memory(record_id="m1", owner="Jarvis", memory_type="semantic", subject_scope="user",
                        content="re-indexed", embedding=_vec(0), authority="user_confirmed",
                        confidence=0.9, created_at=9.0, valid_from=9.0)
    assert store.linked_memories(owner="Jarvis", record_ids=["m1"]) == []   # edge dropped → caller re-links
