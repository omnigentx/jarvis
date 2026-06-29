"""Per-agent retrieval cache (spec §10). Keyed by owner + normalized query +
filters + memory index revision + recall-settings fingerprint + policy version.
Invalidated by an index-revision OR a recall-settings change, NOT TTL alone;
never shared across agents.
"""
from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict

from services.retrieval.contracts import Evidence

POLICY_VERSION = "1"
_MAX_ENTRIES = 512

# Memory settings whose value changes WHAT recall returns for the same query +
# index revision: the reranker on/off + its knobs, the relevance gate, the dense
# embedding model, and the graph/budget/gate parameters. A change to any of these
# must invalidate cached results — the cache key embeds settings_fingerprint(),
# so a stale entry is simply never hit again (the SAME mechanism as
# index_revision: no explicit clear, works regardless of process boundaries).
# Capture/curator/retention/approval settings are deliberately NOT here — they
# don't touch the recall read path, so folding them in would bust the cache for
# no reason. Add a new recall-affecting setting → add its name here (one line).
_RECALL_AFFECTING_KEYS = (
    "reranker_enabled", "rerank_model", "rerank_top_k", "rerank_min_score",
    "recall_min_similarity", "graph_max_hops", "hub_max_df",
    "embedding_model", "embedding_revision",
    "evidence_token_budget", "quality_gate_thresholds", "trigger_lexicon_overrides",
)


def settings_fingerprint(settings) -> str:
    """Short, stable hash of the recall-affecting settings (``_RECALL_AFFECTING_KEYS``).
    Folded into :func:`cache_key` so a settings edit (e.g. enabling the reranker)
    invalidates cached recalls for the same query — the next call is a guaranteed
    miss and reflects the new config immediately, no explicit cache clear."""
    payload = {k: getattr(settings, k, None) for k in _RECALL_AFFECTING_KEYS}
    raw = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def cache_key(*, owner_agent_name: str, normalized_query: str, filters: str,
              index_revision: int, settings_fp: str = "",
              policy_version: str = POLICY_VERSION) -> str:
    raw = (f"{owner_agent_name}\x1f{normalized_query}\x1f{filters}\x1f"
           f"{index_revision}\x1f{settings_fp}\x1f{policy_version}")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_query(query: str) -> str:
    return " ".join((query or "").lower().split())


class RetrievalCache:
    """Small in-process LRU. The key embeds the index revision AND a
    recall-settings fingerprint, so a stale entry is simply never hit again after
    an index update or a settings change (no explicit invalidation needed)."""

    def __init__(self, max_entries: int = _MAX_ENTRIES):
        self._data: "OrderedDict[str, list[Evidence]]" = OrderedDict()
        self._max = max_entries
        # The OrderedDict is mutated by both the asyncio request path and the
        # index worker's own loop/thread; guard get/set so a concurrent
        # move_to_end/popitem can't corrupt the LRU ("RuntimeError: OrderedDict
        # mutated during iteration" / lost entries).
        self._lock = threading.Lock()

    def get(self, key: str) -> list[Evidence] | None:
        with self._lock:
            if key not in self._data:
                return None
            self._data.move_to_end(key)
            return self._data[key]

    def set(self, key: str, evidence: list[Evidence]) -> None:
        with self._lock:
            self._data[key] = evidence
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
