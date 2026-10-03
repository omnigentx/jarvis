"""Short-lived confirmations for authenticated human UI; never exposed to MCP."""

from __future__ import annotations
import base64
import hashlib
import hmac
import json
import secrets
import time
from services.plugins.lifecycle import PluginStateError

REVIEW_TTL = 600


def _key(engine=None) -> bytes:
    """Share the signing identity across workers using the protected runtime DB."""
    from sqlalchemy import text

    if engine is None:
        from core.database import engine
    with engine.begin() as db:
        db.execute(
            text(
                "CREATE TABLE IF NOT EXISTS plugin_review_signing_key (id INTEGER PRIMARY KEY CHECK(id=1), secret TEXT NOT NULL)"
            )
        )
        db.execute(
            text("INSERT OR IGNORE INTO plugin_review_signing_key VALUES (1, :secret)"),
            {"secret": secrets.token_hex(32)},
        )
        return bytes.fromhex(
            db.execute(
                text("SELECT secret FROM plugin_review_signing_key WHERE id=1")
            ).scalar_one()
        )


def issue_review(claims: dict, *, now: float | None = None) -> str:
    payload = {**claims, "expires": (time.time() if now is None else now) + REVIEW_TTL}
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True).encode()
    ).decode()
    signature = hmac.new(_key(), encoded.encode(), hashlib.sha256).hexdigest()
    return encoded + "." + signature


def verify_review(token: str, claims: dict, *, now: float | None = None) -> None:
    try:
        encoded, signature = token.split(".")
        expected = hmac.new(_key(), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        payload = json.loads(base64.urlsafe_b64decode(encoded))
        if payload["expires"] <= (time.time() if now is None else now):
            raise ValueError("expired")
        if any(payload.get(key) != value for key, value in claims.items()):
            raise ValueError("changed")
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise PluginStateError(
            "Review expired or changed; refresh and review again"
        ) from exc
