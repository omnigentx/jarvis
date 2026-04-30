"""E2E cross-step tests for the Setup Wizard (``routes/setup.py``).

Unit tests in ``tests/test_routes/test_setup.py`` cover each endpoint in
isolation.  This file exercises the *sequential* wizard flow end-to-end:

1. Happy path — run all 5 steps in order, assert ``current_step`` advances
   correctly and DB rows (``setup_wizard`` + ``system_config``) match.
2. Verify without LLM — skipping step 2 then calling ``/verify`` fails with
   a ``missing`` list.
3. Reset after complete — full wizard then reset flips state back and the
   setup-gate middleware blocks a non-bootstrap API endpoint with 503.
4. Resume — ``GET /api/setup/status`` reports the correct ``current_step``
   after every individual step (isolated from Test 1).
5. Invalid-then-valid LLM — 400 on bad provider leaves the step incomplete;
   a retry with a valid provider completes it.

Tests use pytest-asyncio with ``httpx.AsyncClient`` + ``ASGITransport`` to
match the project's async E2E convention (see ``tests/e2e/``).  Fixture wiring
mirrors ``tests/test_routes/test_setup.py`` so DB isolation and crypto reset
behave identically.
"""
from __future__ import annotations

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import auth as core_auth
from core import secrets_crypto
from core.database import (
    SETUP_WIZARD_STEPS,
    Base,
    SetupWizardStep,
    SystemConfig,
)
from middleware.setup_gate import (
    SetupGateMiddleware,
    _reset_cache_for_tests,
)


# ---- Fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_module_state(monkeypatch):
    """Start every test from a blank slate.

    - No in-memory master key (so ``/api/setup/auth`` is open).
    - Fresh crypto state (Fernet is rebuilt on demand from the key).
    - Setup-gate cache reset so middleware re-reads from the temp DB.
    """
    monkeypatch.setattr(core_auth, "JARVIS_API_KEY", "")
    monkeypatch.delenv("JARVIS_API_KEY", raising=False)
    secrets_crypto._fernet = None
    secrets_crypto._fingerprint = None
    _reset_cache_for_tests()
    yield
    secrets_crypto._fernet = None
    secrets_crypto._fingerprint = None
    _reset_cache_for_tests()


@pytest.fixture()
def db_factory(tmp_path, monkeypatch):
    """Fresh SQLite DB wired into every module that caches ``SessionLocal``.

    Same shape as the fixture in ``tests/test_routes/test_setup.py`` — kept
    local rather than hoisted so this file stays self-contained for now.
    """
    db_file = tmp_path / "setup_wizard_flow.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Seed one row per wizard step so `/status` reports the full skeleton
    # from the first request.
    with SessionFactory() as db:
        for name in SETUP_WIZARD_STEPS:
            db.add(SetupWizardStep(step_name=name))
        db.commit()

    import core.database as core_db
    from services import config_service as config_module

    monkeypatch.setattr(core_db, "SessionLocal", SessionFactory)
    monkeypatch.setattr(config_module, "SessionLocal", SessionFactory)
    monkeypatch.setattr(
        config_module,
        "config_service",
        config_module.ConfigService(db_factory=SessionFactory),
    )
    import routes.setup as setup_routes

    monkeypatch.setattr(setup_routes, "config_service", config_module.config_service)

    yield SessionFactory
    engine.dispose()


def _build_app(*, with_gate: bool = False) -> FastAPI:
    """Compose an app with just the setup router (+ optional gate + dummy)."""
    from routes.setup import router as setup_router

    app = FastAPI()
    if with_gate:
        app.add_middleware(SetupGateMiddleware)
    app.include_router(setup_router)

    if with_gate:
        # Stand-in for a gated API route (e.g. /api/chat) so we can prove the
        # 503 behaviour without pulling in the whole application graph.
        @app.get("/api/dummy")
        def dummy():
            return {"ok": True}

    return app


@pytest_asyncio.fixture()
async def client(db_factory):
    app = _build_app(with_gate=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {core_auth.JARVIS_API_KEY}"}


# ---- Helpers ---------------------------------------------------------------


async def _complete_auth(client: httpx.AsyncClient) -> None:
    resp = await client.post(
        "/api/setup/auth",
        json={"api_key": "a-test-key-at-least-sixteen-chars"},
    )
    assert resp.status_code == 200


async def _complete_llm(
    client: httpx.AsyncClient,
    *,
    provider: str = "anthropic",
    model: str = "claude-sonnet-4",
    api_key: str = "sk-ant-fake",
) -> None:
    resp = await client.post(
        "/api/setup/llm",
        json={"provider": provider, "model": model, "api_key": api_key},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200, resp.text


# ---- Tests -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_wizard_happy_path_full_sequence(client, db_factory):
    """All five steps in order → overall_complete=True; DB reflects writes."""
    # Step 1: auth
    resp = await client.post(
        "/api/setup/auth",
        json={"api_key": "a-test-key-at-least-sixteen-chars"},
    )
    assert resp.status_code == 200
    status = (await client.get("/api/setup/status", headers=_auth_headers())).json()
    assert status["current_step"] == "llm"

    # Step 2: llm
    resp = await client.post(
        "/api/setup/llm",
        json={
            "provider": "anthropic",
            "model": "claude-sonnet-4",
            "api_key": "sk-ant-fake",
        },
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    status = (await client.get("/api/setup/status", headers=_auth_headers())).json()
    assert status["current_step"] == "services"

    # Step 3: services (empty payload — mark done with no services)
    resp = await client.post(
        "/api/setup/services",
        json={"services": {}},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    status = (await client.get("/api/setup/status", headers=_auth_headers())).json()
    assert status["current_step"] == "yaml_config"

    # Step 4: yaml_config
    resp = await client.post(
        "/api/setup/yaml_config", json={}, headers=_auth_headers()
    )
    assert resp.status_code == 200
    status = (await client.get("/api/setup/status", headers=_auth_headers())).json()
    assert status["current_step"] == "verify"

    # Step 5: verify
    resp = await client.post(
        "/api/setup/verify",
        json={"accept_warnings": False},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["overall_complete"] is True
    assert body["current_step"] is None

    # DB assertions
    with db_factory() as db:
        rows = db.query(SetupWizardStep).all()
        assert len(rows) == 5
        assert all(r.completed for r in rows), {
            r.step_name: (r.completed, r.skipped) for r in rows
        }

        cfg_keys = {
            (r.category, r.key) for r in db.query(SystemConfig).all()
        }
    assert ("auth", "JARVIS_API_KEY") in cfg_keys
    assert ("llm", "anthropic_api_key") in cfg_keys
    assert ("llm", "model") in cfg_keys
    assert ("llm", "provider") in cfg_keys


@pytest.mark.asyncio
async def test_wizard_verify_fails_without_llm(client):
    """Skipping LLM then calling /verify → 400 with ``missing`` list."""
    await _complete_auth(client)

    resp = await client.post(
        "/api/setup/verify",
        json={"accept_warnings": False},
        headers=_auth_headers(),
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    # Detail is a dict: {"message": ..., "missing": [...]} (see routes/setup.py).
    assert isinstance(detail, dict)
    missing = detail.get("missing") or []
    assert any(
        "api_key" in m or "model" in m for m in missing
    ), f"expected llm-related entry in {missing!r}"


@pytest.mark.asyncio
async def test_wizard_reset_clears_all_steps(db_factory):
    """Happy path → reset → state cleared + gated endpoint returns 503.

    Runs its own gated app so we can prove the middleware reacts to reset.
    """
    app = _build_app(with_gate=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        # Drive the full wizard to completion.
        await _complete_auth(c)
        await _complete_llm(c)
        await c.post(
            "/api/setup/services", json={"services": {}}, headers=_auth_headers()
        )
        await c.post(
            "/api/setup/yaml_config", json={}, headers=_auth_headers()
        )
        verify = await c.post(
            "/api/setup/verify",
            json={"accept_warnings": False},
            headers=_auth_headers(),
        )
        assert verify.status_code == 200
        # Gate open now that setup is complete.
        assert (await c.get("/api/dummy")).status_code == 200

        # Reset.
        resp = await c.post("/api/setup/reset", headers=_auth_headers())
        assert resp.status_code == 200
        status = resp.json()
        assert status["overall_complete"] is False
        assert status["current_step"] == "auth"

        # DB rows back to blank.
        with db_factory() as db:
            rows = db.query(SetupWizardStep).all()
            assert len(rows) == 5
            for r in rows:
                assert r.completed is False
                assert r.skipped is False

        # Gate closes again — non-bootstrap endpoint blocked with 503.
        dummy = await c.get("/api/dummy")
        assert dummy.status_code == 503


@pytest.mark.asyncio
async def test_wizard_current_step_resume_after_each_step(client):
    """GET /status returns the correct ``current_step`` after every step.

    Mirrors the in-line assertions in the happy-path test but isolated so a
    regression in the "where am I?" pointer fails independently.
    """
    # Before anything: current_step == 'auth'
    # (The status endpoint is open pre-auth.)
    status = (await client.get("/api/setup/status")).json()
    assert status["current_step"] == "auth"

    await _complete_auth(client)
    status = (await client.get("/api/setup/status", headers=_auth_headers())).json()
    assert status["current_step"] == "llm"

    await _complete_llm(client)
    status = (await client.get("/api/setup/status", headers=_auth_headers())).json()
    assert status["current_step"] == "services"

    await client.post(
        "/api/setup/services", json={"services": {}}, headers=_auth_headers()
    )
    status = (await client.get("/api/setup/status", headers=_auth_headers())).json()
    assert status["current_step"] == "yaml_config"

    await client.post(
        "/api/setup/yaml_config", json={}, headers=_auth_headers()
    )
    status = (await client.get("/api/setup/status", headers=_auth_headers())).json()
    assert status["current_step"] == "verify"

    await client.post(
        "/api/setup/verify",
        json={"accept_warnings": False},
        headers=_auth_headers(),
    )
    status = (await client.get("/api/setup/status", headers=_auth_headers())).json()
    assert status["current_step"] is None
    assert status["overall_complete"] is True


@pytest.mark.asyncio
async def test_wizard_llm_invalid_provider_then_retry_success(client, db_factory):
    """POST /llm with bad provider → 400, step stays incomplete; retry OK."""
    await _complete_auth(client)

    # First attempt: bogus provider
    resp = await client.post(
        "/api/setup/llm",
        json={
            "provider": "invalid_xxx",
            "model": "foo",
            "api_key": "bar",
        },
        headers=_auth_headers(),
    )
    assert resp.status_code == 400

    with db_factory() as db:
        llm_row = (
            db.query(SetupWizardStep).filter_by(step_name="llm").one()
        )
    assert llm_row.completed is False
    assert llm_row.skipped is False

    # Retry with valid provider
    resp = await client.post(
        "/api/setup/llm",
        json={
            "provider": "anthropic",
            "model": "claude-sonnet-4",
            "api_key": "sk-ant-fake",
        },
        headers=_auth_headers(),
    )
    assert resp.status_code == 200

    with db_factory() as db:
        llm_row = (
            db.query(SetupWizardStep).filter_by(step_name="llm").one()
        )
    assert llm_row.completed is True
