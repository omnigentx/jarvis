"""Memory settings — typed facade over the generic config DB (category
``memory``), mirroring services.context_compaction's settings pattern.

Single source for defaults + validation, shared by the HTTP route and any
backend caller (router, indexer, curator). The feature flag ``enabled``
gates the entire subsystem (default OFF).

See docs/agent-memory-adaptive-rag-spec.md §18.3 and docs/memory-impl/02.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from services.config_service import config_service
from services.retrieval.contracts import RetrievalMode

MEMORY_CATEGORY = "memory"
_CURATOR_API_KEY = "curator_api_key"  # stored is_secret=True, never returned plain

_VALID_MODES = {m.value for m in RetrievalMode}
_VALID_APPROVAL = {"manual", "auto_low_risk"}


@dataclass
class MemorySettings:
    enabled: bool = False
    mode: str = RetrievalMode.BALANCED.value
    auto_capture_preferences: bool = True
    approval_policy: str = "manual"
    pinned_token_budget: int = 1500
    evidence_token_budget: int = 2500
    # Curator LLM — same shape as the existing LLM provider selection.
    curator_model: str = ""   # empty = inherit the main LLM's model (safe default)
    curator_provider: str = ""
    curator_base_url: str = ""
    curator_api_key_set: bool = False  # masked; raw via get_curator_api_key()
    # Embeddings / index
    embedding_model: str = "BAAI/bge-m3"
    embedding_revision: str = ""
    reranker_enabled: bool = True
    # Vector/graph backend: "ladybug" (embedded graph + HNSW, memory v2) or
    # "qdrant" (legacy dense-only). ``ladybug_path`` = the embedded DB dir.
    vector_backend: str = "ladybug"
    ladybug_path: str = "data/memory_graph"
    qdrant_url: str = "http://localhost:6333"
    # Retention (days)
    retention_episodic_days: int = 90
    retention_retrieval_runs_days: int = 30
    # Recall relevance gate: drop a dense hit whose cosine similarity to the
    # query is below this (distance = 1 - similarity). Calibrated 2026-06-17 on
    # real BGE-M3 distances: on-topic queries match ≤0.53 distance, off-topic
    # ≥0.59 — so 0.44 similarity (≤0.56 distance) keeps on-topic, drops off-topic
    # (→ no memory injected for an unrelated turn).
    recall_min_similarity: float = 0.44
    # GraphRAG traversal depth for RELATES expansion. 1 is the sweet spot for the
    # star-shaped personal graph (the user hub is a super-node — 2 hops through it
    # pulls the whole profile = noise). Bump to 2 only with hop-decay.
    graph_max_hops: int = 1
    # Tuning overrides (JSON)
    trigger_lexicon_overrides: dict = field(default_factory=dict)
    quality_gate_thresholds: dict = field(default_factory=dict)


# key -> (kind, default). kind in {bool,int,str,json}.
_SCHEMA: dict[str, tuple[str, Any]] = {
    "enabled": ("bool", False),
    "mode": ("str", RetrievalMode.BALANCED.value),
    "auto_capture_preferences": ("bool", True),
    "approval_policy": ("str", "manual"),
    "pinned_token_budget": ("int", 1500),
    "evidence_token_budget": ("int", 2500),
    "curator_model": ("str", ""),
    "curator_provider": ("str", ""),
    "curator_base_url": ("str", ""),
    "embedding_model": ("str", "BAAI/bge-m3"),
    "embedding_revision": ("str", ""),
    "reranker_enabled": ("bool", True),
    "vector_backend": ("str", "ladybug"),
    "ladybug_path": ("str", "data/memory_graph"),
    "qdrant_url": ("str", "http://localhost:6333"),
    "retention_episodic_days": ("int", 90),
    "retention_retrieval_runs_days": ("int", 30),
    "recall_min_similarity": ("float", 0.44),
    "graph_max_hops": ("int", 1),
    "trigger_lexicon_overrides": ("json", {}),
    "quality_gate_thresholds": ("json", {}),
}


def _coerce_read(kind: str, raw: str | None, default: Any) -> Any:
    if raw is None:
        return default
    if kind == "bool":
        return raw.lower() in ("1", "true", "yes", "on")
    if kind == "int":
        try:
            return int(raw)
        except ValueError:
            return default
    if kind == "float":
        try:
            return float(raw)
        except ValueError:
            return default
    if kind == "json":
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return default
    return raw


def _coerce_write(kind: str, value: Any) -> str:
    if kind == "bool":
        return "true" if value else "false"
    if kind == "int":
        return str(int(value))
    if kind == "float":
        return str(float(value))
    if kind == "json":
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def get_memory_settings() -> MemorySettings:
    """Read all keys (DB value or default) and the api-key presence flag."""
    values: dict[str, Any] = {}
    for key, (kind, default) in _SCHEMA.items():
        values[key] = _coerce_read(kind, config_service.get(MEMORY_CATEGORY, key), default)
    entry = config_service.get_entry(MEMORY_CATEGORY, _CURATOR_API_KEY)
    values["curator_api_key_set"] = entry is not None
    return MemorySettings(**values)


def get_curator_api_key() -> str | None:
    """Decrypted curator API key for backend use only (never sent to UI)."""
    return config_service.get(MEMORY_CATEGORY, _CURATOR_API_KEY)


def validate_settings_updates(updates: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if "mode" in updates and updates["mode"] not in _VALID_MODES:
        errors.append(f"mode must be one of {sorted(_VALID_MODES)}")
    if "approval_policy" in updates and updates["approval_policy"] not in _VALID_APPROVAL:
        errors.append(f"approval_policy must be one of {sorted(_VALID_APPROVAL)}")
    for k in ("pinned_token_budget", "evidence_token_budget",
              "retention_episodic_days", "retention_retrieval_runs_days"):
        if k in updates and (not isinstance(updates[k], int) or updates[k] < 0):
            errors.append(f"{k} must be a non-negative integer")
    if "recall_min_similarity" in updates:
        v = updates["recall_min_similarity"]
        if not isinstance(v, (int, float)) or not (0.0 <= float(v) <= 1.0):
            errors.append("recall_min_similarity must be a number in [0, 1]")
    if "graph_max_hops" in updates:
        v = updates["graph_max_hops"]
        if not isinstance(v, int) or isinstance(v, bool) or not (1 <= v <= 3):
            errors.append("graph_max_hops must be an integer in [1, 3]")
    unknown = set(updates) - set(_SCHEMA) - {_CURATOR_API_KEY}
    if unknown:
        errors.append(f"unknown settings: {sorted(unknown)}")
    return errors


def update_memory_settings(updates: dict[str, Any], *, user: str = "user") -> MemorySettings:
    """Validate + persist changed keys (PATCH semantics). The curator API key
    is written separately as a secret; passing ``curator_api_key`` with an
    empty string deletes it."""
    errors = validate_settings_updates(updates)
    if errors:
        raise ValueError("; ".join(errors))

    for key, value in updates.items():
        if key == _CURATOR_API_KEY:
            # Empty string clears the secret; otherwise store encrypted.
            config_service.set(
                MEMORY_CATEGORY, key, (value or None),
                is_secret=True, user=user,
            )
            continue
        kind, _ = _SCHEMA[key]
        config_service.set(MEMORY_CATEGORY, key, _coerce_write(kind, value), user=user)

    return get_memory_settings()
