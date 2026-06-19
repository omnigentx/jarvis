"""WS03 providers: degraded behavior when embedding/qdrant deps are absent."""

import pytest

from services.indexing import embedding_provider as ep
from services.indexing import qdrant_indexer as qi


def test_embedding_factory_returns_null_when_deps_missing(monkeypatch):
    # Force "deps missing" regardless of environment.
    monkeypatch.setattr(ep, "_deps_available", lambda: False)
    prov = ep.get_embedding_provider()
    assert isinstance(prov, ep.NullEmbeddingProvider)
    assert prov.is_available() is False
    assert prov.dim() == ep.BGE_M3_DIM
    with pytest.raises(RuntimeError, match="unavailable"):
        prov.embed_query("hi")


def test_bge_refuses_outside_main_process(monkeypatch):
    monkeypatch.delenv(ep.MAIN_PROCESS_ENV, raising=False)
    prov = ep.BGEEmbeddingProvider()
    assert prov.is_available() is True          # provider exists...
    with pytest.raises(RuntimeError, match="outside the main backend process"):
        prov.embed_query("hi")                  # ...but model load is guarded


def test_qdrant_unavailable_when_deps_missing(monkeypatch):
    monkeypatch.setattr(qi, "client_deps_available", lambda: False)
    idx = qi.get_qdrant_indexer()
    assert idx.is_available() is False


def test_qdrant_point_id_deterministic():
    a = qi.point_id("rec-1", 0, 1)
    b = qi.point_id("rec-1", 0, 1)
    c = qi.point_id("rec-1", 1, 1)
    assert a == b and a != c
    # valid uuid string
    import uuid
    uuid.UUID(a)
