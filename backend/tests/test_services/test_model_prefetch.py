"""Tests for services.memory.model_prefetch — runtime model verify/warm that
replaces baking models into the Docker image."""
from __future__ import annotations

from types import SimpleNamespace

import services.memory.model_prefetch as mp


class _InlineThread:
    """Run the thread target synchronously so we can assert what was kicked."""
    def __init__(self, target=None, args=(), **_kw):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


def _settings(**kw):
    base = dict(enabled=True, embedding_model="emb/model", embedding_revision="",
                reranker_enabled=True, rerank_model="rr/model")
    base.update(kw)
    return SimpleNamespace(**base)


def test_ensure_models_warm_kicks_both_from_config(monkeypatch):
    calls = []
    monkeypatch.setattr(mp, "prefetch_embedding",
                        lambda m, r, cb: calls.append(("emb", m, r)))
    import services.retrieval.reranker as rr
    monkeypatch.setattr(rr, "prefetch_and_warm", lambda m, cb: calls.append(("rr", m)))
    monkeypatch.setattr(mp.threading, "Thread", _InlineThread)

    mp.ensure_models_warm(_settings(embedding_revision="r1"), loop=None)

    assert ("emb", "emb/model", "r1") in calls
    assert ("rr", "rr/model") in calls


def test_ensure_models_warm_skips_when_memory_disabled(monkeypatch):
    calls = []
    monkeypatch.setattr(mp, "prefetch_embedding", lambda *a: calls.append("emb"))
    monkeypatch.setattr(mp.threading, "Thread", _InlineThread)
    mp.ensure_models_warm(_settings(enabled=False), loop=None)
    assert calls == []


def test_ensure_models_warm_skips_reranker_when_disabled(monkeypatch):
    calls = []
    monkeypatch.setattr(mp, "prefetch_embedding", lambda m, r, cb: calls.append(("emb", m)))
    import services.retrieval.reranker as rr
    monkeypatch.setattr(rr, "prefetch_and_warm", lambda m, cb: calls.append(("rr", m)))
    monkeypatch.setattr(mp.threading, "Thread", _InlineThread)

    mp.ensure_models_warm(_settings(reranker_enabled=False), loop=None)

    assert ("emb", "emb/model") in calls
    assert not any(c[0] == "rr" for c in calls)


def test_prefetch_embedding_emits_phases_in_order(monkeypatch):
    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda **k: None)
    import services.indexing.embedding_provider as ep
    prov = SimpleNamespace(is_available=lambda: True, embed_query=lambda t: [0.0])
    monkeypatch.setattr(ep, "get_shared_embedding_provider", lambda m, r: prov)

    states = []
    mp.prefetch_embedding("emb/model", "", lambda st, pct: states.append(st))

    assert states[0] == "downloading"
    assert "loading" in states
    assert states[-1] == "ready"


def test_prefetch_embedding_emits_error_on_unavailable(monkeypatch):
    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda **k: None)
    import services.indexing.embedding_provider as ep
    prov = SimpleNamespace(is_available=lambda: False)  # triggers the RuntimeError path
    monkeypatch.setattr(ep, "get_shared_embedding_provider", lambda m, r: prov)

    states = []
    mp.prefetch_embedding("emb/model", "", lambda st, pct: states.append(st))

    assert states[-1] == "error"


def test_make_sse_progress_broadcasts_event(monkeypatch):
    sent = []
    monkeypatch.setattr(mp.activity_stream_manager, "broadcast", lambda ev: sent.append(ev))
    on_prog = mp.make_sse_progress("memory_embedding_loading", "emb/model", None)
    on_prog("downloading", 42)
    assert sent == [{"event_type": "memory_embedding_loading", "state": "downloading",
                     "progress": 42, "model": "emb/model"}]
