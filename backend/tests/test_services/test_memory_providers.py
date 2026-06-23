"""WS03 providers: degraded behavior when the embedding deps are absent."""

import pytest

from services.indexing import embedding_provider as ep


def test_embedding_factory_returns_null_when_deps_missing(monkeypatch):
    # Force "the model's backend package is missing" regardless of environment
    # (dispatch is per-model now: bge-m3→FlagEmbedding, else→sentence-transformers).
    monkeypatch.setattr(ep, "_have", lambda pkg: False)
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
