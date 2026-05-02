"""Shared test fixtures for Jarvis backend tests."""

import os
import sys
import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure backend/ is on sys.path
BACKEND_DIR = Path(__file__).parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


# ──────────────────────────────────────────────
# Database fixtures
# ──────────────────────────────────────────────


@pytest.fixture()
def tmp_db(tmp_path):
    """Create a temporary SQLite database for testing."""
    db_path = tmp_path / "test.db"
    os.environ["JARVIS_DB_PATH"] = str(db_path)
    yield db_path
    os.environ.pop("JARVIS_DB_PATH", None)


# ──────────────────────────────────────────────
# FastAPI test client
# ──────────────────────────────────────────────


@pytest.fixture()
def mock_agent_app():
    """Mock FastAgent app to avoid real LLM initialization."""
    app = MagicMock()
    app._agents = {}
    app.send = AsyncMock(return_value="Test response")
    return app


@pytest.fixture()
def app_client(mock_agent_app, monkeypatch):
    """Create a test client with mocked FastAgent.

    Pin the API key to a deterministic test value (and patch the module
    global the ``verify_api_key`` dependency reads at call-time) so the
    test passes regardless of whatever real ``JARVIS_API_KEY`` the dev
    environment / DB master-key restore left in ``core_auth``.

    Usage::

        async def test_something(app_client):
            response = await app_client.get("/api/health")
            assert response.status_code == 200
    """
    import httpx
    # Import server FIRST so its bootstrap (which calls apply_master_key →
    # writes the real key into core_auth.JARVIS_API_KEY) settles. Only then
    # do we pin the test value, so it isn't clobbered out from under us.
    from server import app

    TEST_KEY = "app-client-fixture-key"
    from core import auth as core_auth
    monkeypatch.setattr(core_auth, "JARVIS_API_KEY", TEST_KEY)
    monkeypatch.setenv("JARVIS_API_KEY", TEST_KEY)

    # Patch shared_state
    import services.shared_state as state
    original_app = state.agent_app
    state.agent_app = mock_agent_app

    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {TEST_KEY}"},
    )

    yield client

    state.agent_app = original_app


# ──────────────────────────────────────────────
# Spawn registry fixtures
# ──────────────────────────────────────────────


@pytest.fixture()
def sample_spawn_registry(tmp_path):
    """Create a sample spawn_registry.json for testing."""
    registry = {
        "run-001": {
            "agent_name": "Linh - PM",
            "role": "Linh - PM",
            "status": "idle",
            "lifecycle": "resumable",
            "task": "Build TODO API",
            "session_id": "session-abc",
        },
        "run-002": {
            "agent_name": "Hoa - BA",
            "role": "Hoa - BA",
            "status": "running",
            "lifecycle": "resumable",
            "task": "Write requirements",
            "session_id": "session-abc",
        },
    }
    registry_file = tmp_path / "spawn_registry.json"
    registry_file.write_text(json.dumps(registry, indent=2))
    return registry_file


# ──────────────────────────────────────────────
# Agent cards fixtures
# ──────────────────────────────────────────────


@pytest.fixture()
def sample_agent_cards_dir(tmp_path):
    """Create sample agent card YAML files for testing."""
    cards_dir = tmp_path / "agent_cards"
    cards_dir.mkdir()

    card1 = {
        "name": "TestAgent",
        "instruction": "You are a test agent.",
        "model": "gpt-4o-mini",
        "servers": ["filesystem"],
    }
    (cards_dir / "test_agent.yaml").write_text(
        "name: TestAgent\ninstruction: You are a test agent.\nmodel: gpt-4o-mini\nservers:\n  - filesystem\n"
    )

    return cards_dir


# ──────────────────────────────────────────────
# Progress event fixtures
# ──────────────────────────────────────────────


@pytest.fixture()
def progress_manager():
    """Fresh ProgressEventManager for testing."""
    from services.sse_progress import ProgressEventManager
    return ProgressEventManager()
