"""Symmetric encryption for ``SystemConfig`` secret values.

The Settings UI stores user-entered secrets (API keys, passwords, OAuth tokens)
in the ``system_config`` table. Those rows are encrypted at rest with Fernet
(AES-128-CBC + HMAC-SHA256) and a key derived from the operator's
``JARVIS_MASTER_KEY``.

Design notes
------------

* **Master key is separate from auth key.** ``JARVIS_MASTER_KEY`` is the sole
  input to the Fernet derivation. ``JARVIS_API_KEY`` (the web/auth password)
  used to double as the master, but operators rotate the auth password as a
  routine action — coupling the two crashed CD repeatedly. They are now
  decoupled: rotating the auth password no longer touches stored ciphertext.
* The key is derived via HKDF-SHA256 with a fixed application salt so it
  cannot be reused for unrelated purposes.
* **Versioned token format** ``v1:<urlsafe_b64_ciphertext>``. Future schemes
  (key rotation, AEAD upgrades) can ship under ``v2:`` while still reading ``v1``.
* **Returns ``None`` on decrypt failure**, never raises. Callers decide whether
  to treat the secret as missing, prompt the user to re-enter it, etc. We never
  log the ciphertext or plaintext — only the token version + a fingerprint.
* **Thread-safe lazy init** via an ``RLock``. ``reload_master_key()`` is called
  by the rotate-master-key CLI after re-encrypting the DB.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from base64 import urlsafe_b64encode
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger(__name__)

# ---- Constants ---------------------------------------------------------------

#: Salt used for HKDF derivation. Fixed (not secret) — its job is domain
#: separation, not entropy. Changing this value invalidates every existing
#: ciphertext in the database.
_HKDF_SALT = b"jarvis-config-store-v1"

#: Info string for HKDF — distinguishes this key from any other key derived
#: from the same master in the future (e.g. session signing).
_HKDF_INFO = b"jarvis-settings-secret-encryption"

#: Token prefix. Bumping the version is the signal to add a migration path.
_TOKEN_VERSION = "v1"
_TOKEN_PREFIX = f"{_TOKEN_VERSION}:"


# ---- Errors ------------------------------------------------------------------


class MissingMasterKeyError(RuntimeError):
    """Raised when ``JARVIS_MASTER_KEY`` is unavailable during encryption."""


class DecryptError(RuntimeError):
    """Raised when a stored secret's ciphertext cannot be decrypted under the
    current master key — typically because the key was rotated without
    re-encrypting the DB. Bootstrap-level callers catch this specifically to
    soft-fail per-secret instead of crashing the whole backend."""


# ---- Internal state ----------------------------------------------------------

_lock = threading.RLock()
_fernet: Optional[Fernet] = None
_fingerprint: Optional[str] = None


def _read_master_key() -> str:
    """Pull the master key from the environment."""
    key = os.getenv("JARVIS_MASTER_KEY", "").strip()
    if not key:
        raise MissingMasterKeyError(
            "JARVIS_MASTER_KEY is not set — cannot encrypt or decrypt secrets. "
            "Set it in your .env (or docker environment) before starting the "
            "backend. Generate one with: python -c 'import secrets; "
            "print(secrets.token_urlsafe(32))'."
        )
    return key


def _derive_fernet_key(master: str) -> bytes:
    """HKDF-SHA256 → 32 raw bytes → urlsafe-b64 (Fernet's expected format)."""
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_HKDF_SALT,
        info=_HKDF_INFO,
    )
    raw = hkdf.derive(master.encode("utf-8"))
    return urlsafe_b64encode(raw)


def _ensure_fernet() -> Fernet:
    """Lazy singleton; rebuilds itself after ``reload_master_key``."""
    global _fernet, _fingerprint
    with _lock:
        if _fernet is not None:
            return _fernet
        master = _read_master_key()
        fernet_key = _derive_fernet_key(master)
        _fernet = Fernet(fernet_key)
        _fingerprint = hashlib.sha256(fernet_key).hexdigest()[:12]
        logger.info("[SECRETS] Master key initialised (fingerprint=%s)", _fingerprint)
        return _fernet


# ---- Public API --------------------------------------------------------------


def encrypt(plaintext: str) -> str:
    """Encrypt ``plaintext`` and return a versioned token.

    Raises:
        MissingMasterKeyError: when no master key is configured.
        TypeError: when ``plaintext`` is not a string.
    """
    if not isinstance(plaintext, str):
        raise TypeError(f"encrypt() expects str, got {type(plaintext).__name__}")
    fernet = _ensure_fernet()
    token = fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
    return _TOKEN_PREFIX + token


def decrypt(token: str) -> Optional[str]:
    """Decrypt a token produced by :func:`encrypt`.

    Returns ``None`` if the token is malformed, has an unknown version, or was
    encrypted with a different master key (e.g. after rotation).
    """
    if not isinstance(token, str) or not token:
        return None
    if not token.startswith(_TOKEN_PREFIX):
        # Either an unencrypted legacy value or a version we don't understand.
        # Be silent for short/empty inputs; warn for everything else (without leaking content).
        if len(token) > 4:
            logger.warning("[SECRETS] decrypt: unknown token version (prefix=%r)", token[:4])
        return None
    payload = token[len(_TOKEN_PREFIX):]
    try:
        fernet = _ensure_fernet()
        return fernet.decrypt(payload.encode("ascii")).decode("utf-8")
    except InvalidToken:
        # Tampered, truncated, or encrypted under a previous master key.
        logger.warning(
            "[SECRETS] decrypt failed: InvalidToken (current_fingerprint=%s)",
            _fingerprint,
        )
        return None
    except MissingMasterKeyError:
        logger.error("[SECRETS] decrypt failed: master key missing")
        return None
    except Exception:
        # Defensive — never let crypto errors crash a request.
        logger.exception("[SECRETS] decrypt failed with unexpected error")
        return None


def is_encrypted(value: str) -> bool:
    """Cheap check: does ``value`` look like one of our tokens?"""
    return isinstance(value, str) and value.startswith(_TOKEN_PREFIX)


def reload_master_key() -> str:
    """Force re-derivation after ``JARVIS_MASTER_KEY`` changes in env.

    Should only be called by the rotate-master-key CLI after it has
    re-encrypted every stored secret under the new key. Calling this without
    a prior re-encryption renders all existing ciphertext unreadable.

    Returns the new key fingerprint for diagnostic logging.
    """
    global _fernet, _fingerprint
    with _lock:
        _fernet = None
        _fingerprint = None
        _ensure_fernet()
        assert _fingerprint is not None
        return _fingerprint


def get_master_fingerprint() -> Optional[str]:
    """Short hex digest of the current Fernet key — useful for diagnostics.

    Returns ``None`` if the master key has not been initialised yet.
    """
    with _lock:
        return _fingerprint
