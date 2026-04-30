"""Tests for routes.yaml_config — YAML file viewer/editor."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import auth as core_auth
from routes import yaml_config as yaml_routes


@pytest.fixture(autouse=True)
def _set_master_key(monkeypatch):
    key = "yaml-tests-master-key-xxxxx"
    monkeypatch.setenv("JARVIS_API_KEY", key)
    monkeypatch.setattr(core_auth, "JARVIS_API_KEY", key)
    return key


@pytest.fixture()
def fake_backend_dir(tmp_path, monkeypatch):
    # Redirect the module's BASE_DIR to a tmp path so tests don't clobber real
    # fast-agent files and so we get a clean slate per test.
    monkeypatch.setattr(yaml_routes, "_BASE_DIR", tmp_path)
    (tmp_path / "fastagent.config.yaml").write_text(
        "default_model: claude-sonnet-4-20250514\nlogger:\n  progress_display: none\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture()
def client(fake_backend_dir):
    app = FastAPI()
    app.include_router(yaml_routes.router)
    return TestClient(app)


AUTH = {"Authorization": "Bearer yaml-tests-master-key-xxxxx"}


class TestList:
    def test_list_returns_known_files(self, client, fake_backend_dir):
        resp = client.get("/api/yaml/files", headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        names = {f["name"]: f for f in body["files"]}
        assert "config" in names and "secrets" in names
        assert names["config"]["exists"] is True
        # secrets doesn't exist in the fixture
        assert names["secrets"]["exists"] is False

    def test_list_requires_auth(self, client):
        resp = client.get("/api/yaml/files")
        assert resp.status_code == 401


class TestRead:
    def test_read_existing_file(self, client):
        resp = client.get("/api/yaml/config", headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["exists"] is True
        assert "default_model" in body["content"]
        assert body["size"] > 0

    def test_read_missing_file_returns_empty(self, client):
        resp = client.get("/api/yaml/secrets", headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["exists"] is False
        assert body["content"] == ""

    def test_read_unknown_name_is_404(self, client):
        resp = client.get("/api/yaml/etc", headers=AUTH)
        assert resp.status_code == 404


class TestWrite:
    def test_write_valid_yaml_succeeds(self, client, fake_backend_dir):
        body = "default_model: gpt-4o\nlogger:\n  level: INFO\n"
        resp = client.put("/api/yaml/config", headers=AUTH, json={"content": body})
        assert resp.status_code == 200
        on_disk = (fake_backend_dir / "fastagent.config.yaml").read_text(encoding="utf-8")
        assert on_disk == body

    def test_write_creates_backup(self, client, fake_backend_dir):
        body = "new_thing: yes\n"
        resp = client.put("/api/yaml/config", headers=AUTH, json={"content": body})
        assert resp.status_code == 200
        backup = fake_backend_dir / "fastagent.config.yaml.bak"
        assert backup.exists()
        # The backup must hold the *previous* content, not the new one.
        assert "default_model: claude-sonnet-4" in backup.read_text(encoding="utf-8")

    def test_write_invalid_yaml_rejected(self, client, fake_backend_dir):
        broken = "foo: [unterminated"
        resp = client.put("/api/yaml/config", headers=AUTH, json={"content": broken})
        assert resp.status_code == 400
        detail = resp.json()["detail"]
        assert "YAML" in detail["message"]
        # Disk content must be untouched.
        on_disk = (fake_backend_dir / "fastagent.config.yaml").read_text(encoding="utf-8")
        assert "default_model: claude-sonnet-4" in on_disk

    def test_write_to_unknown_name_404(self, client):
        resp = client.put("/api/yaml/etc", headers=AUTH, json={"content": "a: 1"})
        assert resp.status_code == 404

    def test_write_creates_missing_file(self, client, fake_backend_dir):
        resp = client.put("/api/yaml/secrets", headers=AUTH, json={"content": "k: v\n"})
        assert resp.status_code == 200
        assert (fake_backend_dir / "fastagent.secrets.yaml").exists()

    def test_empty_body_is_valid(self, client, fake_backend_dir):
        """Empty file is legal YAML (interpreted as None)."""
        resp = client.put("/api/yaml/secrets", headers=AUTH, json={"content": ""})
        assert resp.status_code == 200

    def test_oversize_rejected_by_pydantic(self, client):
        huge = "a" * (256 * 1024 + 1)
        resp = client.put("/api/yaml/config", headers=AUTH, json={"content": huge})
        assert resp.status_code == 422  # Pydantic validation
