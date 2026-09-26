from concurrent.futures import ThreadPoolExecutor
import pytest
from services.model_selection import ModelAuthorizationPolicy, ModelSelectionService, InMemoryModelConfigStore, UnauthorizedModelChange, UnknownModel

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
