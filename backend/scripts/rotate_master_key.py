"""One-shot CLI to rotate ``JARVIS_MASTER_KEY`` and re-encrypt every stored
secret in the system_config table.

Usage::

    JARVIS_MASTER_KEY_OLD=<current> \\
    JARVIS_MASTER_KEY_NEW=<new> \\
    python -m scripts.rotate_master_key

What it does
------------

1. Open the system_config DB.
2. For every row with ``is_secret=True``: decrypt the ciphertext under the
   OLD key, re-encrypt under the NEW key, write back in a single
   transaction.
3. Print a summary. On any error, the transaction rolls back — the DB
   stays under the old key.

After a clean run, set ``JARVIS_MASTER_KEY=<new>`` in your environment
(``.env`` / docker-compose) and restart the backend.

Why the operator workflow is two-step (re-encrypt, then swap env)
-----------------------------------------------------------------

If we re-derived the Fernet key in the running process mid-rotation, any
concurrent request reading a not-yet-re-encrypted row would see a
DecryptError. Stopping the backend → re-encrypting → starting under the
new key removes that window entirely.
"""
from __future__ import annotations

import os
import sys
from base64 import urlsafe_b64encode
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


# Mirror the constants from core.secrets_crypto so this script doesn't
# pick up a partially-initialised module-level Fernet at import time.
_HKDF_SALT = b"jarvis-config-store-v1"
_HKDF_INFO = b"jarvis-settings-secret-encryption"
_TOKEN_PREFIX = "v1:"


def _derive(master: str) -> Fernet:
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_HKDF_SALT,
        info=_HKDF_INFO,
    )
    raw = hkdf.derive(master.encode("utf-8"))
    return Fernet(urlsafe_b64encode(raw))


def main() -> int:
    old = os.environ.get("JARVIS_MASTER_KEY_OLD", "").strip()
    new = os.environ.get("JARVIS_MASTER_KEY_NEW", "").strip()
    if not old or not new:
        print(
            "ERROR: set both JARVIS_MASTER_KEY_OLD and JARVIS_MASTER_KEY_NEW.",
            file=sys.stderr,
        )
        return 2
    if old == new:
        print("ERROR: OLD and NEW keys are identical — nothing to do.", file=sys.stderr)
        return 2

    # Add backend/ to sys.path so 'core' / 'services' import the same way
    # the running backend does.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    from core.database import SessionLocal, SystemConfig

    f_old = _derive(old)
    f_new = _derive(new)

    rotated = 0
    skipped: list[tuple[str, str, str]] = []

    with SessionLocal() as db:
        try:
            rows = db.query(SystemConfig).filter(SystemConfig.is_secret == True).all()  # noqa: E712
            for row in rows:
                if not row.value or not row.value.startswith(_TOKEN_PREFIX):
                    skipped.append((row.category, row.key, "not-a-v1-token"))
                    continue
                payload = row.value[len(_TOKEN_PREFIX):].encode("ascii")
                try:
                    plain = f_old.decrypt(payload).decode("utf-8")
                except InvalidToken:
                    skipped.append((row.category, row.key, "undecryptable-under-old-key"))
                    continue
                new_token = f_new.encrypt(plain.encode("utf-8")).decode("ascii")
                row.value = _TOKEN_PREFIX + new_token
                rotated += 1
            db.commit()
        except Exception:
            db.rollback()
            raise

    print(f"Rotated {rotated} secret(s).")
    if skipped:
        print(f"Skipped {len(skipped)} row(s):")
        for cat, key, reason in skipped:
            print(f"  - {cat}/{key}: {reason}")
    print(
        "Now update JARVIS_MASTER_KEY in your environment and restart the backend."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
