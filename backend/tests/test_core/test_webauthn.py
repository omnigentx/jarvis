"""Tests for ``core.webauthn`` — RP-ID derivation, ceremony store, and
ceremony helpers.

The real attestation/assertion verification is exercised end-to-end in
``tests/test_routes/test_passkey_routes.py`` with a fixture credential.
Here we focus on the parts that are easy to break in isolation:

* RP-ID and origin parsing for every shape of Host header we might see
  in production (bare hostname, host:port, IPv6, X-Forwarded-Host,
  X-Forwarded-Proto).
* Ceremony store: TTL eviction, single-use semantics, kind-mismatch
  rejection, RP-ID rebinding on verify.
* ``options_dict`` round-trips through JSON (so the response is
  actually shippable as JSON to the browser).
"""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from core import webauthn as wa


def _make_request(
    *,
    host: str = "localhost:3001",
    scheme: str = "http",
    forwarded_host: str = "",
    forwarded_proto: str = "",
):
    """Build a stub matching the ``Request`` surface we touch.

    We don't pull in starlette's Request because constructing one
    requires a full ASGI scope; everything ``core.webauthn`` uses is
    ``request.headers.get(...)`` and ``request.url.{scheme,netloc}``,
    so a dotted namespace with a dict-like ``headers`` is enough.
    """
    headers = {"host": host}
    if forwarded_host:
        headers["x-forwarded-host"] = forwarded_host
    if forwarded_proto:
        headers["x-forwarded-proto"] = forwarded_proto
    return SimpleNamespace(
        headers=headers,
        url=SimpleNamespace(scheme=scheme, netloc=host),
    )


@pytest.fixture(autouse=True)
def _clean_ceremonies():
    wa._clear_ceremonies()
    yield
    wa._clear_ceremonies()


# ---- RP-ID and origin derivation -------------------------------------------


class TestRpIdFromRequest:
    def test_strips_port_from_host_header(self):
        req = _make_request(host="localhost:3001")
        assert wa.rp_id_from_request(req) == "localhost"

    def test_bare_hostname_no_port(self):
        req = _make_request(host="jarvis.alice.com")
        assert wa.rp_id_from_request(req) == "jarvis.alice.com"

    def test_ipv4_with_port(self):
        req = _make_request(host="192.168.1.50:8001")
        assert wa.rp_id_from_request(req) == "192.168.1.50"

    def test_ipv6_bracketed_with_port(self):
        req = _make_request(host="[::1]:3001")
        assert wa.rp_id_from_request(req) == "::1"

    def test_ipv6_bracketed_no_port(self):
        req = _make_request(host="[::1]")
        assert wa.rp_id_from_request(req) == "::1"

    def test_forwarded_host_takes_precedence(self):
        # Reverse proxy talks to backend at localhost:8001 but the
        # public origin is jarvis.alice.com — RP ID must follow the
        # public name so the browser-signed origin matches.
        req = _make_request(
            host="localhost:8001",
            forwarded_host="jarvis.alice.com",
        )
        assert wa.rp_id_from_request(req) == "jarvis.alice.com"

    def test_forwarded_host_multi_entry_picks_leftmost(self):
        req = _make_request(
            host="localhost:8001",
            forwarded_host="jarvis.alice.com, internal-proxy:9000",
        )
        assert wa.rp_id_from_request(req) == "jarvis.alice.com"


class TestOriginFromRequest:
    def test_http_localhost(self):
        req = _make_request(host="localhost:3001", scheme="http")
        assert wa.origin_from_request(req) == "http://localhost:3001"

    def test_https_via_url_scheme(self):
        req = _make_request(host="jarvis.alice.com", scheme="https")
        assert wa.origin_from_request(req) == "https://jarvis.alice.com"

    def test_https_via_forwarded_proto(self):
        req = _make_request(
            host="jarvis.alice.com",
            scheme="http",  # connection to backend is plaintext
            forwarded_proto="https",  # but public-facing is https
        )
        assert wa.origin_from_request(req) == "https://jarvis.alice.com"

    def test_https_inferred_for_public_host_with_no_proto_hint(self):
        # Regression: Cloudflare tunnel → nginx → app terminates TLS upstream
        # and speaks plain http internally, so both request.url.scheme AND the
        # forwarded proto arrive as http. A public host is still https in the
        # browser, so we must infer https from the host, not trust the proxy —
        # otherwise verify_registration_response raised InvalidRegistrationResponse
        # ("Attestation rejected") on app.omnigentx.com.
        req = _make_request(host="app.omnigentx.com", scheme="http")
        assert wa.origin_from_request(req) == "https://app.omnigentx.com"

    def test_loopback_stays_http_for_dev(self):
        # The one plaintext exception WebAuthn allows: loopback dev keeps http.
        for h in ("localhost:3001", "127.0.0.1:8001", "[::1]:8000"):
            req = _make_request(host=h, scheme="http")
            assert wa.origin_from_request(req).startswith("http://")


# ---- Ceremony store --------------------------------------------------------


class TestCeremonyStore:
    def test_store_then_pop_returns_payload_once(self):
        cid = wa.store_ceremony(
            challenge=b"chal-123",
            rp_id="localhost",
            user_id="u-1",
            kind="register",
        )
        entry = wa.pop_ceremony(cid, kind="register")
        assert entry is not None
        assert entry.challenge == b"chal-123"
        assert entry.user_id == "u-1"
        # Single-use: a second pop returns None.
        assert wa.pop_ceremony(cid, kind="register") is None

    def test_pop_with_wrong_kind_returns_none_and_removes(self):
        cid = wa.store_ceremony(
            challenge=b"x",
            rp_id="localhost",
            user_id="u-1",
            kind="register",
        )
        # Cross-flow attempt: asking for "authenticate" when the
        # ceremony was filed as "register" is treated as hostile.
        assert wa.pop_ceremony(cid, kind="authenticate") is None
        # …and the ceremony is consumed, not left dangling for retry.
        assert wa.pop_ceremony(cid, kind="register") is None

    def test_unknown_ceremony_id_returns_none(self):
        assert wa.pop_ceremony("never-stored", kind="register") is None

    def test_expired_ceremony_is_gc_d_on_next_access(self, monkeypatch):
        cid = wa.store_ceremony(
            challenge=b"x", rp_id="localhost", user_id="u-1", kind="register",
        )
        assert wa._ceremony_count() == 1
        # Jump forward past the TTL.
        now_future = time.time() + wa._CEREMONY_TTL_SECONDS + 1
        monkeypatch.setattr(wa.time, "time", lambda: now_future)
        # _gc_ceremonies runs inside _ceremony_count; the stale row is
        # dropped, and a subsequent pop sees nothing.
        assert wa._ceremony_count() == 0
        assert wa.pop_ceremony(cid, kind="register") is None

    def test_each_store_emits_distinct_ids(self):
        ids = {
            wa.store_ceremony(
                challenge=b"x", rp_id="localhost", user_id="u-1", kind="register",
            )
            for _ in range(20)
        }
        assert len(ids) == 20


# ---- Registration ceremony round-trip --------------------------------------


class TestBuildRegistrationOptions:
    def test_returns_ceremony_id_and_json_options(self):
        req = _make_request(host="localhost:3001")
        cid, opts = wa.build_registration_options(
            request=req,
            user_id="u-1",
            username="owner",
            existing_credential_ids=[],
        )
        # ceremony id is non-empty and unique-ish.
        assert isinstance(cid, str) and len(cid) > 16
        # options must JSON-serialize untouched (we already pre-parsed
        # via options_to_json, so dumps should never raise).
        json.dumps(opts)
        # WebAuthn-required fields present.
        assert opts["rp"]["id"] == "localhost"
        assert opts["rp"]["name"] == wa.RP_NAME
        assert opts["user"]["name"] == "owner"
        # Resident-key required so passkey is discoverable without
        # the server first sending allowCredentials. UV required so
        # the credential is "what you have + what you are" (Touch ID
        # / PIN / Face ID), not just "what you have" (stolen YubiKey
        # without PIN).
        sel = opts.get("authenticatorSelection", {})
        assert sel.get("residentKey") == "required"
        assert sel.get("userVerification") == "required"

    def test_excludes_existing_credentials(self):
        req = _make_request(host="localhost:3001")
        existing = [wa.b64url_encode(b"existing-cred-id-bytes")]
        _, opts = wa.build_registration_options(
            request=req,
            user_id="u-1",
            username="owner",
            existing_credential_ids=existing,
        )
        excl = opts.get("excludeCredentials", [])
        assert len(excl) == 1
        # The browser receives the credential id as base64url too.
        assert excl[0]["id"] == existing[0]

    def test_ceremony_stored_with_request_rp_id(self):
        req = _make_request(host="jarvis.alice.com")
        cid, _ = wa.build_registration_options(
            request=req,
            user_id="u-1",
            username="owner",
            existing_credential_ids=[],
        )
        entry = wa.pop_ceremony(cid, kind="register")
        assert entry is not None
        assert entry.rp_id == "jarvis.alice.com"
        assert entry.user_id == "u-1"


class TestVerifyRegistration:
    def test_unknown_ceremony_raises(self):
        req = _make_request(host="localhost:3001")
        with pytest.raises(ValueError, match="ceremony_unknown_or_expired"):
            wa.verify_registration(
                request=req,
                ceremony_id="bogus",
                credential={},
                expected_user_id="u-1",
            )

    def test_user_mismatch_raises(self):
        req = _make_request(host="localhost:3001")
        cid, _ = wa.build_registration_options(
            request=req,
            user_id="u-actual",
            username="owner",
            existing_credential_ids=[],
        )
        # Wrong user — even single-user mode upholds the invariant.
        with pytest.raises(ValueError, match="ceremony_user_mismatch"):
            wa.verify_registration(
                request=req,
                ceremony_id=cid,
                credential={},
                expected_user_id="u-someone-else",
            )

    def test_rp_id_mismatch_raises(self):
        # User started register on one origin and the finish call
        # arrives on a different RP — most plausibly a stale tab after
        # the domain changed. Reject loudly so we don't silently bind
        # a credential to the wrong RP.
        req_register = _make_request(host="localhost:3001")
        cid, _ = wa.build_registration_options(
            request=req_register,
            user_id="u-1",
            username="owner",
            existing_credential_ids=[],
        )
        req_finish = _make_request(host="jarvis.alice.com")
        with pytest.raises(ValueError, match="ceremony_rp_mismatch"):
            wa.verify_registration(
                request=req_finish,
                ceremony_id=cid,
                credential={},
                expected_user_id="u-1",
            )


# ---- Authentication ceremony round-trip ------------------------------------


class TestBuildAuthenticationOptions:
    def test_emits_allow_credentials_when_passed(self):
        req = _make_request(host="localhost:3001")
        allow = [wa.b64url_encode(b"cred-id-1"), wa.b64url_encode(b"cred-id-2")]
        _, opts = wa.build_authentication_options(
            request=req, allow_credential_ids=allow,
        )
        listed = opts.get("allowCredentials", [])
        assert {c["id"] for c in listed} == set(allow)

    def test_empty_allow_means_any_discoverable_credential(self):
        req = _make_request(host="localhost:3001")
        _, opts = wa.build_authentication_options(
            request=req, allow_credential_ids=[],
        )
        # Either omitted or present-but-empty — both signal "any
        # resident credential the platform offers".
        assert not opts.get("allowCredentials")

    def test_ceremony_stored_as_authenticate_kind(self):
        req = _make_request(host="localhost:3001")
        cid, _ = wa.build_authentication_options(
            request=req, allow_credential_ids=[],
        )
        # Wrong kind → pop refuses (cross-flow defence).
        assert wa.pop_ceremony(cid, kind="register") is None


class TestVerifyAuthentication:
    def test_unknown_ceremony_raises(self):
        req = _make_request(host="localhost:3001")
        with pytest.raises(ValueError, match="ceremony_unknown_or_expired"):
            wa.verify_authentication(
                request=req,
                ceremony_id="bogus",
                credential={},
                credential_public_key=b"",
                credential_current_sign_count=0,
            )

    def test_rp_id_mismatch_raises(self):
        req_begin = _make_request(host="localhost:3001")
        cid, _ = wa.build_authentication_options(
            request=req_begin, allow_credential_ids=[],
        )
        req_finish = _make_request(host="jarvis.alice.com")
        with pytest.raises(ValueError, match="ceremony_rp_mismatch"):
            wa.verify_authentication(
                request=req_finish,
                ceremony_id=cid,
                credential={},
                credential_public_key=b"",
                credential_current_sign_count=0,
            )


# ---- base64url helpers -----------------------------------------------------


class TestBase64UrlRoundTrip:
    def test_round_trip_arbitrary_bytes(self):
        for b in (b"", b"a", b"hello, world", bytes(range(256))):
            encoded = wa.b64url_encode(b)
            assert "=" not in encoded
            assert "+" not in encoded
            assert "/" not in encoded
            assert wa._b64url_decode(encoded) == b
