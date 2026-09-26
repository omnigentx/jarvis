"""TDD contract for per-agent model selection snapshots."""
from services.model_selection import (
    ModelAuthorizationPolicy, ModelSelectionService, InMemoryModelConfigStore,
    UnauthorizedModelChange,
)


def test_authorizes_self_or_lower_and_rejects_peer_or_higher():
    policy = ModelAuthorizationPolicy({"root": 0, "worker": 1, "intern": 2})
    assert policy.can_change("worker", "worker")
    assert policy.can_change("worker", "intern")
    assert not policy.can_change("worker", "root")
    assert not policy.can_change("worker", "other")


def test_update_applies_to_next_snapshot_only():
    service = ModelSelectionService(
        InMemoryModelConfigStore("provider-default"),
        ModelAuthorizationPolicy({"worker": 1}),
    )
    first = service.snapshot("worker")
    revision = service.set_model("worker", "worker", "gpt-4o")
    second = service.snapshot("worker")
    assert first.model_id == "provider-default"
    assert second.model_id == "gpt-4o"
    assert second.config_revision == revision
    assert first.config_revision < second.config_revision


def test_unauthorized_update_does_not_mutate_store():
    service = ModelSelectionService(InMemoryModelConfigStore("default"),
                                    ModelAuthorizationPolicy({"root": 0, "worker": 1}))
    try:
        service.set_model("worker", "root", "blocked")
    except UnauthorizedModelChange:
        pass
    else:
        raise AssertionError("expected authorization failure")
    assert service.snapshot("worker").model_id == "default"
