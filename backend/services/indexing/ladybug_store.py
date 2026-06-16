"""LadybugDB store — the rebuildable graph + vector index for memory v2.

SQLite stays the source of truth (write authority, versions, audit); this store
is a disposable PROJECTION fed by the outbox worker, so adopting an early-stage
embedded graph DB is reversible (rebuild from SQLite if it ever underperforms).

LadybugDB (``ladybug`` pkg, Kùzu-lineage) gives us, in ONE embedded engine:
  - HNSW vector search (``QUERY_VECTOR_INDEX``) for dense recall,
  - a property graph (Memory/Entity nodes, MENTIONS/RELATES edges) for entity
    linking + multi-hop GraphRAG — vector hits feed straight into MATCH.

Owner scoping is a hard invariant: every read filters by ``owner`` so an agent
only ever sees its own memory (mirrors the SQLite/Qdrant rules).
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
from dataclasses import dataclass

logger = logging.getLogger("memory.ladybug")


def _similarity_edges(ids: list[str], embs: list, k: int, threshold: float) -> list[dict]:
    """Undirected memory↔memory edges: each memory linked to its top-``k``
    nearest neighbours with cosine similarity ≥ ``threshold``. Reuses the node
    embeddings already in the graph (no model load), so related memories cluster
    in the UI even when no entities have been extracted yet."""
    if len(ids) < 2:
        return []
    try:
        import numpy as np
    except Exception:  # noqa: BLE001 — numpy unavailable → just no similarity edges
        return []
    m = np.asarray(embs, dtype="float32")
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    m = m / norms
    sim = m @ m.T
    seen: set = set()
    out: list[dict] = []
    for i in range(len(ids)):
        added = 0
        for j in np.argsort(-sim[i]):          # most similar first (self is at the top)
            if added >= k:
                break
            if j == i:
                continue
            if sim[i, j] < threshold:
                break                          # sorted desc → nothing further qualifies
            added += 1
            a, b = sorted((ids[i], ids[int(j)]))
            if (a, b) in seen:
                continue
            seen.add((a, b))
            out.append({"source": a, "target": b, "kind": "similar",
                        "weight": round(float(sim[i, j]), 3)})
    return out


def _wipe_graph_files(path: str) -> None:
    """Remove a LadybugDB graph and its sidecars (``.wal`` / ``.lock`` /
    ``.shadow``). The store is a disposable projection — see ``_open``."""
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    for p in (path, f"{path}.wal", f"{path}.lock", f"{path}.shadow", f"{path}.tmp"):
        try:
            os.remove(p)
        except OSError:
            pass

EMBED_DIM = 1024  # BAAI/bge-m3
_VECTOR_INDEX = "memory_emb_idx"


@dataclass
class VectorHit:
    record_id: str
    owner: str
    memory_type: str
    content: str
    distance: float
    created_at: float = 0.0
    authority: str = "agent_observed"
    confidence: float = 0.5


def _lit(s: str) -> str:
    """Single-quote a string literal for Cypher (Ladybug has no param binding
    for some DDL paths; values are our own ids/enums, never user free-text in
    DDL). Content/owner go through parameterized statements instead."""
    return "'" + str(s).replace("\\", "\\\\").replace("'", "\\'") + "'"


class LadybugStore:
    """Thin wrapper over a LadybugDB graph holding the memory projection.

    One connection guarded by a lock — LadybugDB is embedded (in-process) and
    the backend touches it from async handlers; serialize writes so concurrent
    turns don't corrupt the single connection. Reads are serialized too (cheap
    at personal-memory scale)."""

    def __init__(self, path: str):
        self._path = path
        self._db, self._con = self._open(path)
        # RLock (reentrant): some read methods call others under the lock
        # (vector_search → count); a plain Lock would self-deadlock.
        self._lock = threading.RLock()
        try:
            self._ensure_schema()
        except Exception:
            self.close()          # don't leak the open file handle/lock on bad bring-up
            raise

    @staticmethod
    def _open(path: str):
        """Open the embedded graph, self-healing a corrupted WAL.

        LadybugDB is a DISPOSABLE projection of SQLite (the source of truth). An
        ungraceful backend shutdown (SIGKILL, container stop) can leave a
        half-written WAL — the next open then throws "Corrupted wal file" and
        dense recall stays dead until a human deletes the files. Since the graph
        is rebuildable, wipe it and recreate empty on that specific failure; the
        startup migration (count()==0 → consistency_service.rebuild) re-projects
        every record from SQLite. Any non-corruption error is re-raised."""
        import ladybug
        try:
            db = ladybug.Database(path)
        except RuntimeError as exc:
            msg = str(exc).lower()
            if "wal" not in msg and "corrupt" not in msg:
                raise
            logger.warning("[MEMORY] LadybugDB open failed (%s) — wiping the "
                           "disposable graph; the startup migration rebuilds "
                           "it from SQLite", exc)
            _wipe_graph_files(path)
            db = ladybug.Database(path)   # fresh, empty graph
        return db, ladybug.Connection(db)

    # ---- schema ---------------------------------------------------------
    def _exec(self, query: str, params: dict | None = None):
        return self._con.execute(query, params) if params else self._con.execute(query)

    def _ensure_schema(self) -> None:
        with self._lock:
            try:
                self._exec("INSTALL vector")   # persists on the DB; no-op-ish on re-open
            except Exception as exc:           # noqa: BLE001 — already installed
                logger.debug("[ladybug] INSTALL vector: %s", exc)
            self._exec("LOAD vector")          # per-connection; required every open
            self._exec(
                "CREATE NODE TABLE IF NOT EXISTS Memory("
                "id STRING, owner STRING, memory_type STRING, subject_scope STRING, "
                "content STRING, emb FLOAT[1024], authority STRING, confidence DOUBLE, "
                "created_at DOUBLE, valid_from DOUBLE, status STRING, PRIMARY KEY(id))")
            self._exec(
                "CREATE NODE TABLE IF NOT EXISTS Entity("
                "id STRING, name STRING, etype STRING, normalized STRING, PRIMARY KEY(id))")
            self._exec("CREATE REL TABLE IF NOT EXISTS MENTIONS(FROM Memory TO Entity)")
            # (Entity)-[:RELATES]->(Entity) is intentionally NOT created yet —
            # GraphRAG is MENTIONS-based today; the relations layer lands when the
            # extractor emits typed relations (spec §4 follow-up).
            # Vector index — created once; ignore "already exists" on re-open.
            try:
                self._exec(
                    f"CALL CREATE_VECTOR_INDEX('Memory', '{_VECTOR_INDEX}', 'emb', "
                    f"metric := 'cosine')")
            except Exception as exc:  # noqa: BLE001
                if "exist" not in str(exc).lower():
                    raise

    # ---- writes (ADD-only; SQLite is the SoT) ---------------------------
    def upsert_memory(self, *, record_id: str, owner: str, memory_type: str,
                      subject_scope: str, content: str, embedding: list[float],
                      authority: str, confidence: float, created_at: float,
                      valid_from: float, status: str = "active") -> None:
        """Replace the Memory node for ``record_id`` (ADD-only/idempotent index).

        NOTE: LadybugDB forbids ``SET`` on a vector-indexed property, so an
        embedding change requires DELETE+CREATE — which DROPS this node's
        MENTIONS/RELATES edges. The caller (outbox worker) re-extracts and
        re-links entities on every (re)index, so the graph is rebuilt; do not
        rely on edges surviving an upsert. Wrapped in a transaction so a crash
        can't leave the node deleted-but-not-recreated."""
        if embedding is None or len(embedding) != EMBED_DIM:
            raise ValueError(f"embedding must be {EMBED_DIM}-d, got "
                             f"{0 if embedding is None else len(embedding)}")
        with self._lock:
            self._exec("BEGIN TRANSACTION")
            try:
                self._exec("MATCH (m:Memory {id: $id}) DETACH DELETE m", {"id": record_id})
                self._exec(
                    "CREATE (:Memory {id: $id, owner: $owner, memory_type: $mt, "
                    "subject_scope: $scope, content: $content, emb: $emb, "
                    "authority: $auth, confidence: $conf, created_at: $ca, "
                    "valid_from: $vf, status: $status})",
                    {"id": record_id, "owner": owner, "mt": memory_type, "scope": subject_scope,
                     "content": content, "emb": embedding, "auth": authority,
                     "conf": float(confidence), "ca": float(created_at),
                     "vf": float(valid_from), "status": status})
                self._exec("COMMIT")
            except Exception:
                try:
                    self._exec("ROLLBACK")
                except Exception:  # noqa: BLE001
                    pass
                raise

    def delete_memory(self, record_id: str) -> None:
        with self._lock:
            self._exec("MATCH (m:Memory {id: $id}) DETACH DELETE m", {"id": record_id})

    def link_entity(self, *, record_id: str, entity_id: str, name: str,
                    etype: str, normalized: str) -> None:
        """MENTIONS edge from a memory to an entity (entity linking). Creates the
        entity node if new. Idempotent on the edge."""
        with self._lock:
            # MERGE keys on IDENTITY only (id). Listing mutable props in the
            # MERGE pattern would fail to match an existing entity whose name was
            # re-normalized → a duplicate node under the same PK. Update props via
            # ON MATCH/ON CREATE SET instead.
            self._exec(
                "MERGE (e:Entity {id: $id}) "
                "ON CREATE SET e.name = $name, e.etype = $et, e.normalized = $n "
                "ON MATCH SET e.name = $name, e.normalized = $n",
                {"id": entity_id, "name": name, "et": etype, "n": normalized})
            self._exec(
                "MATCH (m:Memory {id: $mid}), (e:Entity {id: $eid}) "
                "MERGE (m)-[:MENTIONS]->(e)", {"mid": record_id, "eid": entity_id})

    # ---- reads (owner-scoped) -------------------------------------------
    def vector_search(self, *, owner: str, query_embedding: list[float], limit: int = 5,
                      oversample: int = 5, max_k: int = 4000) -> list[VectorHit]:
        """k-NN over Memory.emb, filtered to ``owner`` and active status.

        QUERY_VECTOR_INDEX returns the GLOBAL top-K; we over-fetch then filter by
        owner. With a busy multi-tenant graph a fixed K could be dominated by
        OTHER owners and silently starve a sparse owner, so we GROW K (×4) until
        we have ``limit`` of this owner's matches, the index is exhausted, or we
        hit ``max_k``. (Pre-filtered PROJECT_GRAPH is a later optimization.)"""
        if query_embedding is None or len(query_embedding) != EMBED_DIM:
            raise ValueError(f"query embedding must be {EMBED_DIM}-d")
        with self._lock:
            k = max(limit * oversample, limit)
            total = None
            hits: list[VectorHit] = []
            while True:
                res = self._exec(
                    f"CALL QUERY_VECTOR_INDEX('Memory', '{_VECTOR_INDEX}', $q, $k) "
                    "WHERE node.owner = $owner AND node.status = 'active' "
                    "RETURN node.id, node.owner, node.memory_type, node.content, "
                    "node.created_at, node.authority, node.confidence, distance "
                    "ORDER BY distance LIMIT $lim",
                    {"q": query_embedding, "k": k, "owner": owner, "lim": limit})
                hits = []
                while res.has_next():
                    rid, own, mt, content, ca, auth, conf, dist = res.get_next()
                    hits.append(VectorHit(rid, own, mt, content, float(dist),
                                          float(ca or 0.0), auth or "agent_observed",
                                          float(conf or 0.5)))
                if len(hits) >= limit:
                    break
                if total is None:
                    total = self.count()              # all memories (RLock: reentrant)
                if k >= total or k >= max_k:
                    break                              # index exhausted / capped
                k = min(k * 4, max_k)
            return hits

    def linked_memories(self, *, owner: str, record_ids: list[str], limit: int = 5) -> list[VectorHit]:
        """Entity-linking boost: memories that share an entity with the given
        seed memories (one graph hop), owner-scoped. The multi-hop signal."""
        if not record_ids:
            return []
        with self._lock:
            res = self._exec(
                "MATCH (seed:Memory)-[:MENTIONS]->(e:Entity)<-[:MENTIONS]-(m:Memory) "
                "WHERE seed.id IN $ids AND m.owner = $owner AND m.status = 'active' "
                "AND NOT m.id IN $ids "
                "RETURN DISTINCT m.id, m.owner, m.memory_type, m.content, "
                "m.created_at, m.authority, m.confidence LIMIT $lim",
                {"ids": record_ids, "owner": owner, "lim": limit})
            hits = []
            while res.has_next():
                rid, own, mt, content, ca, auth, conf = res.get_next()
                # distance is a placeholder for graph-linked hits (not vector-ranked);
                # the fusion layer ranks them separately, never as "nearest".
                hits.append(VectorHit(rid, own, mt, content, 1.0, float(ca or 0.0),
                                      auth or "agent_observed", float(conf or 0.5)))
            return hits

    def count(self, owner: str | None = None) -> int:
        with self._lock:
            if owner:
                res = self._exec("MATCH (m:Memory) WHERE m.owner = $o RETURN count(m)", {"o": owner})
            else:
                res = self._exec("MATCH (m:Memory) RETURN count(m)")
            return res.get_next()[0] if res.has_next() else 0

    def graph_dump(self, *, owner: str, limit: int = 200,
                   sim_neighbors: int = 3, sim_threshold: float = 0.55) -> dict:
        """Owner-scoped snapshot for the UI graph view.

        Returns Memory nodes + (a) the Entity nodes they MENTION ("mentions"
        edges, the GraphRAG structure) AND (b) memory↔memory "similar" edges:
        each memory linked to its nearest neighbours by embedding cosine
        similarity. The similarity edges give the graph real structure —
        clusters of related memories — even before any entities are extracted
        (otherwise the view is just disconnected dots). Capped at ``limit``
        memories; content truncated for the label."""
        with self._lock:
            res = self._exec(
                "MATCH (m:Memory) WHERE m.owner = $o AND m.status = 'active' "
                "RETURN m.id, m.content, m.memory_type, m.authority, m.created_at, m.emb "
                "ORDER BY m.created_at DESC LIMIT $lim", {"o": owner, "lim": limit})
            memories, mem_ids, embs = [], [], []
            for_emb_ids = []
            while res.has_next():
                mid, content, mt, auth, ca, emb = res.get_next()
                memories.append({"id": mid, "content": (content or "")[:200],
                                 "memory_type": mt, "authority": auth, "created_at": ca})
                mem_ids.append(mid)
                if emb:
                    embs.append(emb)
                    for_emb_ids.append(mid)
            entities, edges = {}, []
            if mem_ids:
                res = self._exec(
                    "MATCH (m:Memory)-[:MENTIONS]->(e:Entity) WHERE m.id IN $ids "
                    "RETURN m.id, e.id, e.name, e.etype", {"ids": mem_ids})
                while res.has_next():
                    mid, eid, ename, etype = res.get_next()
                    edges.append({"source": mid, "target": eid, "kind": "mentions"})
                    entities[eid] = {"id": eid, "name": ename, "etype": etype}
            edges.extend(_similarity_edges(for_emb_ids, embs, sim_neighbors, sim_threshold))
            return {"memories": memories, "entities": list(entities.values()), "edges": edges}

    def close(self) -> None:
        with self._lock:
            for obj in (getattr(self, "_con", None), getattr(self, "_db", None)):
                close = getattr(obj, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # noqa: BLE001
                        pass


_STORE_SINGLETON: LadybugStore | None = None


def get_ladybug_store(path: str) -> LadybugStore:
    """Process-wide singleton (embedded DB → one open handle per process)."""
    global _STORE_SINGLETON
    if _STORE_SINGLETON is None:
        _STORE_SINGLETON = LadybugStore(path)
    return _STORE_SINGLETON


class LadybugIndexer:
    """Worker-facing adapter: lets the outbox worker target LadybugDB with the
    SAME interface it used for Qdrant (is_available / ensure_collection /
    upsert_points / delete_by_record), so swapping the backend is a drop-in.

    Maps per-chunk Qdrant points → ONE graph node per memory record. Memories
    are short facts (≈1 chunk), so the first point per record represents it; its
    ``dense`` vector becomes the node embedding."""

    def __init__(self, store: LadybugStore):
        self._store = store

    def is_available(self) -> bool:
        return self._store is not None

    def ensure_collection(self) -> None:
        pass                                    # schema built at store init

    def upsert_points(self, points: list[dict]) -> None:
        seen: set[str] = set()
        for p in points:
            pl = p.get("payload", {}) or {}
            rid = pl.get("record_id")
            if not rid or rid in seen:
                continue                        # one node per record (primary chunk)
            seen.add(rid)
            created = float(pl.get("created_at", 0.0) or 0.0)
            self._store.upsert_memory(
                record_id=rid, owner=pl.get("owner_agent_name", ""),
                memory_type=pl.get("memory_type", "semantic"),
                subject_scope=pl.get("subject_scope", "user"),
                content=pl.get("excerpt", ""), embedding=p.get("dense"),
                authority=pl.get("authority", "agent_observed"),
                confidence=float(pl.get("confidence", 0.5) or 0.5),
                created_at=created, valid_from=float(pl.get("valid_from", created) or created),
                status=pl.get("status", "active"))
            # Entity linking (GraphRAG): re-link this memory's entities. upsert
            # replaced the node (edges dropped) so we (re)create them every index.
            for e in (pl.get("entities") or []):
                name = (e.get("name") or "").strip() if isinstance(e, dict) else ""
                if not name:
                    continue
                norm = name.lower()
                self._store.link_entity(
                    record_id=rid, entity_id=f"ent:{norm}", name=name,
                    etype=(e.get("etype") or "topic"), normalized=norm)

    def delete_by_record(self, record_id: str) -> None:
        self._store.delete_memory(record_id)
