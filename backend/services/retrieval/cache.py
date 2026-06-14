"""Per-agent retrieval cache (spec §10). Keyed by owner + normalized query +
filters + memory index revision + policy version. Invalidated by revision
change, NOT TTL alone; never shared across agents.
"""
from __future__ import annotations

import hashlib
from collections import OrderedDict

from services.retrieval.contracts import Evidence

POLICY_VERSION = "1"
_MAX_ENTRIES = 512


def cache_key(*, owner_agent_name: str, normalized_query: str, filters: str,
              index_revision: int, policy_version: str = POLICY_VERSION) -> str:
    raw = f"{owner_agent_name}\x1f{normalized_query}\x1f{filters}\x1f{index_revision}\x1f{policy_version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_query(query: str) -> str:
    return " ".join((query or "").lower().split())


class RetrievalCache:
    """Small in-process LRU. The key embeds the index revision, so a stale
    entry is simply never hit again after an index update (no explicit
    invalidation needed)."""

    def __init__(self, max_entries: int = _MAX_ENTRIES):
        self._data: "OrderedDict[str, list[Evidence]]" = OrderedDict()
        self._max = max_entries

    def get(self, key: str) -> list[Evidence] | None:
        if key not in self._data:
            return None
        self._data.move_to_end(key)
        return self._data[key]

    def set(self, key: str, evidence: list[Evidence]) -> None:
        self._data[key] = evidence
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()
