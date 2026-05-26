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

# Redirect the DB to an isolated jarvis.test.db BEFORE any test module
# imports core.database. Without this, MCP test cleanup fixtures using the
# global engine would delete from the developer's runtime data/jarvis.db
# and the dashboard's MCP list would go empty (tests still pass because
# they self-seed). Path mirrors the runtime DB so it sits next to it in
# data/ for easy inspection / .gitignore (data/ is already ignored).
_TEST_DB_PATH = BACKEND_DIR / "data" / "jarvis.test.db"
_TEST_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("JARVIS_DB_PATH", str(_TEST_DB_PATH))

# Session-level default so any test that imports secrets_crypto without
# explicitly setting a master key (the typical case for routes/integration
# tests) gets a deterministic Fernet bring-up. Tests that need to exercise
# rotation override this via monkeypatch.
os.environ.setdefault("JARVIS_MASTER_KEY", "pytest-session-master-key-xxxxx")
# Session-level default for the cookie-mint path. Wizard step 1 and the
# auth/refresh routes raise 503 when ``JWT_SECRET`` is unset (the
# fail-loud landed in PR #49 review fix M1) — without this, e2e tests
# that drive the wizard through ``_complete_auth`` helpers see 503 on
# CI runners that don't carry the env. Individual tests that exercise
# the "missing secret" path (e.g. ``test_missing_jwt_secret_raises_503``)
# explicitly ``monkeypatch.delenv`` to override.
os.environ.setdefault("JWT_SECRET", "pytest-session-jwt-secret-xxxxxxxxxxxxxxxx")
# Start each pytest session from a clean test DB so leftover rows from a
# previous run can never leak into assertions.
for _suffix in ("", "-wal", "-shm"):
    _path = _TEST_DB_PATH.with_name(_TEST_DB_PATH.name + _suffix)
    try:
        if _path.exists():
            _path.unlink()
    except OSError:
        pass


# ──────────────────────────────────────────────
# Database fixtures
# ──────────────────────────────────────────────


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    """Create a temporary SQLite database for testing.

    Uses monkeypatch so the session-level JARVIS_DB_PATH (set at the top of
    this conftest) is restored on teardown rather than removed entirely —
    otherwise tests running after this fixture see no override and fall
    through to the runtime data/jarvis.db.
    """
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("JARVIS_DB_PATH", str(db_path))
    yield db_path


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
