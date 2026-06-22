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
    real_q = ls._quarantine_graph_files

    def spy_quarantine(p):
        wiped["n"] += 1
        real_q(p)

    monkeypatch.setattr(ls, "_quarantine_graph_files", spy_quarantine)

    store = ls.LadybugStore(path)        # first open throws → quarantine → retry → ok
    try:
        assert calls["n"] == 2           # retried exactly once
        assert wiped["n"] == 1           # graph was moved aside before the retry
        assert store.count() == 0        # fresh empty graph, usable (no raise)
    finally:
        store.close()


def test_open_reraises_non_corruption_error(monkeypatch, tmp_path):
    # M2: the self-heal must match ONLY Ladybug's exact corrupt-WAL signature —
    # a transient/lock error (or any message merely containing "wal"/"corrupt")
    # must propagate, NOT wipe a healthy graph.
    import ladybug

    from services.indexing import ladybug_store as ls

    def boom(p=None, **kw):
        raise RuntimeError("IO error: database is locked (wal busy)")

    monkeypatch.setattr(ladybug, "Database", boom)
    healed = {"n": 0}
    monkeypatch.setattr(ls, "_quarantine_graph_files",
                        lambda p: healed.__setitem__("n", healed["n"] + 1))

    with pytest.raises(RuntimeError, match="locked"):
        ls.LadybugStore(str(tmp_path / "graph"))
    assert healed["n"] == 0              # never quarantined a healthy graph


def test_graph_dump_knowledge_graph(store):
    # The graph view is a KNOWLEDGE graph: typed (subject)-[predicate]->(object)
    # edges, owner-scoped, with the user hub flagged as a 'subject' node.
    store.link_relations(record_id="m1", owner="Jarvis", triples=[
        {"s": "user", "p": "likes", "o": "pho"},
        {"s": "user", "p": "works at", "o": "Acme"},
    ])
    store.link_relations(record_id="r1", owner="Riley [SA]", triples=[
        {"s": "Riley", "p": "likes", "o": "tea"}])          # other owner — must not leak
    g = store.graph_dump(owner="Jarvis")
    labels = {n["label"] for n in g["nodes"]}
    assert {"user", "pho", "Acme"} <= labels
    assert "tea" not in labels                              # Riley's relation excluded
    preds = {(e["predicate"], e["target"].split(":")[-1]) for e in g["edges"]}
    assert ("likes", "pho") in preds
    assert ("works at", "acme") in preds
    # the user hub is a 'subject' node; leaves are 'object'.
    kinds = {n["label"]: n["kind"] for n in g["nodes"]}
    assert kinds["user"] == "subject" and kinds["pho"] == "object"


def test_link_relations_replaces_on_reproject(store):
    # Re-projecting a memory replaces exactly ITS relations (no stale edges).
    store.link_relations(record_id="m1", owner="Jarvis",
                         triples=[{"s": "user", "p": "likes", "o": "pho"}])
    store.link_relations(record_id="m1", owner="Jarvis",
                         triples=[{"s": "user", "p": "likes", "o": "noodles"}])
    labels = {n["label"] for n in store.graph_dump(owner="Jarvis")["nodes"]}
    assert "noodles" in labels and "pho" not in labels         # old triple dropped


def test_graph_dump_empty_owner(store):
    assert store.graph_dump(owner="Nobody") == {"nodes": [], "edges": []}


def test_vector_search_relevance_gate(store):
    # cosine distance = 1 - similarity (LadybugDB): identical→0, orthogonal→1.
    store.upsert_memory(record_id="near", owner="J", memory_type="semantic",
                        subject_scope="user", content="near", embedding=_vec(0),
                        authority="user_confirmed", confidence=0.9, created_at=1.0, valid_from=1.0)
    store.upsert_memory(record_id="far", owner="J", memory_type="semantic",
                        subject_scope="user", content="far", embedding=_vec(50),
                        authority="user_confirmed", confidence=0.9, created_at=1.0, valid_from=1.0)
    # query == near's vector: near dist 0, far dist 1.0
    q = _vec(0)
    assert {h.record_id for h in store.vector_search(owner="J", query_embedding=q, limit=5)} == {"near", "far"}
    # gate at 0.5 drops the orthogonal 'far' (1.0 > 0.5), keeps 'near' (0.0)
    gated = store.vector_search(owner="J", query_embedding=q, limit=5, max_distance=0.5)
    assert {h.record_id for h in gated} == {"near"}
    # off-topic query (orthogonal to BOTH) → nearest are all far → [] (no spin to max_k)
    assert store.vector_search(owner="J", query_embedding=_vec(99), limit=5, max_distance=0.5) == []


def test_indexer_projects_relations_to_graph(store):
    # The worker adapter writes a memory's relations into the KG on (re)index.
    idx = LadybugIndexer(store)
    idx.upsert_points([{
        "dense": _vec(1),
        "payload": {"record_id": "m1", "owner_agent_name": "Jarvis",
                    "memory_type": "semantic", "subject_scope": "user",
                    "excerpt": "user likes pho", "created_at": 1.0,
                    "relations": [{"s": "user", "p": "likes", "o": "pho"}]},
    }])
    g = store.graph_dump(owner="Jarvis")
    assert "pho" in {n["label"] for n in g["nodes"]}
    assert any(e["predicate"] == "likes" for e in g["edges"])
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


def test_linked_memories_respects_max_hops(store):
    # Chain A —shares X— B —shares Y— C. A and C share NO entity, so they are
    # reachable only at 2 memory hops. hop=1 (default) must NOT leak C; hop=2 must.
    for rid, vi in (("A", 0), ("B", 1), ("C", 2)):
        store.upsert_memory(record_id=rid, owner="Jarvis", memory_type="semantic",
                            subject_scope="user", content=rid, embedding=_vec(vi),
                            authority="user_confirmed", confidence=0.9,
                            created_at=1.0, valid_from=1.0)
    store.link_entity(record_id="A", entity_id="X", name="X", etype="topic", normalized="x")
    store.link_entity(record_id="B", entity_id="X", name="X", etype="topic", normalized="x")
    store.link_entity(record_id="B", entity_id="Y", name="Y", etype="topic", normalized="y")
    store.link_entity(record_id="C", entity_id="Y", name="Y", etype="topic", normalized="y")

    hop1 = {h.record_id for h in store.linked_memories(owner="Jarvis", record_ids=["A"], limit=10, max_hops=1)}
    hop2 = {h.record_id for h in store.linked_memories(owner="Jarvis", record_ids=["A"], limit=10, max_hops=2)}
    assert hop1 == {"B"}            # one hop: only the directly-shared-entity memory
    assert hop2 == {"B", "C"}       # two hops: C reached via B's entity Y


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


def test_delete_to_empty_rebuilds_vector_index(store):
    # Regression (2026-06-22 prod recall outage): on LadybugDB 0.17.1, emptying the
    # Memory table (the "forget all memories" op) leaves the HNSW vector index dead
    # — rows inserted afterward exist (count>0, valid embeddings) but
    # QUERY_VECTOR_INDEX returns nothing, so dense recall silently dies for every
    # new memory until a full rebuild. Reproduced 12/12. delete_memory must rebuild
    # the index the instant the table empties. Partial deletes/updates are safe;
    # only deletion-to-empty triggers it, so this test wipes ALL rows then re-adds.
    def _put(rid, axis, t):
        store.upsert_memory(record_id=rid, owner="J", memory_type="semantic",
                            subject_scope="user", content=rid, embedding=_vec(axis),
                            authority="user_confirmed", confidence=0.9,
                            created_at=t, valid_from=t)

    for i in range(4):
        _put(f"a{i}", i, 1.0)
    assert len(store.vector_search(owner="J", query_embedding=_vec(0), limit=10)) == 4

    for i in range(4):
        store.delete_memory(f"a{i}")
    assert store.count() == 0

    for i in range(3):
        _put(f"b{i}", i, 2.0)
    # Without the post-empty index rebuild this returns < 3 (usually 0).
    hits = store.vector_search(owner="J", query_embedding=_vec(0), limit=10)
    assert len(hits) == 3, f"dense recall dead after wipe+re-add: got {len(hits)}"
