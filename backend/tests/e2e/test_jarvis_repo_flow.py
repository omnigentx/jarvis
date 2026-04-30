"""E2E lifecycle for ``service.jarvis_repo / JARVIS_REPO_URL``.

Covers the full chain that previously had three sources of truth wired
together by hope (env / DB / git remote):

1. Wizard writes to DB → ``get_repo_url()`` resolves immediately.
2. Backend "restart" (clear in-process env, re-run the boot bootstrap) →
   ``get_repo_url()`` still resolves because DB is the canonical source.
3. Wizard delete → ``get_repo_url()`` raises (fail loud, not git-remote).
4. Hard cut: an ambient ``JARVIS_REPO_URL`` env var must never paper
   over a missing DB row.
"""
from __future__ import annotations

import os

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import auth as core_auth
from core import secrets_crypto
from core.database import SETUP_WIZARD_STEPS, Base, SetupWizardStep


@pytest.fixture(autouse=True)
def _clean_module_state(monkeypatch):
    monkeypatch.setattr(core_auth, "JARVIS_API_KEY", "")
    monkeypatch.delenv("JARVIS_API_KEY", raising=False)
    monkeypatch.delenv("JARVIS_REPO_URL", raising=False)
    secrets_crypto._fernet = None
    secrets_crypto._fingerprint = None
    yield
    secrets_crypto._fernet = None
    secrets_crypto._fingerprint = None


@pytest.fixture()
def db_factory(tmp_path, monkeypatch):
    db_file = tmp_path / "jarvis_repo_flow.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    with SessionFactory() as db:
        for name in SETUP_WIZARD_STEPS:
            db.add(SetupWizardStep(step_name=name))
        db.commit()

    import core.database as core_db
    from services import config_service as config_module
    from services import repo_config

    monkeypatch.setattr(core_db, "SessionLocal", SessionFactory)
    monkeypatch.setattr(config_module, "SessionLocal", SessionFactory)
    svc = config_module.ConfigService(db_factory=SessionFactory)
    monkeypatch.setattr(config_module, "config_service", svc)
    monkeypatch.setattr(repo_config, "config_service", svc)

    import routes.setup as setup_routes

    monkeypatch.setattr(setup_routes, "config_service", svc)

    yield SessionFactory
    engine.dispose()


def _build_app() -> FastAPI:
    from routes.setup import router as setup_router

    app = FastAPI()
    app.include_router(setup_router)
    return app


@pytest_asyncio.fixture()
async def client(db_factory):
    app = _build_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {core_auth.JARVIS_API_KEY}"}


async def _complete_auth(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/setup/auth",
        json={"api_key": "a-test-key-at-least-sixteen-chars"},
    )
    assert resp.status_code == 200


async def _post_jarvis_repo(client: httpx.AsyncClient, url: str) -> httpx.Response:
    return await client.post(
        "/api/setup/services",
        json={"services": {"jarvis_repo": {"JARVIS_REPO_URL": url}}},
        headers=_auth_headers(),
    )


# ---- Tests ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wizard_to_resolution_immediate(client):
    from services import repo_config

    await _complete_auth(client)
    resp = await _post_jarvis_repo(client, "https://github.com/owner/jarvis.git")
    assert resp.status_code == 200, resp.text
    assert repo_config.get_repo_url() == "https://github.com/owner/jarvis.git"


@pytest.mark.asyncio
async def test_resolution_survives_simulated_restart(client, monkeypatch):
    """Boot bootstrap rehydrates env, but get_repo_url no longer reads it.

    Simulate: wizard writes → ``os.environ`` is wiped (process restart) →
    ``get_repo_url()`` still resolves via DB. Even better, asserts that no
    one accidentally re-introduced an env-leak path.
    """
    from services import config_service as config_module
    from services import repo_config

    await _complete_auth(client)
    resp = await _post_jarvis_repo(client, "https://github.com/owner/jarvis.git")
    assert resp.status_code == 200

    # The runtime listener may have side-effected os.environ; clear it to
    # mimic a fresh process where the env starts empty.
    monkeypatch.delenv("JARVIS_REPO_URL", raising=False)
    assert "JARVIS_REPO_URL" not in os.environ

    # Even with no env, no fallback chain — DB alone is enough.
    assert repo_config.get_repo_url() == "https://github.com/owner/jarvis.git"

    # And paranoia: poison the env with a wrong value; DB still wins.
    monkeypatch.setenv("JARVIS_REPO_URL", "https://github.com/sneaky/wrong.git")
    assert repo_config.get_repo_url() == "https://github.com/owner/jarvis.git"

    # Sanity: the singleton actually read from DB and not memoization.
    # ``ConfigService.get`` is DB-only by contract — the env value set
    # above must not leak in here either.
    row = config_module.config_service.get(
        "service.jarvis_repo", "JARVIS_REPO_URL"
    )
    assert row == "https://github.com/owner/jarvis.git"


@pytest.mark.asyncio
async def test_delete_makes_get_repo_url_fail_loud(client):
    from services import config_service as config_module
    from services import repo_config

    await _complete_auth(client)
    resp = await _post_jarvis_repo(client, "https://github.com/owner/jarvis.git")
    assert resp.status_code == 200
    assert repo_config.get_repo_url() == "https://github.com/owner/jarvis.git"

    # Simulate the user clearing the value in Settings → Services. There's
    # no dedicated wizard "delete" call yet (B2 work) — go through the
    # service API directly, which is what the future Settings UI will use.
    config_module.config_service.delete("service.jarvis_repo", "JARVIS_REPO_URL")

    with pytest.raises(RuntimeError, match="not configured"):
        repo_config.get_repo_url()


@pytest.mark.asyncio
async def test_env_var_alone_is_not_enough(client, monkeypatch):
    """Hard-cut behaviour: ``JARVIS_REPO_URL=...`` deploys must run wizard."""
    from services import repo_config

    await _complete_auth(client)
    monkeypatch.setenv("JARVIS_REPO_URL", "https://github.com/legacy/from-env.git")

    with pytest.raises(RuntimeError, match="not configured"):
        repo_config.get_repo_url()


@pytest.mark.asyncio
async def test_overwrite_via_wizard_replays_cleanly(client):
    """Re-running the wizard with a new URL replaces the prior value."""
    from services import repo_config

    await _complete_auth(client)

    resp1 = await _post_jarvis_repo(client, "https://github.com/owner/v1.git")
    assert resp1.status_code == 200
    assert repo_config.get_repo_url() == "https://github.com/owner/v1.git"

    resp2 = await _post_jarvis_repo(client, "https://github.com/owner/v2.git")
    assert resp2.status_code == 200
    assert repo_config.get_repo_url() == "https://github.com/owner/v2.git"
