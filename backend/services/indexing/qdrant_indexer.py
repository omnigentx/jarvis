"""Qdrant index management (spec §8.1). One collection per embedding schema
version; agent isolation via mandatory payload filters (NOT per-agent
collections). Lazy/guarded so the module imports and reports unavailable when
qdrant-client or the server is absent — the system then degrades to FTS5.
"""
from __future__ import annotations

import importlib.util
import logging
import uuid

logger = logging.getLogger("memory.qdrant")

# Collection name carries the embedding schema version. A model/revision change
# = a NEW collection + rebuild (never a silently mixed vector space).
COLLECTION = "jarvis_memory_bge_m3_v1"
DENSE_VECTOR = "dense"
SPARSE_VECTOR = "bm25"

# Payload fields that get an index (spec §8.1).
_PAYLOAD_INDEX_FIELDS = [
    "owner_agent_name", "memory_type", "subject_scope", "source_type",
    "status", "authority", "created_at", "embedding_revision",
]

_NS = uuid.UUID("a4f0c2e1-0000-4000-8000-000000000000")  # stable namespace


def point_id(record_id: str, chunk_ordinal: int, index_revision: int) -> str:
    """Deterministic point id so upserts are idempotent (re-running the same
    work overwrites rather than duplicates)."""
    return str(uuid.uuid5(_NS, f"{record_id}:{chunk_ordinal}:{index_revision}"))


def client_deps_available() -> bool:
    return importlib.util.find_spec("qdrant_client") is not None


class QdrantIndexer:
    def __init__(self, url: str = "http://localhost:6333", dim: int = 1024):
        self._url = url
        self._dim = dim
        self._client = None
        self._checked = False
        self._available = False

    def _get_client(self):
        if self._client is None:
            from qdrant_client import QdrantClient  # lazy
            self._client = QdrantClient(url=self._url, timeout=5.0)
        return self._client

    def is_available(self) -> bool:
        """Deps installed AND server reachable. Cached after first probe;
        callers re-instantiate to re-probe after an outage."""
        if self._checked:
            return self._available
        self._checked = True
        if not client_deps_available():
            self._available = False
            return False
        try:
            self._get_client().get_collections()
            self._available = True
        except Exception as exc:
            logger.warning("[MEMORY] Qdrant unreachable at %s: %s", self._url, exc)
            self._available = False
        return self._available

    def ensure_collection(self) -> None:
        from qdrant_client import models
        client = self._get_client()
        existing = {c.name for c in client.get_collections().collections}
        if COLLECTION not in existing:
            client.create_collection(
                collection_name=COLLECTION,
                vectors_config={DENSE_VECTOR: models.VectorParams(
                    size=self._dim, distance=models.Distance.COSINE)},
                sparse_vectors_config={SPARSE_VECTOR: models.SparseVectorParams()},
            )
            for field in _PAYLOAD_INDEX_FIELDS:
                try:
                    client.create_payload_index(
                        collection_name=COLLECTION, field_name=field,
                        field_schema=models.PayloadSchemaType.KEYWORD
                        if field != "created_at" else models.PayloadSchemaType.FLOAT,
                    )
                except Exception:
                    pass
            logger.info("[MEMORY] Created Qdrant collection %s", COLLECTION)

    def upsert_points(self, points: list[dict]) -> None:
        """``points``: list of {id, dense (list[float]), payload (dict),
        sparse (optional {indices, values})}."""
        from qdrant_client import models
        client = self._get_client()
        qpoints = []
        for p in points:
            vectors: dict = {DENSE_VECTOR: p["dense"]}
            if p.get("sparse"):
                vectors[SPARSE_VECTOR] = models.SparseVector(
                    indices=p["sparse"]["indices"], values=p["sparse"]["values"])
            qpoints.append(models.PointStruct(
                id=p["id"], vector=vectors, payload=p["payload"]))
        client.upsert(collection_name=COLLECTION, points=qpoints)

    def delete_by_record(self, record_id: str) -> None:
        from qdrant_client import models
        client = self._get_client()
        client.delete(
            collection_name=COLLECTION,
            points_selector=models.FilterSelector(filter=models.Filter(
                must=[models.FieldCondition(
                    key="record_id", match=models.MatchValue(value=record_id))])),
        )


def get_qdrant_indexer(url: str = "http://localhost:6333", dim: int = 1024) -> QdrantIndexer:
    return QdrantIndexer(url=url, dim=dim)
