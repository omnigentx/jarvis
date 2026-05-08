"""Tests for core.secrets_crypto — Fernet-based secret encryption."""
import pytest

from core import secrets_crypto


@pytest.fixture(autouse=True)
def _reset_crypto_state(monkeypatch):
    """Each test starts from a clean module state + a known master key."""
    monkeypatch.setenv("JARVIS_MASTER_KEY", "test-master-key-please-rotate")
    # Force re-derivation on each test to avoid leakage between tests.
    secrets_crypto._fernet = None
    secrets_crypto._fingerprint = None
    yield
    secrets_crypto._fernet = None
    secrets_crypto._fingerprint = None


class TestRoundTrip:
    def test_basic_roundtrip(self):
        token = secrets_crypto.encrypt("sk-prod-abc123")
        assert token.startswith("v1:")
        assert secrets_crypto.decrypt(token) == "sk-prod-abc123"

    def test_empty_string_roundtrip(self):
        token = secrets_crypto.encrypt("")
        assert secrets_crypto.decrypt(token) == ""

    def test_unicode_roundtrip(self):
        secret = "Mật khẩu — 密码 — пароль 🔐"
        assert secrets_crypto.decrypt(secrets_crypto.encrypt(secret)) == secret

    def test_long_string_roundtrip(self):
        secret = "x" * 10_000
        assert secrets_crypto.decrypt(secrets_crypto.encrypt(secret)) == secret

    def test_distinct_ciphertexts_for_same_plaintext(self):
        """Fernet bakes in a random IV — same input must produce different tokens."""
        a = secrets_crypto.encrypt("same")
        b = secrets_crypto.encrypt("same")
        assert a != b
        assert secrets_crypto.decrypt(a) == secrets_crypto.decrypt(b) == "same"


class TestMissingKey:
    def test_encrypt_without_master_raises(self, monkeypatch):
        monkeypatch.delenv("JARVIS_MASTER_KEY", raising=False)
        secrets_crypto._fernet = None
        with pytest.raises(secrets_crypto.MissingMasterKeyError):
            secrets_crypto.encrypt("anything")

    def test_blank_master_raises(self, monkeypatch):
        monkeypatch.setenv("JARVIS_MASTER_KEY", "   ")
        secrets_crypto._fernet = None
        with pytest.raises(secrets_crypto.MissingMasterKeyError):
            secrets_crypto.encrypt("anything")


class TestDecryptFailures:
    def test_decrypt_none_returns_none(self):
        assert secrets_crypto.decrypt("") is None
        assert secrets_crypto.decrypt(None) is None  # type: ignore[arg-type]

    def test_decrypt_unknown_prefix_returns_none(self, caplog):
        with caplog.at_level("WARNING"):
            assert secrets_crypto.decrypt("v9:bogus") is None
        assert any("unknown token version" in r.message for r in caplog.records)

    def test_decrypt_unencrypted_legacy_value_returns_none(self):
        assert secrets_crypto.decrypt("plain-text-secret") is None

    def test_decrypt_tampered_token_returns_none(self, caplog):
        token = secrets_crypto.encrypt("real")
        # Flip the last char; still a v1 token but with broken HMAC.
        bad = token[:-1] + ("A" if token[-1] != "A" else "B")
        with caplog.at_level("WARNING"):
            assert secrets_crypto.decrypt(bad) is None
        assert any("decrypt failed" in r.message for r in caplog.records)


class TestKeyRotation:
    def test_key_change_invalidates_old_ciphertexts(self, monkeypatch):
        token = secrets_crypto.encrypt("api-key")
        assert secrets_crypto.decrypt(token) == "api-key"

        old_fp = secrets_crypto.get_master_fingerprint()

        monkeypatch.setenv("JARVIS_MASTER_KEY", "rotated-master-key-zzz")
        new_fp = secrets_crypto.reload_master_key()

        assert new_fp != old_fp
        assert secrets_crypto.decrypt(token) is None

    def test_reload_returns_new_fingerprint(self, monkeypatch):
        secrets_crypto.encrypt("warmup")  # ensures lazy init
        original = secrets_crypto.get_master_fingerprint()

        monkeypatch.setenv("JARVIS_MASTER_KEY", "different-master-key-xyz")
        new_fp = secrets_crypto.reload_master_key()
        assert new_fp == secrets_crypto.get_master_fingerprint()
        assert new_fp != original


class TestHelpers:
    def test_is_encrypted_recognises_our_tokens(self):
        token = secrets_crypto.encrypt("x")
        assert secrets_crypto.is_encrypted(token) is True

    def test_is_encrypted_rejects_other_strings(self):
        assert secrets_crypto.is_encrypted("plaintext") is False
        assert secrets_crypto.is_encrypted("") is False
        assert secrets_crypto.is_encrypted("v2:foo") is False  # different version
        assert secrets_crypto.is_encrypted(None) is False  # type: ignore[arg-type]

    def test_encrypt_rejects_non_string(self):
        with pytest.raises(TypeError):
            secrets_crypto.encrypt(123)  # type: ignore[arg-type]

    def test_fingerprint_initially_none(self, monkeypatch):
        # Module just reloaded by fixture — fingerprint should still be None until init.
        secrets_crypto._fernet = None
        secrets_crypto._fingerprint = None
        assert secrets_crypto.get_master_fingerprint() is None
        secrets_crypto.encrypt("warm")
        assert secrets_crypto.get_master_fingerprint() is not None
