"""Tests for ``core.session`` — HMAC-signed session tokens.

Cross-layer invariants under test:

1. **Round-trip**: a freshly-minted token verifies cleanly.
2. **Tamper resistance**: mutating any byte of payload or signature
   raises with a stable ``reason``.
3. **Expiry**: tokens past ``exp`` raise ``expired``.
4. **Hard ceiling**: refresh chains cannot extend ``abs_exp``; once it
   passes, ``max_lifetime_exceeded`` fires even with a fresh ``exp``.
5. **Key rotation**: changing ``JARVIS_API_KEY`` invalidates every
   existing session via the embedded fingerprint.
6. **Signing-secret rotation**: changing ``JWT_SECRET`` invalidates
   every existing session.
7. **JWT_SECRET missing**: minting fails loudly (no silent forgery).
"""
from __future__ import annotations

import time

import pytest

from core import auth as core_auth
from core import session as core_session
from core.session import (
    SessionVerifyError,
    create_session_token,
    refresh_session_token,
    verify_session_token,
)


@pytest.fixture(autouse=True)
def _stable_secrets(monkeypatch):
    """Pin both secrets so each test starts deterministic."""
    monkeypatch.setenv("JWT_SECRET", "test-jwt-secret-xxxxxxxxxxxxxxxxxxxx")
    monkeypatch.setattr(core_auth, "JARVIS_API_KEY", "test-api-key-xxxxxxxxxxxxxxxx")
    yield


class TestRoundTrip:
    def test_mint_and_verify(self):
        token, payload = create_session_token()
        verified = verify_session_token(token)
        assert verified["sid"] == payload["sid"]
        assert verified["kfp"] == payload["kfp"]

    def test_payload_has_required_fields(self):
        _, payload = create_session_token()
        for field in ("iat", "exp", "nbf", "sid", "kfp", "abs_exp"):
            assert field in payload, f"missing {field}"
        assert payload["exp"] > payload["iat"]
        assert payload["abs_exp"] >= payload["exp"]
        assert len(payload["sid"]) == 32  # 16 bytes hex
        assert len(payload["kfp"]) == 16  # 8 bytes hex prefix


class TestTamperResistance:
    def test_flipped_signature_rejected(self):
        token, _ = create_session_token()
        # Flip the last char of the sig. Use a different ASCII char to
        # ensure the value actually changes.
        body, sig = token.rsplit(".", 1)
        flipped = sig[:-1] + ("A" if sig[-1] != "A" else "B")
        bad = f"{body}.{flipped}"
        with pytest.raises(SessionVerifyError) as exc:
            verify_session_token(bad)
        assert exc.value.reason == "invalid_signature"

    def test_flipped_payload_rejected(self):
        token, _ = create_session_token()
        body, sig = token.rsplit(".", 1)
        flipped = body[:-1] + ("A" if body[-1] != "A" else "B")
        bad = f"{flipped}.{sig}"
        with pytest.raises(SessionVerifyError) as exc:
            verify_session_token(bad)
        # Could be invalid_signature OR malformed depending on whether the
        # flip lands on a base64 char that still decodes.
        assert exc.value.reason in {"invalid_signature", "malformed"}

    def test_malformed_token(self):
        with pytest.raises(SessionVerifyError) as exc:
            verify_session_token("not-a-token")
        assert exc.value.reason == "malformed"

    def test_empty_token(self):
        with pytest.raises(SessionVerifyError) as exc:
            verify_session_token("")
        assert exc.value.reason == "malformed"


class TestExpiry:
    def test_token_after_exp_rejected(self):
        # Mint with iat 2 hours in the past so exp is in the past too.
        far_past = int(time.time()) - 7200
        token, _ = create_session_token(now=far_past)
        with pytest.raises(SessionVerifyError) as exc:
            verify_session_token(token)
        assert exc.value.reason == "expired"

    def test_token_before_nbf_rejected(self):
        future = int(time.time()) + 3600
        token, _ = create_session_token(now=future)
        with pytest.raises(SessionVerifyError) as exc:
            verify_session_token(token)
        assert exc.value.reason == "not_yet_valid"


class TestHardCeiling:
    def test_refresh_preserves_abs_exp(self):
        """A refresh must NOT extend the original abs_exp ceiling — that
        invariant is what stops indefinitely-renewable session leaks."""
        token, original = create_session_token()
        new_token, refreshed = refresh_session_token(token)
        assert refreshed["abs_exp"] == original["abs_exp"]
        # Fresh exp window though
        assert refreshed["exp"] >= original["exp"]
        # New sid each time
        assert refreshed["sid"] != original["sid"]

    def test_max_lifetime_exceeded(self, monkeypatch):
        """Once abs_exp passes, the token is dead even if exp is fresh."""
        # Mint a token whose abs_exp is already expired but exp is fresh.
        # Easiest: monkeypatch SESSION_MAX_LIFETIME to 0 then mint.
        monkeypatch.setattr(core_session, "SESSION_MAX_LIFETIME_SECONDS", 0)
        token, _ = create_session_token()
        with pytest.raises(SessionVerifyError) as exc:
            verify_session_token(token)
        assert exc.value.reason == "max_lifetime_exceeded"


class TestKeyRotation:
    def test_rotating_api_key_invalidates_existing_sessions(self):
        token, _ = create_session_token()
        # Pretend the operator rotated the API key.
        core_auth.JARVIS_API_KEY = "different-api-key-yyyyyyyyyyyyyyyy"
        with pytest.raises(SessionVerifyError) as exc:
            verify_session_token(token)
        assert exc.value.reason == "key_rotated"

    def test_rotating_jwt_secret_invalidates_existing_sessions(self, monkeypatch):
        token, _ = create_session_token()
        monkeypatch.setenv("JWT_SECRET", "rotated-jwt-secret-zzzzzzzzzzzzzzzz")
        with pytest.raises(SessionVerifyError) as exc:
            verify_session_token(token)
        assert exc.value.reason == "invalid_signature"


class TestMissingSecrets:
    def test_mint_fails_without_jwt_secret(self, monkeypatch):
        monkeypatch.delenv("JWT_SECRET", raising=False)
        with pytest.raises(RuntimeError) as exc:
            create_session_token()
        assert "JWT_SECRET" in str(exc.value)


class TestRefreshChain:
    def test_refresh_only_works_for_valid_tokens(self):
        """``refresh_session_token`` must re-verify so a stale call site
        cannot accidentally re-mint a dead token."""
        # Build a token that's already expired.
        token, _ = create_session_token(now=int(time.time()) - 7200)
        with pytest.raises(SessionVerifyError) as exc:
            refresh_session_token(token)
        assert exc.value.reason == "expired"
