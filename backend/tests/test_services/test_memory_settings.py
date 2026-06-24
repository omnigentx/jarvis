"""WS02 settings: defaults, coercion, validation, secret masking, feature flag.

Uses an in-memory fake config store so the test exercises the settings module
logic without DB coupling (config_service itself is covered elsewhere)."""

import pytest

import services.memory.settings as ms


class _FakeEntry:
    pass


class _FakeConfig:
    def __init__(self):
        self.store: dict[tuple[str, str], tuple[str | None, bool]] = {}

    def get(self, category, key, *, default=None):
        v = self.store.get((category, key))
        return default if v is None else v[0]

    def get_entry(self, category, key):
        v = self.store.get((category, key))
        return _FakeEntry() if (v is not None and v[0] is not None) else None

    def set(self, category, key, value, *, is_secret=False, source="user", user="user"):
        if value is None:
            self.store.pop((category, key), None)
        else:
            self.store[(category, key)] = (value, is_secret)


@pytest.fixture()
def fake_cfg(monkeypatch):
    cfg = _FakeConfig()
    monkeypatch.setattr(ms, "config_service", cfg)
    return cfg


def test_defaults_when_empty(fake_cfg):
    s = ms.get_memory_settings()
    assert s.enabled is False           # feature flag OFF by default
    assert s.mode == "balanced"
    assert s.pinned_token_budget == 1500
    assert s.embedding_model == "Qwen/Qwen3-Embedding-0.6B"
    assert s.curator_api_key_set is False


def test_patch_round_trip_and_coercion(fake_cfg):
    ms.update_memory_settings({
        "enabled": True,
        "mode": "deep",
        "pinned_token_budget": 800,
        "trigger_lexicon_overrides": {"vi": ["lần trước"]},
    })
    s = ms.get_memory_settings()
    assert s.enabled is True
    assert s.mode == "deep"
    assert s.pinned_token_budget == 800
    assert s.trigger_lexicon_overrides == {"vi": ["lần trước"]}
    # stored as strings under the hood
    assert fake_cfg.store[("memory", "enabled")][0] == "true"
    assert fake_cfg.store[("memory", "pinned_token_budget")][0] == "800"


def test_recall_gate_and_hops_round_trip_and_validation(fake_cfg):
    ms.update_memory_settings({"recall_min_similarity": 0.5, "graph_max_hops": 2})
    s = ms.get_memory_settings()
    assert s.recall_min_similarity == 0.5 and s.graph_max_hops == 2
    assert fake_cfg.store[("memory", "recall_min_similarity")][0] == "0.5"   # float coercion
    # defaults
    fake_cfg.store.clear()
    d = ms.get_memory_settings()
    assert d.recall_min_similarity == 0.30 and d.graph_max_hops == 1
    # range validation
    with pytest.raises(ValueError, match="recall_min_similarity"):
        ms.update_memory_settings({"recall_min_similarity": 1.5})
    with pytest.raises(ValueError, match="graph_max_hops"):
        ms.update_memory_settings({"graph_max_hops": 0})
    with pytest.raises(ValueError, match="graph_max_hops"):
        ms.update_memory_settings({"graph_max_hops": 5})


def test_invalid_mode_rejected(fake_cfg):
    with pytest.raises(ValueError, match="mode must be"):
        ms.update_memory_settings({"mode": "turbo"})


def test_invalid_approval_and_negative_budget(fake_cfg):
    with pytest.raises(ValueError, match="approval_policy"):
        ms.update_memory_settings({"approval_policy": "auto"})
    with pytest.raises(ValueError, match="non-negative"):
        ms.update_memory_settings({"evidence_token_budget": -1})


def test_unknown_key_rejected(fake_cfg):
    with pytest.raises(ValueError, match="unknown settings"):
        ms.update_memory_settings({"nope": 1})


def test_curator_api_key_is_secret_and_masked(fake_cfg):
    ms.update_memory_settings({"curator_api_key": "sk-secret"})
    # stored as secret
    assert fake_cfg.store[("memory", "curator_api_key")] == ("sk-secret", True)
    # masked on read: only presence flag, never the value
    s = ms.get_memory_settings()
    assert s.curator_api_key_set is True
    assert not hasattr(s, "curator_api_key")
    assert ms.get_curator_api_key() == "sk-secret"
    # empty string clears it
    ms.update_memory_settings({"curator_api_key": ""})
    assert ms.get_memory_settings().curator_api_key_set is False


def test_gate_mistuned_warning():
    # Review #5: advisory mismatch between embedding model and the gate scale.
    from services.memory.settings import gate_mistuned_warning
    assert gate_mistuned_warning("Qwen/Qwen3-Embedding-0.6B", 0.34) is None   # matched
    assert gate_mistuned_warning("BAAI/bge-m3", 0.44) is None                 # matched
    assert gate_mistuned_warning("BAAI/bge-m3", 0.34) is not None             # bge at Qwen floor
    assert gate_mistuned_warning("Qwen/Qwen3-Embedding-0.6B", 0.44) is not None  # qwen at bge floor


def test_new_tuning_knobs_config(fake_cfg):
    # hub_max_df / extract_every_n / rerank_top_k / rerank_min_score are now
    # config keys (promoted from hardcoded). Defaults + round-trip + validation.
    d = ms.get_memory_settings()
    assert d.hub_max_df == 0.5 and d.extract_every_n == 4
    assert d.rerank_top_k == 20 and d.rerank_min_score == 0.001
    ms.update_memory_settings({"hub_max_df": 0.6, "extract_every_n": 3,
                               "rerank_top_k": 10, "rerank_min_score": 0.01})
    s = ms.get_memory_settings()
    assert (s.hub_max_df, s.extract_every_n, s.rerank_top_k, s.rerank_min_score) == (0.6, 3, 10, 0.01)
    for bad, match in [({"hub_max_df": 0}, "hub_max_df"), ({"hub_max_df": 1.5}, "hub_max_df"),
                       ({"extract_every_n": 0}, "extract_every_n"), ({"rerank_top_k": 0}, "rerank_top_k"),
                       ({"rerank_min_score": -0.1}, "rerank_min_score")]:
        with pytest.raises(ValueError, match=match):
            ms.update_memory_settings(bad)
