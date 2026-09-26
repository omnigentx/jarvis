from concurrent.futures import ThreadPoolExecutor
import pytest
from services.model_selection import ModelAuthorizationPolicy, ModelSelectionService, InMemoryModelConfigStore, UnauthorizedModelChange, UnknownModel, RevisionConflict

def service():
    return ModelSelectionService(InMemoryModelConfigStore("provider", "app"), ModelAuthorizationPolicy({"root": 0, "worker": 1, "intern": 2}), {"gpt-4o", "gpt-4o-mini"})

def test_precedence_and_snapshot_options():
    s = service(); a = s.snapshot("worker", {"stream": True, "retries": 2})
    assert (a.model_id, a.provider_options["stream"]) == ("app", True)
    s.set_model("root", "worker", "gpt-4o"); b = s.snapshot("worker")
    assert b.model_id == "gpt-4o" and a.model_id == "app" and a.provider_options == {"stream": True, "retries": 2}

def test_identity_and_unknown_validation_before_mutation():
    s = service()
    with pytest.raises(UnauthorizedModelChange): s.set_model("worker", "root", "gpt-4o")
    with pytest.raises(UnknownModel): s.set_model("worker", "worker", "bad")
    assert s.snapshot("worker").model_id == "app"

def test_concurrent_updates_do_not_mutate_captured_snapshot():
    s = service(); captured = s.snapshot("worker")
    with ThreadPoolExecutor(max_workers=2) as pool:
        pool.submit(s.set_model, "root", "worker", "gpt-4o").result()
        later = pool.submit(s.snapshot, "worker").result()
    assert captured.model_id == "app" and later.model_id == "gpt-4o" and captured.config_revision < later.config_revision

def test_sqlite_store_forwards_actor_and_expected_revision(tmp_path, monkeypatch):
    from services import agent_definitions
    from services.model_selection import AgentDefinitionConfigStore
    monkeypatch.setenv("SPAWN_REGISTRY_DB", str(tmp_path / "agents.db"))
    agent_definitions.create_definition(name="worker", instruction="x", model="old")
    rev = agent_definitions.get_rev()
    store = AgentDefinitionConfigStore("provider", "app")
    new_rev = store.set("worker", "gpt-4o", expected_revision=rev, actor="root")
    assert new_rev > rev
    assert agent_definitions.list_audit_events()[-1]["actor"] == "root"


def test_service_preserves_requester_in_audit_and_rejects_stale_revision(tmp_path, monkeypatch):
    from services import agent_definitions
    from services.model_selection import AgentDefinitionConfigStore, RevisionConflict
    monkeypatch.setenv("SPAWN_REGISTRY_DB", str(tmp_path / "agents.db"))
    agent_definitions.create_definition(name="worker", instruction="x", model="old")
    service = ModelSelectionService(
        AgentDefinitionConfigStore("provider"),
        ModelAuthorizationPolicy({"root": 0, "worker": 1}),
        {"new", "bad"},
    )
    revision = service.snapshot("worker").config_revision
    service.set_model("root", "worker", "new", expected_revision=revision)
    assert agent_definitions.list_audit_events()[-1]["actor"] == "root"
    with pytest.raises(RevisionConflict):
        service.set_model("root", "worker", "bad", expected_revision=revision)
    assert service.snapshot("worker").model_id == "new"


def test_in_memory_revision_guard_is_atomic():
    store = InMemoryModelConfigStore("provider")
    revision = store.snapshot("worker")[1]
    assert store.set("worker", "new", expected_revision=revision) == revision + 1
    with pytest.raises(RevisionConflict):
        store.set("worker", "bad", expected_revision=revision)
    assert store.snapshot("worker")[0] == "new"

def test_model_management_capability_required_even_for_self():
    policy = ModelAuthorizationPolicy({"worker": 1, "intern": 2}, capabilities={"worker": set(), "intern": {"model-management"}})
    s = ModelSelectionService(InMemoryModelConfigStore("provider"), policy, {"gpt-4o"})
    with pytest.raises(UnauthorizedModelChange): s.set_model("worker", "worker", "gpt-4o")
    assert s.set_model("intern", "intern", "gpt-4o") == 1
