"""Ensure the CONFIGURED memory models are present + warm — at startup and on a
config change — instead of baking a fixed model list into the Docker image.

Why this exists
---------------
The reranker/embedding defaults change over time (a Docker-baked list is a second
source of truth that silently drifts from the config default — exactly how a new
default reranker once shipped without being on the server). Here the model set is
derived from ONE source: the live memory settings. The HF cache is a persistent
volume (see docker-compose.yaml), so a model downloaded once survives deploys and
the next startup is a fast no-op.

Non-blocking by design: each model is downloaded + warmed in a daemon thread and
streams byte-level progress to the activity SSE (the Settings UI shows a progress
bar). The server becomes ready immediately; recall degrades gracefully (keeps
fusion order / skips rerank) until a model is ready.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Callable, Optional

from services.activity_stream import activity_stream_manager

logger = logging.getLogger("memory")

# SSE event types the frontend already routes to the memory store (it gates
# forwarding on event_type.startswith("memory_")).
_RERANKER_EVENT = "memory_reranker_loading"
_EMBEDDING_EVENT = "memory_embedding_loading"


def make_sse_progress(
    event_type: str, model_name: str, loop: Optional[asyncio.AbstractEventLoop]
) -> Callable[[str, int], None]:
    """Build an ``on_progress(state, pct)`` that broadcasts model-load progress
    to the activity SSE. ``broadcast`` touches asyncio.Queues that are not
    thread-safe to write from a worker thread, so when a loop is supplied the
    event is marshalled back onto it with ``call_soon_threadsafe``."""
    def on_progress(state: str, pct: int) -> None:
        ev = {"event_type": event_type, "state": state,
              "progress": pct, "model": model_name}
        if loop is not None:
            loop.call_soon_threadsafe(activity_stream_manager.broadcast, ev)
        else:
            activity_stream_manager.broadcast(ev)
    return on_progress


def prefetch_embedding(model_name: str, revision: str, on_progress) -> None:
    """Download (with byte-level progress) then warm the embedding model, so the
    first index write / recall never pays the download+load on the request path.
    Mirrors :func:`services.retrieval.reranker.prefetch_and_warm`. Best-effort:
    any failure ends in a single ``error`` event and is logged, never raised
    (the embedding still loads lazily on first use as a fallback).

    MUST run OFF the event loop (blocks on network + a model load).
    """
    state = {"done": 0, "total": 0, "last_pct": -1}

    def _emit(st: str, pct: int) -> None:
        if st == "downloading" and pct == state["last_pct"]:
            return  # throttle: only fire when the integer pct advances
        state["last_pct"] = pct
        try:
            on_progress(st, pct)
        except Exception:  # noqa: BLE001 — progress is best-effort, never break warm
            logger.debug("[MEMORY] embedding progress callback failed", exc_info=True)

    try:
        _emit("downloading", 0)
        try:
            from huggingface_hub import snapshot_download
            from tqdm.auto import tqdm as _tqdm

            # Aggregate byte progress across HF's per-file bars; ignore the outer
            # "Fetching N files" bar (unit 'it') so it doesn't skew the total.
            class _ProgressTqdm(_tqdm):
                def __init__(self, *a, **k):
                    super().__init__(*a, **k)
                    if self.unit == "B":
                        state["total"] += (self.total or 0)

                def update(self, n=1):
                    r = super().update(n)
                    if self.unit == "B" and state["total"]:
                        state["done"] += n
                        _emit("downloading",
                              min(99, int(state["done"] * 100 / state["total"])))
                    return r

            kwargs = {"repo_id": model_name, "tqdm_class": _ProgressTqdm}
            if revision:
                kwargs["revision"] = revision
            snapshot_download(**kwargs)
        except Exception as exc:  # noqa: BLE001 — already cached / offline / hub error
            logger.info("[MEMORY] embedding prefetch skipped/failed (%s) — trying load", exc)

        _emit("loading", max(state["last_pct"], 99))
        from services.indexing.embedding_provider import get_shared_embedding_provider
        prov = get_shared_embedding_provider(model_name, revision)
        if not prov.is_available():
            raise RuntimeError("embedding provider unavailable after load (missing dep?)")
        prov.embed_query("warm")  # forces the weights into RAM
        _emit("ready", 100)
        logger.info("[MEMORY] embedding '%s' downloaded + warmed", model_name)
    except Exception as exc:  # noqa: BLE001 — surface one error event, never crash startup
        logger.warning("[MEMORY] embedding prefetch_and_warm failed: %s", exc)
        try:
            on_progress("error", 0)
        except Exception:  # noqa: BLE001
            pass


def ensure_models_warm(settings, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
    """Download + warm every memory model the CURRENT settings require, each in
    its own daemon thread with SSE progress. Safe to call repeatedly (a cached
    model is a fast no-op) — used at server startup and after a settings change.

    Resolve the running loop on the caller's thread (the event loop), because the
    worker threads can only marshal progress back via a loop captured here.
    """
    if not getattr(settings, "enabled", False):
        return
    if loop is None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

    # Embedding powers dense recall + the knowledge graph — always needed.
    emb_model = getattr(settings, "embedding_model", None)
    if emb_model:
        emb_rev = getattr(settings, "embedding_revision", "") or ""
        on_prog = make_sse_progress(_EMBEDDING_EVENT, emb_model, loop)
        threading.Thread(
            target=prefetch_embedding, args=(emb_model, emb_rev, on_prog),
            name="embedding-prefetch", daemon=True,
        ).start()
        logger.info("[MEMORY] embedding prefetch/warm kicked off: %s", emb_model)

    # Reranker only when enabled. Reuse the reranker's own download+warm.
    if getattr(settings, "reranker_enabled", False):
        rr_model = getattr(settings, "rerank_model", None)
        if rr_model:
            from services.retrieval.reranker import prefetch_and_warm
            on_prog = make_sse_progress(_RERANKER_EVENT, rr_model, loop)
            threading.Thread(
                target=prefetch_and_warm, args=(rr_model, on_prog),
                name="reranker-prefetch", daemon=True,
            ).start()
            logger.info("[MEMORY] reranker prefetch/warm kicked off: %s", rr_model)
