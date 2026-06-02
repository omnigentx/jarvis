"""Integration tests for the WebAuthn / passkey routes in
``routes/auth.py``.

What's covered here vs elsewhere
--------------------------------
* Unit tests for the ``core.webauthn`` wrapper live in
  ``test_core/test_webauthn.py`` (RP-ID parsing, ceremony store,
  options shape).
* Real cryptographic round-trip — the authenticator actually signing
  an attestation/assertion — is covered by the Playwright virtual
  authenticator E2E in ``frontend/tests/e2e/flows/``.
* THIS file covers the HTTP-layer plumbing: route auth gates, DB
  persistence, RP-ID scoping, error-mapping, idempotency. The
  cryptographic verify call is mocked because we exercise it
  thoroughly in the two layers above; mocking here keeps these tests
  fast and independent of a real authenticator.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Optional

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core import auth as core_auth
from core import webauthn as wa
from core.database import (
    Base,
    DEFAULT_USERNAME,
    DEFAULT_USER_ID,
    PasskeyCredential,
    User,
)


# ---- Shared fixtures --------------------------------------------------------


@pytest.fixture(autouse=True)
def _stable_secrets(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "test-jwt-secret-xxxxxxxxxxxxxxxxxxxx")
    monkeypatch.setattr(core_auth, "JARVIS_API_KEY", "test-api-key-xxxxxxxxxxxxxx")
    core_auth._login_attempts.clear()
    wa._clear_ceremonies()
    yield
    wa._clear_ceremonies()


@pytest.fixture()
def db_factory(tmp_path, monkeypatch):
    """Fresh SQLite DB with the seeded default user row, swapped in for
    the route's ``Depends(get_db)`` lookup."""
    db_file = tmp_path / "passkey_test.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    SessionFactory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    with SessionFactory() as db:
        db.add(User(id=DEFAULT_USER_ID, username=DEFAULT_USERNAME))
        db.commit()

    import core.database as core_db
    monkeypatch.setattr(core_db, "SessionLocal", SessionFactory)
    yield SessionFactory
    engine.dispose()


@pytest.fixture()
def client(db_factory) -> TestClient:
    from routes.auth import router as auth_router

    app = FastAPI()
    app.include_router(auth_router)
    c = TestClient(app)
    # Default Bearer header so most tests don't have to repeat themselves.
    c.headers.update({"Authorization": f"Bearer {core_auth.JARVIS_API_KEY}"})
    return c


def _no_auth_client(db_factory) -> TestClient:
    from routes.auth import router as auth_router

    app = FastAPI()
    app.include_router(auth_router)
    return TestClient(app)


def _fake_verified_registration(
    *,
    credential_id: bytes = b"fake-credential-id-bytes-32",
    public_key: bytes = b"fake-cose-public-key",
    sign_count: int = 0,
):
    """Build a VerifiedRegistration-look-alike that satisfies the
    attributes our route reads. Keeps the dependency on the library's
    actual struct shape minimal — we only care about the three fields
    the route persists."""
    return SimpleNamespace(
        credential_id=credential_id,
        credential_public_key=public_key,
        sign_count=sign_count,
    )


# ---- /passkey/register/begin ------------------------------------------------


class TestRegisterBegin:
    def test_requires_auth(self, db_factory):
        """No Bearer → 401 from verify_api_key dep."""
        c = _no_auth_client(db_factory)
        resp = c.post("/api/auth/passkey/register/begin")
        assert resp.status_code == 401

    def test_returns_ceremony_id_and_options(self, client):
        resp = client.post(
            "/api/auth/passkey/register/begin",
            headers={"Host": "localhost:3001"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "ceremony_id" in body and len(body["ceremony_id"]) > 16
        opts = body["options"]
        assert opts["rp"]["id"] == "localhost"
        # WebAuthn options are JSON-shaped and contain a challenge ready
        # for the browser to consume.
        assert isinstance(opts["challenge"], str) and len(opts["challenge"]) > 0
        assert opts["user"]["name"] == DEFAULT_USERNAME

    def test_excludes_credentials_already_registered_on_this_rp(
        self, client, db_factory,
    ):
        """Re-registering on a passkey-bound authenticator should be
        blocked by the browser. We surface this via excludeCredentials."""
        with db_factory() as db:
            db.add(PasskeyCredential(
                id=wa.b64url_encode(b"existing-cred-bytes"),
                user_id=DEFAULT_USER_ID,
                public_key=b"\x00",
                sign_count=0,
                rp_id="localhost",
            ))
            db.commit()

        resp = client.post(
            "/api/auth/passkey/register/begin",
            headers={"Host": "localhost:3001"},
        )
        assert resp.status_code == 200
        excl = resp.json()["options"].get("excludeCredentials", [])
        assert len(excl) == 1

    def test_does_not_exclude_credentials_from_other_rp(
        self, client, db_factory,
    ):
        """A passkey registered on a different RP is irrelevant here."""
        with db_factory() as db:
            db.add(PasskeyCredential(
                id=wa.b64url_encode(b"other-rp-cred"),
                user_id=DEFAULT_USER_ID,
                public_key=b"\x00",
                sign_count=0,
                rp_id="jarvis.alice.com",
            ))
            db.commit()
        resp = client.post(
            "/api/auth/passkey/register/begin",
            headers={"Host": "localhost:3001"},
        )
        assert resp.status_code == 200
        assert resp.json()["options"].get("excludeCredentials", []) == []


# ---- /passkey/register/finish -----------------------------------------------


class TestRegisterFinish:
    def test_requires_auth(self, db_factory):
        c = _no_auth_client(db_factory)
        resp = c.post(
            "/api/auth/passkey/register/finish",
            json={"ceremony_id": "x", "credential": {}, "label": None},
        )
        assert resp.status_code == 401

    def test_unknown_ceremony_returns_400_with_stable_reason(self, client):
        resp = client.post(
            "/api/auth/passkey/register/finish",
            json={
                "ceremony_id": "definitely-not-stored",
                "credential": {},
                "label": "My Mac",
            },
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["detail"]["error"] == "passkey_register_failed"
        assert body["detail"]["reason"] == "ceremony_unknown_or_expired"

    def test_happy_path_persists_credential(self, client, db_factory, monkeypatch):
        # Begin to get a real ceremony id stored in wa._ceremonies.
        begin = client.post(
            "/api/auth/passkey/register/begin",
            headers={"Host": "localhost:3001"},
        )
        ceremony_id = begin.json()["ceremony_id"]

        # Stub the library verify call — its real cryptographic check is
        # exercised in E2E. Here we just prove the route persists the
        # right fields.
        monkeypatch.setattr(
            wa, "verify_registration",
            lambda **kw: _fake_verified_registration(
                credential_id=b"happy-path-cred-bytes",
                public_key=b"happy-cose-bytes",
                sign_count=0,
            ),
        )

        resp = client.post(
            "/api/auth/passkey/register/finish",
            headers={"Host": "localhost:3001"},
            json={
                "ceremony_id": ceremony_id,
                "credential": {
                    "id": "irrelevant-here",
                    "response": {"transports": ["internal", "hybrid"]},
                },
                "label": "My MacBook",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["replaced"] is False
        expected_id = wa.b64url_encode(b"happy-path-cred-bytes")
        assert body["credential_id"] == expected_id

        # DB landing: row exists with the right fields.
        with db_factory() as db:
            cred = db.query(PasskeyCredential).filter(
                PasskeyCredential.id == expected_id,
            ).first()
            assert cred is not None
            assert cred.user_id == DEFAULT_USER_ID
            assert cred.public_key == b"happy-cose-bytes"
            assert cred.sign_count == 0
            assert cred.rp_id == "localhost"
            assert cred.label == "My MacBook"
            # transports round-trip through JSON encoding.
            import json
            assert json.loads(cred.transports) == ["internal", "hybrid"]

    def test_replay_finish_is_idempotent_via_replace(
        self, client, db_factory, monkeypatch,
    ):
        """If the same credential_id arrives twice (network retry,
        double-tap), the second call overwrites instead of throwing
        UniqueConstraintViolation."""
        begin1 = client.post(
            "/api/auth/passkey/register/begin",
            headers={"Host": "localhost:3001"},
        ).json()
        monkeypatch.setattr(
            wa, "verify_registration",
            lambda **kw: _fake_verified_registration(
                credential_id=b"same-id", public_key=b"k1", sign_count=0,
            ),
        )
        r1 = client.post(
            "/api/auth/passkey/register/finish",
            headers={"Host": "localhost:3001"},
            json={
                "ceremony_id": begin1["ceremony_id"],
                "credential": {"id": "x", "response": {}},
                "label": "First",
            },
        )
        assert r1.json()["replaced"] is False

        begin2 = client.post(
            "/api/auth/passkey/register/begin",
            headers={"Host": "localhost:3001"},
        ).json()
        monkeypatch.setattr(
            wa, "verify_registration",
            lambda **kw: _fake_verified_registration(
                credential_id=b"same-id", public_key=b"k2-updated", sign_count=5,
            ),
        )
        r2 = client.post(
            "/api/auth/passkey/register/finish",
            headers={"Host": "localhost:3001"},
            json={
                "ceremony_id": begin2["ceremony_id"],
                "credential": {"id": "x", "response": {}},
                "label": "Second",
            },
        )
        assert r2.json()["replaced"] is True

        with db_factory() as db:
            rows = db.query(PasskeyCredential).filter(
                PasskeyCredential.id == wa.b64url_encode(b"same-id"),
            ).all()
            assert len(rows) == 1
            assert rows[0].public_key == b"k2-updated"
            assert rows[0].sign_count == 5
            assert rows[0].label == "Second"

    def test_verify_failure_surfaces_400_and_does_not_persist(
        self, client, db_factory, monkeypatch,
    ):
        begin = client.post(
            "/api/auth/passkey/register/begin",
            headers={"Host": "localhost:3001"},
        ).json()

        def boom(**kw):
            from webauthn.helpers.exceptions import (
                InvalidRegistrationResponse,
            )
            raise InvalidRegistrationResponse("attestation signature invalid")

        monkeypatch.setattr(wa, "verify_registration", boom)
        resp = client.post(
            "/api/auth/passkey/register/finish",
            headers={"Host": "localhost:3001"},
            json={
                "ceremony_id": begin["ceremony_id"],
                "credential": {"id": "x", "response": {}},
                "label": None,
            },
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["error"] == "passkey_register_failed"

        with db_factory() as db:
            assert db.query(PasskeyCredential).count() == 0


# ---- /passkey/list ----------------------------------------------------------


class TestListPasskeys:
    def test_requires_auth(self, db_factory):
        c = _no_auth_client(db_factory)
        assert c.get("/api/auth/passkey/list").status_code == 401

    def test_empty_when_no_credentials(self, client):
        resp = client.get(
            "/api/auth/passkey/list",
            headers={"Host": "localhost:3001"},
        )
        assert resp.status_code == 200
        assert resp.json() == []

    def test_filters_by_request_rp_id(self, client, db_factory):
        # Two credentials: one for localhost, one for jarvis.alice.com.
        # The request hitting localhost should ONLY see the localhost one.
        with db_factory() as db:
            db.add(PasskeyCredential(
                id="cred-on-localhost", user_id=DEFAULT_USER_ID,
                public_key=b"\x00", sign_count=0, rp_id="localhost",
                label="laptop",
            ))
            db.add(PasskeyCredential(
                id="cred-on-public", user_id=DEFAULT_USER_ID,
                public_key=b"\x00", sign_count=0, rp_id="jarvis.alice.com",
                label="public",
            ))
            db.commit()

        local = client.get(
            "/api/auth/passkey/list",
            headers={"Host": "localhost:3001"},
        ).json()
        assert [c["id"] for c in local] == ["cred-on-localhost"]
        assert local[0]["label"] == "laptop"

        public = client.get(
            "/api/auth/passkey/list",
            headers={"Host": "jarvis.alice.com"},
        ).json()
        assert [c["id"] for c in public] == ["cred-on-public"]

    def test_transports_round_trip_as_list(self, client, db_factory):
        with db_factory() as db:
            import json
            db.add(PasskeyCredential(
                id="cred-tx", user_id=DEFAULT_USER_ID,
                public_key=b"\x00", sign_count=0, rp_id="localhost",
                transports=json.dumps(["internal", "hybrid"]),
            ))
            db.commit()
        resp = client.get(
            "/api/auth/passkey/list",
            headers={"Host": "localhost:3001"},
        )
        assert resp.status_code == 200
        rows = resp.json()
        assert rows[0]["transports"] == ["internal", "hybrid"]

    def test_corrupt_transports_returns_empty_list_not_500(
        self, client, db_factory,
    ):
        with db_factory() as db:
            db.add(PasskeyCredential(
                id="cred-bad", user_id=DEFAULT_USER_ID,
                public_key=b"\x00", sign_count=0, rp_id="localhost",
                transports="this-is-not-json",
            ))
            db.commit()
        resp = client.get(
            "/api/auth/passkey/list",
            headers={"Host": "localhost:3001"},
        )
        assert resp.status_code == 200
        assert resp.json()[0]["transports"] == []


# ---- DELETE /passkey/{credential_id} ----------------------------------------


class TestDeletePasskey:
    def test_requires_auth(self, db_factory):
        c = _no_auth_client(db_factory)
        assert c.delete("/api/auth/passkey/anything").status_code == 401

    def test_unknown_id_returns_404(self, client):
        resp = client.delete("/api/auth/passkey/never-existed")
        assert resp.status_code == 404
        assert resp.json()["detail"]["error"] == "passkey_not_found"

    def test_happy_path_removes_row(self, client, db_factory):
        with db_factory() as db:
            db.add(PasskeyCredential(
                id="to-delete", user_id=DEFAULT_USER_ID,
                public_key=b"\x00", sign_count=0, rp_id="localhost",
            ))
            db.commit()

        resp = client.delete("/api/auth/passkey/to-delete")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        with db_factory() as db:
            assert db.query(PasskeyCredential).count() == 0

    def test_cannot_delete_credentials_of_a_different_user(
        self, client, db_factory,
    ):
        """Belt-and-braces for the multi-user future. Today single-user
        only has 'owner', but the route still scopes deletes."""
        with db_factory() as db:
            db.add(User(id="some-other-user", username="other"))
            db.add(PasskeyCredential(
                id="other-users-cred", user_id="some-other-user",
                public_key=b"\x00", sign_count=0, rp_id="localhost",
            ))
            db.commit()
        resp = client.delete("/api/auth/passkey/other-users-cred")
        assert resp.status_code == 404
        with db_factory() as db:
            # Row still there — not collateral-damaged.
            assert db.query(PasskeyCredential).count() == 1


# ---- GET /passkey/has-any ---------------------------------------------------


class TestHasAny:
    """Public probe used by AuthGate to decide whether to show the
    'Sign in with passkey' button on first paint."""

    def test_public_no_auth_needed(self, db_factory):
        c = _no_auth_client(db_factory)
        resp = c.get(
            "/api/auth/passkey/has-any",
            headers={"Host": "localhost:3001"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"has_passkey": False}

    def test_true_when_credential_for_current_rp_exists(self, db_factory):
        with db_factory() as db:
            db.add(PasskeyCredential(
                id="c1", user_id=DEFAULT_USER_ID,
                public_key=b"\x00", sign_count=0, rp_id="localhost",
            ))
            db.commit()
        c = _no_auth_client(db_factory)
        resp = c.get(
            "/api/auth/passkey/has-any",
            headers={"Host": "localhost:3001"},
        )
        assert resp.json() == {"has_passkey": True}

    def test_false_when_only_other_rp_credentials_exist(self, db_factory):
        with db_factory() as db:
            db.add(PasskeyCredential(
                id="c2", user_id=DEFAULT_USER_ID,
                public_key=b"\x00", sign_count=0, rp_id="jarvis.alice.com",
            ))
            db.commit()
        c = _no_auth_client(db_factory)
        resp = c.get(
            "/api/auth/passkey/has-any",
            headers={"Host": "localhost:3001"},
        )
        # Per-RP scoping: the credential for jarvis.alice.com is
        # invisible to a localhost request because the browser literally
        # cannot use it here.
        assert resp.json() == {"has_passkey": False}


# ---- /passkey/authenticate/begin -------------------------------------------


class TestAuthenticateBegin:
    """Public — no auth header required."""

    def test_returns_ceremony_id_and_options_without_leaking_credential_ids(
        self, db_factory,
    ):
        """H3 fix: even when credentials are registered for this RP,
        the public /begin response MUST NOT echo them back — they leak
        to unauthenticated probes that way. Discoverable-credential UX
        means the browser already knows what to offer; the server
        looks the credential up on the assertion at /finish time."""
        with db_factory() as db:
            db.add(PasskeyCredential(
                id="cred-for-allow",
                user_id=DEFAULT_USER_ID,
                public_key=b"\x00", sign_count=0, rp_id="localhost",
            ))
            db.commit()
        c = _no_auth_client(db_factory)
        resp = c.post(
            "/api/auth/passkey/authenticate/begin",
            headers={"Host": "localhost:3001"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["ceremony_id"]) > 16
        opts = body["options"]
        assert opts["rpId"] == "localhost"
        # H3: allowCredentials must be empty/absent regardless of how
        # many credentials this user has registered.
        assert not opts.get("allowCredentials")
        # UV REQUIRED for the primary credential — see H1.
        assert opts.get("userVerification") == "required"

    def test_no_credentials_returns_empty_or_omitted_allow_credentials(
        self, db_factory,
    ):
        c = _no_auth_client(db_factory)
        resp = c.post(
            "/api/auth/passkey/authenticate/begin",
            headers={"Host": "localhost:3001"},
        )
        assert resp.status_code == 200
        # Discoverable-credential UX path: ``allowCredentials`` empty or
        # absent means "let the platform pick any resident credential".
        assert not resp.json()["options"].get("allowCredentials")


# ---- /passkey/authenticate/finish ------------------------------------------


def _fake_verified_authentication(*, new_sign_count: int = 1):
    """VerifiedAuthentication look-alike — only the field the route
    reads (``new_sign_count``) is needed."""
    return SimpleNamespace(new_sign_count=new_sign_count)


class TestAuthenticateFinish:
    def test_unknown_credential_id_returns_401(self, db_factory):
        c = _no_auth_client(db_factory)
        resp = c.post(
            "/api/auth/passkey/authenticate/finish",
            headers={"Host": "localhost:3001"},
            json={
                "ceremony_id": "doesnt-matter",
                "credential": {"id": "never-registered", "response": {}},
            },
        )
        assert resp.status_code == 401
        assert resp.json()["detail"]["reason"] == "credential_unknown"

    def test_missing_credential_id_returns_400(self, db_factory):
        c = _no_auth_client(db_factory)
        resp = c.post(
            "/api/auth/passkey/authenticate/finish",
            headers={"Host": "localhost:3001"},
            json={"ceremony_id": "x", "credential": {}},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["reason"] == "credential_id_missing"

    def test_happy_path_mints_session_cookie_and_bumps_counter(
        self, db_factory, monkeypatch,
    ):
        # Pre-seed a credential.
        with db_factory() as db:
            db.add(PasskeyCredential(
                id="happy-path-cred",
                user_id=DEFAULT_USER_ID,
                public_key=b"pubkey-bytes",
                sign_count=3,
                rp_id="localhost",
                label="My Laptop",
            ))
            db.commit()

        c = _no_auth_client(db_factory)

        # Begin → stores a real ceremony id in the in-process registry.
        begin = c.post(
            "/api/auth/passkey/authenticate/begin",
            headers={"Host": "localhost:3001"},
        ).json()

        # Stub the lib verify — real signature check is exercised in E2E.
        monkeypatch.setattr(
            wa, "verify_authentication",
            lambda **kw: _fake_verified_authentication(new_sign_count=4),
        )

        resp = c.post(
            "/api/auth/passkey/authenticate/finish",
            headers={"Host": "localhost:3001"},
            json={
                "ceremony_id": begin["ceremony_id"],
                "credential": {
                    "id": "happy-path-cred",
                    "response": {"signature": "...", "authenticatorData": "..."},
                },
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["csrf_token"]

        # Session + CSRF cookies dropped, same shape as /login.
        from core.session import CSRF_COOKIE_NAME, SESSION_COOKIE_NAME
        assert SESSION_COOKIE_NAME in c.cookies
        assert CSRF_COOKIE_NAME in c.cookies

        # sign_count + last_used_at updated.
        with db_factory() as db:
            cred = db.query(PasskeyCredential).filter(
                PasskeyCredential.id == "happy-path-cred",
            ).first()
            assert cred.sign_count == 4
            assert cred.last_used_at is not None
            assert cred.last_used_at > 0

    def test_verify_failure_does_not_bump_counter(
        self, db_factory, monkeypatch,
    ):
        """If verify fails we MUST NOT advance sign_count — that
        invariant detects cloned authenticators on the next legit
        login attempt."""
        with db_factory() as db:
            db.add(PasskeyCredential(
                id="will-fail-cred",
                user_id=DEFAULT_USER_ID,
                public_key=b"pubkey-bytes",
                sign_count=7,
                rp_id="localhost",
            ))
            db.commit()
        c = _no_auth_client(db_factory)
        c.post(
            "/api/auth/passkey/authenticate/begin",
            headers={"Host": "localhost:3001"},
        )
        monkeypatch.setattr(
            wa, "verify_authentication",
            lambda **kw: (_ for _ in ()).throw(ValueError("signature_mismatch")),
        )
        resp = c.post(
            "/api/auth/passkey/authenticate/finish",
            headers={"Host": "localhost:3001"},
            json={
                "ceremony_id": "fake-id-doesnt-matter-we-mocked-verify",
                "credential": {"id": "will-fail-cred", "response": {}},
            },
        )
        assert resp.status_code == 401
        with db_factory() as db:
            cred = db.query(PasskeyCredential).filter(
                PasskeyCredential.id == "will-fail-cred",
            ).first()
            assert cred.sign_count == 7  # untouched
            assert cred.last_used_at is None

    def test_minted_session_cookie_authenticates_subsequent_requests(
        self, db_factory, monkeypatch,
    ):
        """End-to-end: after passkey auth, the session cookie alone is
        enough to call a protected route (no Bearer needed)."""
        from fastapi import Depends, FastAPI
        from core.auth import verify_api_key
        from routes.auth import router as auth_router

        with db_factory() as db:
            db.add(PasskeyCredential(
                id="full-flow-cred",
                user_id=DEFAULT_USER_ID,
                public_key=b"k", sign_count=0, rp_id="localhost",
            ))
            db.commit()

        app = FastAPI()
        app.include_router(auth_router)

        @app.get(
            "/api/private",
            dependencies=[Depends(verify_api_key)],
        )
        async def private() -> dict:
            return {"ok": True}

        c = TestClient(app)
        c.post(
            "/api/auth/passkey/authenticate/begin",
            headers={"Host": "localhost:3001"},
        )
        monkeypatch.setattr(
            wa, "verify_authentication",
            lambda **kw: _fake_verified_authentication(new_sign_count=1),
        )
        resp = c.post(
            "/api/auth/passkey/authenticate/finish",
            headers={"Host": "localhost:3001"},
            json={
                "ceremony_id": "any",
                "credential": {"id": "full-flow-cred", "response": {}},
            },
        )
        assert resp.status_code == 200
        # No Bearer header; rely solely on the cookie set by finish.
        # X-CSRF-Token only matters for mutating methods.
        private_resp = c.get("/api/private")
        assert private_resp.status_code == 200
        assert private_resp.json() == {"ok": True}


# ---- PR #49 review hardening: rate-limit + race ----------------------------


class TestRateLimits:
    """M2: ``/passkey/has-any`` and ``/passkey/authenticate/begin`` are
    public and allocate state — they must trip the same per-IP bucket
    as ``/login`` so a noisy or malicious client can't balloon
    in-process ceremony state or DB load."""

    def test_has_any_rate_limited_after_burst(self, db_factory):
        from core import auth as core_auth
        c = _no_auth_client(db_factory)
        # 5/60s bucket — first 5 succeed, 6th trips.
        for _ in range(core_auth.LOGIN_RATE_LIMIT):
            r = c.get(
                "/api/auth/passkey/has-any",
                headers={"Host": "localhost:3001"},
            )
            assert r.status_code == 200
        r = c.get(
            "/api/auth/passkey/has-any",
            headers={"Host": "localhost:3001"},
        )
        assert r.status_code == 429
        assert r.json()["detail"]["error"] == "rate_limited"

    def test_authenticate_begin_rate_limited_after_burst(self, db_factory):
        from core import auth as core_auth
        c = _no_auth_client(db_factory)
        for _ in range(core_auth.LOGIN_RATE_LIMIT):
            r = c.post(
                "/api/auth/passkey/authenticate/begin",
                headers={"Host": "localhost:3001"},
            )
            assert r.status_code == 200
        r = c.post(
            "/api/auth/passkey/authenticate/begin",
            headers={"Host": "localhost:3001"},
        )
        assert r.status_code == 429


class TestRegisterFinishRace:
    """M3: simultaneous register/finish for the same credential id
    must not surface as a 500 UniqueConstraintViolation. The
    ``IntegrityError`` retry path converts the lost race into the
    same shape as the "existing row, refresh fields" branch."""

    def test_race_winner_loses_to_existing_row_returns_replaced_true(
        self, client, db_factory, monkeypatch,
    ):
        # Begin → mocked verify returns a credential_id that ALREADY
        # exists in the DB. The "existing is None" probe sees it and
        # takes the update branch immediately — we still cover that
        # path. The genuine race (commit-time IntegrityError) is
        # harder to reproduce deterministically in a unit test; this
        # case at least pins the user-visible contract: same id twice
        # → "replaced": True, never 500.
        existing_id = wa.b64url_encode(b"race-existing-id")
        with db_factory() as db:
            db.add(PasskeyCredential(
                id=existing_id, user_id=DEFAULT_USER_ID,
                public_key=b"old-key", sign_count=0, rp_id="localhost",
            ))
            db.commit()

        begin = client.post(
            "/api/auth/passkey/register/begin",
            headers={"Host": "localhost:3001"},
        ).json()
        monkeypatch.setattr(
            wa, "verify_registration",
            lambda **kw: _fake_verified_registration(
                credential_id=b"race-existing-id",
                public_key=b"new-key",
                sign_count=1,
            ),
        )
        resp = client.post(
            "/api/auth/passkey/register/finish",
            headers={"Host": "localhost:3001"},
            json={
                "ceremony_id": begin["ceremony_id"],
                "credential": {"id": "x", "response": {}},
                "label": "race-winner",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["replaced"] is True

        with db_factory() as db:
            rows = db.query(PasskeyCredential).filter(
                PasskeyCredential.id == existing_id,
            ).all()
            assert len(rows) == 1  # no duplicate inserted
            assert rows[0].public_key == b"new-key"  # refreshed
            assert rows[0].sign_count == 1
