import os
import pytest
from services import agent_definitions


def test_update_model_expected_revision_is_atomic_and_audited(tmp_path, monkeypatch):
    monkeypatch.setenv("SPAWN_REGISTRY_DB", str(tmp_path / "agents.db"))
    agent_definitions.create_definition(name="a", instruction="x", model="old")
    rev = agent_definitions.get_rev()
    updated = agent_definitions.update_definition("a", model="new", expected_revision=rev, actor="admin")
    assert updated["model"] == "new"
    with pytest.raises(agent_definitions.RevisionConflict):
        agent_definitions.update_definition("a", model="bad", expected_revision=rev, actor="admin")
    assert agent_definitions.get_definition("a")["model"] == "new"
    assert agent_definitions.list_audit_events()[-1]["actor"] == "admin"
