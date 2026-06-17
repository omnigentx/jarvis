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
    assert s.embedding_model == "BAAI/bge-m3"
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
    assert d.recall_min_similarity == 0.44 and d.graph_max_hops == 1
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
