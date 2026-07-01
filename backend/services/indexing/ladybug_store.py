"""LadybugDB store — the rebuildable graph + vector index for memory v2.

SQLite stays the source of truth (write authority, versions, audit); this store
is a disposable PROJECTION fed by the outbox worker, so adopting an early-stage
embedded graph DB is reversible (rebuild from SQLite if it ever underperforms).

LadybugDB (``ladybug`` pkg, Kùzu-lineage) gives us, in ONE embedded engine:
  - HNSW vector search (``QUERY_VECTOR_INDEX``) for dense recall,
  - a property graph (Memory/Entity nodes, MENTIONS/RELATES edges) for entity
    linking + multi-hop GraphRAG — vector hits feed straight into MATCH.

Owner scoping is a hard invariant: every read filters by ``owner`` so an agent
only ever sees its own memory (mirrors the SQLite rules).
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
from dataclasses import dataclass

logger = logging.getLogger("memory.ladybug")


def _norm(s: str) -> str:
    """Identity key for an entity name: lowercased + whitespace-collapsed, so
    'Phở' and 'phở ' collapse to one node."""
    return " ".join((s or "").strip().lower().split())


_GRAPH_SIDECARS = ("", ".wal", ".lock", ".shadow", ".tmp")


def _wipe_graph_files(path: str) -> None:
    """Remove a LadybugDB graph and its sidecars. The store is a disposable
    projection — see ``_open``."""
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    for s in _GRAPH_SIDECARS:
        try:
            os.remove(path + s)
        except OSError:
            pass


def _quarantine_graph_files(path: str) -> None:
    """Move a corrupt graph + sidecars ASIDE to ``<path>*.corrupt`` instead of
    deleting them, so a mis-triggered self-heal is forensically recoverable (the
    projection is rebuildable from SQLite either way). Overwrites a previous
    quarantine — only the latest corruption is kept. Best-effort."""
    for s in _GRAPH_SIDECARS:
        src = path + s
        if not os.path.exists(src):
            continue
        dst = src + ".corrupt"
        try:
            if os.path.isdir(dst):
                shutil.rmtree(dst, ignore_errors=True)
            elif os.path.exists(dst):
                os.remove(dst)
            os.replace(src, dst)
        except OSError:
            # last resort: don't let an unmovable file block recovery
            _wipe_graph_files(src)

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
            # Match Ladybug's WAL-replay corruption — BOTH known signatures:
            #   * the old "Corrupted wal file" message, and
            #   * the newer assertion that surfaces as
            #     'Assertion failed in file ".../storage/wal/wal_record.cpp" ...
            #      UNREACHABLE_CODE' when an ungraceful exit left a dirty WAL.
            # (Matching only the old string left prod stuck "FTS-only" — the
            # assertion never triggered the self-heal. See ladybugdb/kuzu WAL
            # recovery: rm the wal / rebuild.) Stay SPECIFIC to WAL replay so a
            # broad "wal"/"corrupt" substring can't wipe a healthy graph on an
            # unrelated transient/lock error. Quarantine (not delete) so a
            # misfire is recoverable; the startup migration rebuilds from SQLite.
            _msg = str(exc).lower()
            if not ("corrupted wal" in _msg or "wal_record.cpp" in _msg):
                raise
            logger.error("[MEMORY] LadybugDB corrupt WAL (%s) — quarantining the "
                         "disposable graph to <path>*.corrupt; the startup "
                         "migration rebuilds it from SQLite", exc)
            _quarantine_graph_files(path)
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
            # Knowledge-graph relations: (subject)-[RELATES {predicate}]->(object),
            # both Entity nodes. ``owner`` scopes the edge to one agent's user;
            # ``mem`` is the source memory so a re-projection can replace exactly
            # that memory's relations (delete-by-mem then re-create).
            self._exec("CREATE REL TABLE IF NOT EXISTS RELATES("
                       "FROM Entity TO Entity, predicate STRING, owner STRING, mem STRING)")
            # Vector index — created once; ignore "already exists" on re-open.
            self._create_vector_index()

    def _create_vector_index(self) -> None:
        """CREATE the HNSW vector index; ignore "already exists". Single source of
        truth for the index name/params — used by schema bring-up AND the
        post-empty rebuild (``_rebuild_vector_index``)."""
        try:
            self._exec(
                f"CALL CREATE_VECTOR_INDEX('Memory', '{_VECTOR_INDEX}', 'emb', "
                f"metric := 'cosine')")
        except Exception as exc:  # noqa: BLE001
            if "exist" not in str(exc).lower():
                raise

    def _rebuild_vector_index(self) -> None:
        """DROP + re-CREATE the vector index. REQUIRED after the Memory table is
        emptied: LadybugDB 0.17.1 leaves the HNSW index DEAD once all indexed rows
        are deleted (count→0) — rows inserted afterward exist (count>0, valid
        embeddings) but QUERY_VECTOR_INDEX returns NOTHING until the index is
        rebuilt (reproduced 12/12). This was the 2026-06-22 prod recall outage: a
        "forget all memories" emptied the table, so every memory added afterward
        was invisible to dense search while index-status still read healthy.
        Partial deletes and updates do NOT trigger it — only deletion-to-empty.
        Caller must hold ``self._lock``."""
        try:
            self._exec(f"CALL DROP_VECTOR_INDEX('Memory', '{_VECTOR_INDEX}')")
        except Exception as exc:  # noqa: BLE001 — index may already be absent
            if "exist" not in str(exc).lower():
                raise
        self._create_vector_index()

    def rebuild_vector_index(self) -> None:
        """Public recovery hook: DROP+CREATE the HNSW index so an already-dead
        index becomes searchable again over the rows currently present. Re-upsert
        alone does NOT revive a dead index (measured), so this is what the UI
        'Restore' button / ``/memory/repair`` calls. Idempotent."""
        with self._lock:
            self._rebuild_vector_index()
            self._checkpoint()

    def _checkpoint(self) -> None:
        """Flush the WAL into the main DB after a write. The vector-index WAL bug
        (LadybugDB/Kùzu, unfixed through 0.18.0 — measured) SIGSEGVs on reopen if
        the process is killed with an unflushed index write; agents/OOM/container
        stops kill ungracefully all the time. An explicit checkpoint after each
        write means a kill can only ever land on a CONSISTENT on-disk state
        (measured: checkpoint-then-kill reopens clean). Best-effort + inside the
        lock — a checkpoint failure must not fail the write. Caller holds ``self._lock``."""
        try:
            self._exec("CHECKPOINT")
        except Exception as exc:  # noqa: BLE001 — durability hygiene, not correctness
            logger.debug("[ladybug] checkpoint skipped: %s", exc)

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
            # A re-upsert of the SOLE memory does DETACH DELETE (table → empty) then
            # CREATE. Emptying the Memory table kills the HNSW index (the SAME
            # LadybugDB bug delete_memory guards against), so the recreated row lands
            # in a dead index and is silently unsearchable — and it STAYS dead for
            # every memory added afterward (measured 12/12; this is what a re-project
            # of a single memory hit). count()==1 after the write means the table is
            # back to one row (fresh-insert-to-1 OR re-upsert-sole-to-1); rebuild so
            # the index is live. Cheap (1 row) and only fires when the whole store
            # holds exactly one memory.
            if self.count() == 1:
                self._rebuild_vector_index()
            self._checkpoint()   # persist the index write so an ungraceful kill can't corrupt it

    def delete_memory(self, record_id: str) -> None:
        with self._lock:
            self._exec("MATCH (m:Memory {id: $id}) DETACH DELETE m", {"id": record_id})
            # RELATES edges live between Entity nodes (not the Memory node), so
            # deleting the memory won't drop them — remove this memory's triples.
            self._exec("MATCH ()-[r:RELATES {mem: $id}]->() DELETE r", {"id": record_id})
            # Emptying the Memory table kills the HNSW vector index on LadybugDB
            # 0.17.1 (see _rebuild_vector_index): without this, "forget all" leaves
            # dense search permanently dead for every memory added afterward.
            # Rebuild the instant the last row goes — cheap on an empty table, and
            # count() is reentrant under self._lock (RLock).
            if self.count() == 0:
                self._rebuild_vector_index()
            self._checkpoint()

    def purge_owner(self, owner: str) -> None:
        """Delete ALL graph state for one agent's silo — Memory nodes + RELATES
        triples scoped to ``owner``. Used when an agent is deleted so the
        rebuildable graph doesn't accumulate orphaned per-agent data."""
        with self._lock:
            self._exec("MATCH (m:Memory {owner: $owner}) DETACH DELETE m", {"owner": owner})
            self._exec("MATCH ()-[r:RELATES {owner: $owner}]->() DELETE r", {"owner": owner})
            if self.count() == 0:                 # see delete_memory: empty table kills HNSW
                self._rebuild_vector_index()

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

    def link_relations(self, *, record_id: str, owner: str, triples: list[dict]) -> None:
        """(Re)write the knowledge-graph triples for one memory: drop the RELATES
        edges this memory previously contributed, then create fresh ones. Entity
        ids are owner-namespaced so two users' graphs never merge. Idempotent —
        re-projecting a memory replaces exactly its own relations."""
        with self._lock:
            self._exec("MATCH ()-[r:RELATES {mem: $mid}]->() DELETE r", {"mid": record_id})
            for t in triples or []:
                s = (t.get("s") or "").strip()
                p = (t.get("p") or "").strip()
                o = (t.get("o") or "").strip()
                if not (s and p and o):
                    continue
                sid, oid = f"kg:{owner}:{_norm(s)}", f"kg:{owner}:{_norm(o)}"
                for eid, name, etype in ((sid, s, t.get("s_type") or "person"),
                                         (oid, o, t.get("o_type") or "topic")):
                    self._exec(
                        "MERGE (e:Entity {id: $id}) "
                        "ON CREATE SET e.name = $n, e.etype = $et, e.normalized = $nm "
                        "ON MATCH SET e.name = $n",
                        {"id": eid, "n": name, "et": etype, "nm": _norm(name)})
                self._exec(
                    "MATCH (s:Entity {id: $s}), (o:Entity {id: $o}) "
                    "CREATE (s)-[:RELATES {predicate: $p, owner: $ow, mem: $mid}]->(o)",
                    {"s": sid, "o": oid, "p": p, "ow": owner, "mid": record_id})

    # ---- reads (owner-scoped) -------------------------------------------
    def vector_search(self, *, owner: str, query_embedding: list[float], limit: int = 5,
                      oversample: int = 5, max_k: int = 4000,
                      max_distance: float | None = None) -> list[VectorHit]:
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
            # Relevance gate: drop hits beyond the cosine-distance threshold —
            # AFTER the grow loop (not inside it) so an off-topic query, whose
            # nearest are all too far, returns [] instead of growing k to max_k
            # hunting for matches that don't exist.
            if max_distance is not None:
                hits = [h for h in hits if h.distance <= max_distance]
            return hits

    def linked_memories(self, *, owner: str, record_ids: list[str], limit: int = 5,
                        max_hops: int = 1) -> list[VectorHit]:
        """Entity-linking boost: memories reachable from the seed memories through
        shared entities, owner-scoped — the GraphRAG co-occurrence signal.

        ``max_hops`` is the number of memory→memory steps through a shared entity:
        hop 1 = "shares an entity with a vector hit" (seed-[MENTIONS]->e<-[MENTIONS]-m);
        hop 2 also reaches a memory that shares an entity with THOSE, and so on.
        The Memory↔Entity graph is bipartite, so a memory→memory walk is always an
        even number of MENTIONS edges → ``max_hops`` memory steps = up to
        ``2*max_hops`` undirected edges; the ``(m:Memory)`` endpoint label drops the
        odd-length paths that land on an entity. Hop 1 keeps the explicit two-edge
        query (the hot path, no recursion); >1 uses a bounded recursive pattern."""
        if not record_ids:
            return []
        hops = max(1, int(max_hops))
        if hops == 1:
            pattern = ("MATCH (seed:Memory)-[:MENTIONS]->(e:Entity)<-[:MENTIONS]-(m:Memory) ")
        else:
            # Bounded recursive walk over the bipartite MENTIONS graph; even lengths
            # land back on a Memory, which the (m:Memory) label enforces.
            pattern = (f"MATCH (seed:Memory)-[:MENTIONS*2..{2 * hops}]-(m:Memory) ")
        with self._lock:
            res = self._exec(
                pattern +
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

    def query_anchored_memories(self, *, owner: str, query: str, limit: int = 5,
                                hub_max_df: float = 0.5) -> list[VectorHit]:
        """GraphRAG recall ANCHORED to entities NAMED IN THE QUERY — not blind
        co-occurrence off the vector seeds (``linked_memories``). Pulls memories
        that MENTION an entity whose normalized name appears as a whole token-run
        in the query.

        Hub entities — mentioned by >= ``hub_max_df`` of the owner's active
        memories (e.g. the user's own name, which co-occurs with nearly every
        personal fact) — are SKIPPED: anchoring on them re-introduces the
        tangential pulls this replaces. (Measured 2026-06-22: the owner's own
        name at 64% df dragged an unrelated career memory into a different topic's
        query via seed co-occurrence.) Returns [] when the query names no non-hub entity → the
        caller falls back to dense + FTS only. One hop (entity → mentioning
        memory): on the bipartite MENTIONS graph deeper walks just re-hit hubs."""
        qn = f" {_norm(query)} "
        with self._lock:
            tot = self.count(owner)
            if tot == 0:
                return []
            hub_cut = max(2, int(tot * hub_max_df))   # floor keeps tiny stores sane
            # Owner-scoped entities + document frequency (how many memories MENTION
            # each). df is the hub signal: a high-df entity carries no discriminative
            # power for co-occurrence recall.
            res = self._exec(
                "MATCH (e:Entity)<-[:MENTIONS]-(m:Memory) "
                "WHERE m.owner = $owner AND m.status = 'active' "
                "RETURN e.id, e.normalized, count(m)", {"owner": owner})
            anchors: list[str] = []
            while res.has_next():
                eid, norm, df = res.get_next()
                if not norm or int(df) >= hub_cut:
                    continue                          # unnamed or hub → drop
                if f" {norm} " in qn:                 # entity named in the query
                    anchors.append(eid)
            if not anchors:
                return []
            res = self._exec(
                "MATCH (e:Entity)<-[:MENTIONS]-(m:Memory) "
                "WHERE e.id IN $ids AND m.owner = $owner AND m.status = 'active' "
                "RETURN DISTINCT m.id, m.owner, m.memory_type, m.content, "
                "m.created_at, m.authority, m.confidence LIMIT $lim",
                {"ids": anchors, "owner": owner, "lim": limit})
            hits = []
            while res.has_next():
                rid, own, mt, content, ca, auth, conf = res.get_next()
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

    def probe_index_healthy(self) -> bool:
        """Real liveness probe for the HNSW index. ``count() > 0`` is NOT enough:
        after an ungraceful kill (LadybugDB/Kùzu vector-index WAL bug — measured
        SIGSEGV/loss on reopen) or a delete-to-empty, the rows survive but
        QUERY_VECTOR_INDEX returns NOTHING — dense recall is silently dead while
        the store still opens and counts fine (the 2026-07 incident). Self-query
        with a stored node's OWN embedding (no embedding model needed): a LIVE
        index must return at least that node; a DEAD index returns []. An empty
        store is trivially healthy. Best-effort — any error → treat as unhealthy
        so the UI fails loud rather than lying green."""
        with self._lock:
            try:
                r = self._exec("MATCH (m:Memory) RETURN m.emb LIMIT 1")
                if not r.has_next():
                    return True                       # nothing indexed → nothing broken
                emb = r.get_next()[0]
                res = self._exec(
                    f"CALL QUERY_VECTOR_INDEX('Memory', '{_VECTOR_INDEX}', $q, 1) "
                    "RETURN node.id", {"q": emb})
                return res.has_next()                 # dead index → no rows → unhealthy
            except Exception as exc:  # noqa: BLE001 — probe failure IS an unhealthy signal
                logger.warning("[MEMORY] index health probe failed: %s", exc)
                return False

    def graph_dump(self, *, owner: str, limit: int = 400) -> dict:
        """Owner-scoped KNOWLEDGE GRAPH for the UI: entity nodes connected by
        typed RELATES edges, i.e. (User)-[likes]->(tea),
        (User)-[works at]->(AcmeCorp). Built from the triples
        projected from each memory's ``relations_json``. Returns ``{nodes,
        edges}``; a node is ``subject`` (the user hub) when it is ever a relation
        source, else ``object``. Duplicate triples collapse to one edge."""
        with self._lock:
            res = self._exec(
                "MATCH (s:Entity)-[r:RELATES]->(o:Entity) WHERE r.owner = $o "
                "RETURN s.id, s.name, o.id, o.name, r.predicate LIMIT $lim",
                {"o": owner, "lim": limit})
            nodes, edges, subjects, seen = {}, [], set(), set()
            while res.has_next():
                sid, sname, oid, oname, pred = res.get_next()
                subjects.add(sid)
                nodes.setdefault(sid, {"id": sid, "label": sname})
                nodes.setdefault(oid, {"id": oid, "label": oname})
                key = (sid, oid, pred)
                if key in seen:
                    continue
                seen.add(key)
                edges.append({"source": sid, "target": oid, "predicate": pred})
            for nid, n in nodes.items():
                n["kind"] = "subject" if nid in subjects else "object"
            return {"nodes": list(nodes.values()), "edges": edges}

    def checkpoint(self) -> None:
        """Flush the WAL into the main DB file so the NEXT open replays nothing.

        The dirty-WAL corruption (wal_record.cpp UNREACHABLE_CODE) happens when a
        process exits without checkpointing; running CHECKPOINT before a graceful
        stop is the documented prevention. Best-effort — a checkpoint failure must
        never block shutdown."""
        con = getattr(self, "_con", None)
        if con is None:
            return
        with self._lock:
            try:
                con.execute("CHECKPOINT")
            except Exception as exc:  # noqa: BLE001
                logger.debug("[ladybug] CHECKPOINT failed: %s", exc)

    def close(self) -> None:
        # CHECKPOINT first so an ungraceful next boot doesn't replay a dirty WAL.
        self.checkpoint()
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


def checkpoint_shared_store() -> None:
    """CHECKPOINT the open singleton (no-op if none) on graceful shutdown so the
    WAL is flushed before the process exits — prevents the dirty-WAL corruption
    that otherwise bricks dense recall on the next boot. Best-effort."""
    if _STORE_SINGLETON is not None:
        try:
            _STORE_SINGLETON.checkpoint()
            logger.info("[MEMORY] LadybugDB checkpoint on shutdown — WAL flushed")
        except Exception as exc:  # noqa: BLE001
            logger.debug("[MEMORY] LadybugDB shutdown checkpoint failed: %s", exc)


def reset_ladybug_store(path: str) -> None:
    """Close (if open) + WIPE the graph projection + drop the singleton, so the
    next ``get_ladybug_store`` rebuilds it from scratch.

    Required when the EMBEDDING MODEL changes: the HNSW vector index is built at
    ``CREATE_VECTOR_INDEX`` time and is effectively STATIC (Kùzu/LadybugDB
    lineage) — re-embedding nodes in place (delete+create) does NOT reliably
    re-index them ('unreachable points' after delete/insert churn; index keeps
    the stale vectors). A clean wipe + re-project from SQLite guarantees the
    index is rebuilt over the NEW vectors. Caller re-enqueues the rebuild."""
    global _STORE_SINGLETON
    if _STORE_SINGLETON is not None:
        try:
            _STORE_SINGLETON.close()
        except Exception:  # noqa: BLE001
            pass
        _STORE_SINGLETON = None
    _wipe_graph_files(path)


class LadybugIndexer:
    """Worker-facing adapter: lets the outbox worker target LadybugDB with the
    same indexer interface (is_available / ensure_collection /
    upsert_points / delete_by_record), so swapping the backend is a drop-in.

    Maps per-chunk points → ONE graph node per memory record. Memories
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
            # Entity ids share the RELATES namespace (kg:{owner}:{norm}) so a
            # MENTIONS entity and the SAME entity used as a triple subject/object
            # are ONE node — the graph view and co-occurrence see a unified graph.
            owner = pl.get("owner_agent_name", "")
            for e in (pl.get("entities") or []):
                name = (e.get("name") or "").strip() if isinstance(e, dict) else ""
                if not name:
                    continue
                norm = _norm(name)
                self._store.link_entity(
                    record_id=rid, entity_id=f"kg:{owner}:{norm}", name=name,
                    etype=(e.get("etype") or "topic"), normalized=norm)
            # Knowledge-graph relations: (re)write this memory's RELATES triples.
            self._store.link_relations(
                record_id=rid, owner=pl.get("owner_agent_name", ""),
                triples=pl.get("relations") or [])

    def delete_by_record(self, record_id: str) -> None:
        self._store.delete_memory(record_id)
