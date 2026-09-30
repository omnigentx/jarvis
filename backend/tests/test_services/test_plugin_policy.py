"""Policy cannot unblock unsupported components or store plaintext secrets."""

import pytest
from sqlalchemy import create_engine, text

from services.plugins.package import PackageError, PluginPackage
from services.plugins.policy import PluginPolicyStore, validate_policy

IMAGE = "sha256:" + "a" * 64


def test_credentials_are_encrypted_at_rest_and_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_MASTER_KEY", "local-test-master-key-only")
    engine = create_engine(f"sqlite:///{tmp_path / 'policy.db'}")
    store = PluginPolicyStore(engine)
    store.put("candidate", IMAGE, {"TOKEN": "secret-value"})
    with engine.connect() as db:
        raw = db.execute(
            text("SELECT credentials FROM plugin_execution_policies")
        ).scalar_one()
    assert "secret-value" not in raw
    loaded = store.get("candidate")
    assert loaded["image"] == IMAGE
    assert loaded["credentials"] == {"TOKEN": "secret-value"}
    assert loaded["revision"]
    engine.dispose()


def test_policy_does_not_unblock_executable_skills(tmp_path):
    package = PluginPackage(
        "unsafe",
        None,
        "claude",
        "a" * 64,
        ("skill",),
        {"server": {"command": "python"}},
        ("executable_content", "mcp_requires_policy_review"),
        None,
    )
    with pytest.raises(PackageError):
        validate_policy(tmp_path, package, {"image": IMAGE, "credentials": {}})


def test_policy_validates_every_declared_server(tmp_path):
    package = PluginPackage(
        "mcp",
        None,
        "claude",
        "a" * 64,
        (),
        {"server": {"command": "python", "env": {"TOKEN": "${API_KEY}"}}},
        ("executable_content", "mcp_requires_policy_review"),
        None,
    )
    with pytest.raises(PackageError, match="explicit configuration"):
        validate_policy(tmp_path, package, {"image": IMAGE, "credentials": {}})
    assert validate_policy(
        tmp_path, package, {"image": IMAGE, "credentials": {"API_KEY": "value"}}
    )


def test_credential_rotation_changes_review_revision(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_MASTER_KEY", "local-test-master-key-only")
    engine = create_engine(f"sqlite:///{tmp_path / 'rotation.db'}")
    store = PluginPolicyStore(engine)
    store.put("candidate", IMAGE, {"TOKEN": "first"})
    first = store.get("candidate")
    store.put("candidate", IMAGE, {"TOKEN": "second"})
    second = store.get("candidate")
    assert first["revision"] != second["revision"]
    assert first["credentials"] != second["credentials"]
    engine.dispose()
