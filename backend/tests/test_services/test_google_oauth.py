"""Tests for services.google_oauth — web-flow helpers."""
from __future__ import annotations

import time

import pytest

from core import auth as core_auth


@pytest.fixture()
def oauth_env(tmp_path, monkeypatch):
    """Wire ConfigService to a throwaway DB and set a master key."""
    from core import secrets_crypto
    from services import google_oauth

    # Master key so Fernet works.
    key = "oauth-tests-master-key-xxxxx"
    monkeypatch.setenv("JARVIS_API_KEY", key)
    monkeypatch.setattr(core_auth, "JARVIS_API_KEY", key)
    secrets_crypto.reload_master_key()

    from core.database import Base
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from services.config_service import ConfigService

    engine = create_engine(f"sqlite:///{tmp_path}/oauth.db", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True, expire_on_commit=False)
    service = ConfigService(db_factory=Session)

    monkeypatch.setattr(google_oauth, "config_service", service)
    return google_oauth


class TestClientStorage:
    def test_save_load_round_trip(self, oauth_env):
        oauth_env.save_client("cid-123", "secret-456", "desktop")
        client = oauth_env.load_client()
        assert client.client_id == "cid-123"
        assert client.client_secret == "secret-456"
        assert oauth_env.client_type() == "desktop"

    def test_web_client_type_round_trip(self, oauth_env):
        oauth_env.save_client("cid-web", "secret-web", "web")
        assert oauth_env.client_type() == "web"

    def test_missing_returns_none(self, oauth_env):
        assert oauth_env.load_client() is None

    def test_rejects_empty(self, oauth_env):
        with pytest.raises(ValueError):
            oauth_env.save_client("", "x", "desktop")

    def test_rejects_invalid_client_type(self, oauth_env):
        with pytest.raises(ValueError):
            oauth_env.save_client("cid", "sec", "installed")  # type: ignore[arg-type]

    def test_clear_client_drops_everything(self, oauth_env):
        oauth_env.save_client("db-cid", "db-sec", "desktop")
        oauth_env.clear_client()
        assert oauth_env.client_type() == "none"
        assert oauth_env.load_client() is None

    def test_client_type_none_when_not_configured(self, oauth_env):
        assert oauth_env.client_type() == "none"
        assert oauth_env.load_client() is None

    def test_legacy_row_without_client_type_defaults_to_web(self, oauth_env):
        # Simulate an older install: only client_id + client_secret saved,
        # no client_type yet. That path came from the legacy custom
        # flow which was always a Web-application client.
        svc = oauth_env.config_service
        svc.set("oauth.google", "client_id", "legacy-cid", is_secret=True)
        svc.set("oauth.google", "client_secret", "legacy-sec", is_secret=True)
        assert oauth_env.client_type() == "web"


class TestSeedClientFromEnv:
    """Boot-time migration GOOGLE_OAUTH_CLIENT_ID/SECRET env → DB row.

    This is the critical upgrade path for existing deployments: the env
    vars were the source of truth before the DB-first refactor and users
    shouldn't have to re-paste credentials on first restart.
    """

    def test_migrates_env_to_db_with_desktop_type(self, oauth_env, monkeypatch):
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "env-cid")
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "env-sec")
        assert oauth_env.seed_client_from_env() is True
        client = oauth_env.load_client()
        assert client.client_id == "env-cid"
        assert client.client_secret == "env-sec"
        # Env is how docker-compose advised users to configure the Desktop
        # loopback flow, so that's the type we assume.
        assert oauth_env.client_type() == "desktop"

    def test_noop_when_env_missing(self, oauth_env, monkeypatch):
        monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
        monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
        assert oauth_env.seed_client_from_env() is False
        assert oauth_env.load_client() is None

    def test_noop_when_only_id_set(self, oauth_env, monkeypatch):
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "only-id")
        monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
        assert oauth_env.seed_client_from_env() is False
        assert oauth_env.load_client() is None

    def test_noop_when_env_values_are_whitespace(self, oauth_env, monkeypatch):
        # Empty strings in .env (e.g. `GOOGLE_OAUTH_CLIENT_ID=`) show up as
        # "" — we must not seed with blanks because Google's OAuth endpoints
        # will reject them with an opaque error later.
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "   ")
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
        assert oauth_env.seed_client_from_env() is False
        assert oauth_env.load_client() is None

    def test_never_overwrites_existing_db_client(self, oauth_env, monkeypatch):
        # User already saved creds via the Settings UI — env vars must NOT
        # silently clobber that choice on next restart.
        oauth_env.save_client("ui-cid", "ui-sec", "web")
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "env-cid")
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "env-sec")
        assert oauth_env.seed_client_from_env() is False
        client = oauth_env.load_client()
        assert client.client_id == "ui-cid"
        assert oauth_env.client_type() == "web"

    def test_strips_whitespace_from_env_values(self, oauth_env, monkeypatch):
        # Users paste values with trailing newlines from terminal copy/paste
        # — the seed should normalise just like the Settings UI PUT does.
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "  env-cid\n")
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", " env-sec ")
        assert oauth_env.seed_client_from_env() is True
        client = oauth_env.load_client()
        assert client.client_id == "env-cid"
        assert client.client_secret == "env-sec"

    def test_idempotent_across_restarts(self, oauth_env, monkeypatch):
        # Second call after a successful seed must be a no-op (the first
        # call wrote to DB, so the second sees the DB client and bails).
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "env-cid")
        monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "env-sec")
        assert oauth_env.seed_client_from_env() is True
        assert oauth_env.seed_client_from_env() is False


class TestTokenStorage:
    def test_save_load_round_trip(self, oauth_env):
        tokens = oauth_env.GoogleOAuthTokens(
            access_token="AT",
            refresh_token="RT",
            expires_at=1_700_000_000.0,
            scopes=("openid", "email"),
            token_uri=oauth_env.DEFAULT_TOKEN_URI,
        )
        oauth_env.save_tokens(tokens)
        loaded = oauth_env.load_tokens()
        assert loaded.access_token == "AT"
        assert loaded.refresh_token == "RT"
        assert loaded.scopes == ("openid", "email")
        assert loaded.expires_at == 1_700_000_000.0

    def test_load_missing_returns_none(self, oauth_env):
        assert oauth_env.load_tokens() is None

    def test_clear_tokens(self, oauth_env):
        oauth_env.save_tokens(oauth_env.GoogleOAuthTokens(
            access_token="AT", refresh_token="RT",
            expires_at=0, scopes=("x",),
            token_uri=oauth_env.DEFAULT_TOKEN_URI,
        ))
        oauth_env.clear_tokens()
        assert oauth_env.load_tokens() is None


class TestConsentUrl:
    def test_builds_url_with_expected_params(self, oauth_env):
        oauth_env.save_client("cid-123", "secret-456", "desktop")
        url = oauth_env.build_consent_url(
            redirect_uri="http://localhost/cb", state="nonce", scopes=("openid",)
        )
        assert url.startswith(oauth_env.DEFAULT_AUTH_URI + "?")
        assert "client_id=cid-123" in url
        assert "redirect_uri=http%3A%2F%2Flocalhost%2Fcb" in url
        assert "state=nonce" in url
        assert "access_type=offline" in url
        assert "prompt=consent" in url

    def test_raises_when_client_missing(self, oauth_env):
        with pytest.raises(RuntimeError, match="not configured"):
            oauth_env.build_consent_url(
                redirect_uri="http://x/cb", state="n"
            )


class _FakeResp:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status
        self.ok = 200 <= status < 300
        self.text = str(body)

    def json(self):
        return self._body


class TestExchangeCode:
    def test_stores_tokens_on_success(self, oauth_env, monkeypatch):
        oauth_env.save_client("cid", "secret", "desktop")

        captured = {}

        def fake_post(url, data=None, timeout=None):
            captured["url"] = url
            captured["data"] = data
            return _FakeResp({
                "access_token": "AT",
                "refresh_token": "RT",
                "expires_in": 3600,
                "scope": "openid email",
            })

        monkeypatch.setattr(oauth_env.requests, "post", fake_post)
        tokens = oauth_env.exchange_code(code="auth-code", redirect_uri="http://x/cb")
        assert tokens.access_token == "AT"
        assert tokens.refresh_token == "RT"
        assert tokens.scopes == ("openid", "email")
        # And they were persisted:
        assert oauth_env.load_tokens().access_token == "AT"
        # Request shape sanity checks:
        assert captured["url"] == oauth_env.DEFAULT_TOKEN_URI
        assert captured["data"]["code"] == "auth-code"
        assert captured["data"]["grant_type"] == "authorization_code"

    def test_failure_does_not_persist(self, oauth_env, monkeypatch):
        oauth_env.save_client("cid", "secret", "desktop")
        monkeypatch.setattr(
            oauth_env.requests, "post",
            lambda *a, **kw: _FakeResp({"error": "invalid_grant"}, status=400),
        )
        with pytest.raises(RuntimeError):
            oauth_env.exchange_code(code="bad", redirect_uri="http://x/cb")
        assert oauth_env.load_tokens() is None


class TestGetCredentials:
    def test_returns_none_when_not_connected(self, oauth_env):
        assert oauth_env.get_credentials() is None

    def test_returns_credentials_when_fresh(self, oauth_env):
        oauth_env.save_client("cid", "secret", "desktop")
        oauth_env.save_tokens(oauth_env.GoogleOAuthTokens(
            access_token="fresh-AT",
            refresh_token="RT",
            expires_at=time.time() + 3600,
            scopes=("openid",),
            token_uri=oauth_env.DEFAULT_TOKEN_URI,
        ))
        creds = oauth_env.get_credentials()
        assert creds is not None
        assert creds.token == "fresh-AT"
        assert creds.refresh_token == "RT"
        assert creds.client_id == "cid"

    def test_refreshes_when_expired(self, oauth_env, monkeypatch):
        oauth_env.save_client("cid", "secret", "desktop")
        oauth_env.save_tokens(oauth_env.GoogleOAuthTokens(
            access_token="stale-AT",
            refresh_token="RT",
            expires_at=time.time() - 1,  # expired
            scopes=("openid",),
            token_uri=oauth_env.DEFAULT_TOKEN_URI,
        ))

        def fake_post(url, data=None, timeout=None):
            assert data["grant_type"] == "refresh_token"
            return _FakeResp({
                "access_token": "new-AT",
                "expires_in": 3600,
                "scope": "openid",
            })

        monkeypatch.setattr(oauth_env.requests, "post", fake_post)
        creds = oauth_env.get_credentials()
        assert creds.token == "new-AT"
        # Refresh token preserved (Google often omits it on refresh).
        assert creds.refresh_token == "RT"

    def test_returns_none_on_refresh_failure(self, oauth_env, monkeypatch):
        oauth_env.save_client("cid", "secret", "desktop")
        oauth_env.save_tokens(oauth_env.GoogleOAuthTokens(
            access_token="stale-AT",
            refresh_token="RT",
            expires_at=time.time() - 1,
            scopes=("openid",),
            token_uri=oauth_env.DEFAULT_TOKEN_URI,
        ))
        monkeypatch.setattr(
            oauth_env.requests, "post",
            lambda *a, **kw: _FakeResp({"error": "invalid_grant"}, status=400),
        )
        assert oauth_env.get_credentials() is None

    def test_no_refresh_token_returns_none_when_expired(self, oauth_env):
        oauth_env.save_client("cid", "secret", "desktop")
        oauth_env.save_tokens(oauth_env.GoogleOAuthTokens(
            access_token="stale-AT",
            refresh_token=None,
            expires_at=time.time() - 1,
            scopes=("openid",),
            token_uri=oauth_env.DEFAULT_TOKEN_URI,
        ))
        assert oauth_env.get_credentials() is None
