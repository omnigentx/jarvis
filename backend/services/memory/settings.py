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
    # Fast-lane capture frequency gate: run the cheap extractor every N user turns
    # (cost knob — a frequency gate can't misclassify content; the LLM decides).
    extract_every_n: int = 4
    approval_policy: str = "manual"
    pinned_token_budget: int = 1500
    evidence_token_budget: int = 2500
    # Curator LLM — same shape as the existing LLM provider selection.
    curator_model: str = ""   # empty = inherit the main LLM's model (safe default)
    curator_provider: str = ""
    curator_base_url: str = ""
    curator_api_key_set: bool = False  # masked; raw via get_curator_api_key()
    # Embeddings / index. Qwen3-Embedding-0.6B (sentence-transformers, dim 1024 —
    # same as bge-m3, so no LadybugDB schema change) measured 2026-06-22 to
    # separate on-topic/off-topic better than bge-m3 on short Vietnamese
    # question→fact recall. Switching the model re-embeds the whole store
    # (startup migration on a revision change); bge-m3 is still selectable.
    embedding_model: str = "Qwen/Qwen3-Embedding-0.6B"
    embedding_revision: str = ""
    # Cross-encoder reranker: re-ORDERS the fused candidates by joint (query,
    # memory) relevance — the bi-encoder can't. ON by default. It is a RANKER, not
    # a precision GATE: see rerank_min_score.
    reranker_enabled: bool = True
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_top_k: int = 20            # how many fused candidates to rerank
    # Drop candidates scoring below this. Grid-searched 2026-06-23 (real reranker,
    # 78-query labelled set): the reranker score CANNOT separate on-topic from
    # off-topic-keyword — on-topic-INDIRECT targets score as low as 0.0000 ("Repo
    # trợ lý tôi làm nằm ở đâu?" → 0.0000) while off-topic-keyword candidates score
    # up to 0.44 (bge) / 0.98 (Qwen3-Reranker), because those queries are genuinely
    # topically similar. Any floor > 0 that drops the off-topic noise ALSO drops
    # ~20%+ of indirect-phrased on-topic recall (the original recall=0 failure mode
    # — verified: floor 0.005 → on-topic recall@5 drops 100%→80%). So the floor is
    # 0.0: the reranker only ORDERS; the dense gate controls recall/precision.
    # Off-topic-keyword false-positives (a topically-related memory injected for a
    # general question) are an ACCEPTED trade — mild noise the LLM ignores. The
    # real fix is an upstream personal-intent gate (deferred), NOT this floor:
    # an instruction-tuned reranker was tested and did NOT help (it rates topical
    # relevance, which is the wrong question — "is it NEEDED?" needs reasoning).
    rerank_min_score: float = 0.0
    # Dense/graph backend = LadybugDB (embedded property graph + HNSW vectors).
    # ``ladybug_path`` is the embedded DB directory.
    ladybug_path: str = "data/memory_graph"
    # Retention (days)
    retention_episodic_days: int = 90
    retention_retrieval_runs_days: int = 30
    # Recall relevance gate: drop a dense hit whose cosine similarity to the query
    # is below this (distance = 1 - similarity). RECALL-oriented, NOT precision.
    # Grid-searched 2026-06-23 with the real Qwen3-Embedding + bge reranker over a
    # 78-query labelled set (on-topic clear/indirect + off-topic clear/keyword-
    # overlap, real memories): on-topic recall@5 is a FLAT 100% across [0.05, 0.39]
    # and only falls off from 0.40 (the cliff). The dense gate therefore does NOT
    # bound recall anywhere in that band — it is a candidate-pool / false-positive
    # knob. 0.30 sits mid-plateau: ~0.10 margin below the 0.40 cliff (robust to
    # unseen / semantic-only phrasings the labelled set under-represents) while
    # trimming more off-topic-clear noise than a very low gate. The old 0.34 sat
    # right at the cliff; bge-m3's scale was higher (~0.44 — gate_mistuned_warning
    # flags the mismatch). NOTE: off-topic-KEYWORD rejection is intentionally NOT
    # solved here — the reranker scores topical similarity, which those queries
    # genuinely have; see rerank_min_score.
    recall_min_similarity: float = 0.30
    # GraphRAG traversal depth for RELATES expansion. 1 is the sweet spot for the
    # star-shaped personal graph (the user hub is a super-node — 2 hops through it
    # pulls the whole profile = noise). Bump to 2 only with hop-decay.
    graph_max_hops: int = 1
    # GraphRAG hub suppression: an entity mentioned by >= this fraction of the
    # owner's active memories (e.g. the user's own name) is a hub that co-occurs
    # with everything → carries no signal → excluded from query-anchored expansion.
    hub_max_df: float = 0.5
    # Tuning overrides (JSON)
    trigger_lexicon_overrides: dict = field(default_factory=dict)
    quality_gate_thresholds: dict = field(default_factory=dict)


# key -> (kind, default). kind in {bool,int,str,json}.
_SCHEMA: dict[str, tuple[str, Any]] = {
    "enabled": ("bool", False),
    "mode": ("str", RetrievalMode.BALANCED.value),
    "auto_capture_preferences": ("bool", True),
    "extract_every_n": ("int", 4),
    "approval_policy": ("str", "manual"),
    "pinned_token_budget": ("int", 1500),
    "evidence_token_budget": ("int", 2500),
    "curator_model": ("str", ""),
    "curator_provider": ("str", ""),
    "curator_base_url": ("str", ""),
    "embedding_model": ("str", "Qwen/Qwen3-Embedding-0.6B"),
    "embedding_revision": ("str", ""),
    "reranker_enabled": ("bool", True),
    "rerank_model": ("str", "BAAI/bge-reranker-v2-m3"),
    "rerank_top_k": ("int", 20),
    "rerank_min_score": ("float", 0.0),
    "ladybug_path": ("str", "data/memory_graph"),
    "retention_episodic_days": ("int", 90),
    "retention_retrieval_runs_days": ("int", 30),
    "recall_min_similarity": ("float", 0.30),
    "graph_max_hops": ("int", 1),
    "hub_max_df": ("float", 0.5),
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
    if "hub_max_df" in updates:
        v = updates["hub_max_df"]
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not (0.0 < float(v) <= 1.0):
            errors.append("hub_max_df must be a number in (0, 1]")
    for k in ("extract_every_n", "rerank_top_k"):
        if k in updates and (not isinstance(updates[k], int) or isinstance(updates[k], bool)
                             or updates[k] < 1):
            errors.append(f"{k} must be an integer >= 1")
    if "rerank_min_score" in updates:
        v = updates["rerank_min_score"]
        if not isinstance(v, (int, float)) or isinstance(v, bool) or float(v) < 0.0:
            errors.append("rerank_min_score must be a number >= 0")
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


def gate_mistuned_warning(embedding_model: str, recall_min_similarity: float) -> str | None:
    """Advisory check (review #5): the recall gate is a cosine-similarity floor on
    the EMBEDDING's score scale, which differs by model (bge-m3 ~0.44,
    Qwen3-Embedding ~0.34). ``embedding_model`` is independently configurable, so
    swapping the model without re-tuning the gate leaves it mistuned (e.g. bge-m3
    at a Qwen 0.34 floor injects loosely-related rows). Returns a warning string
    when the pair looks mismatched, else None — heuristic, never raises."""
    m = (embedding_model or "").lower()
    if "bge-m3" in m and recall_min_similarity < 0.40:
        return (f"recall_min_similarity={recall_min_similarity} is Qwen-scale but "
                f"embedding_model is bge-m3 (expected ~0.44) — gate too permissive")
    if "qwen" in m and recall_min_similarity > 0.42:
        return (f"recall_min_similarity={recall_min_similarity} is bge-scale but "
                f"embedding_model is {embedding_model} (expected ~0.34) — gate too strict")
    return None
