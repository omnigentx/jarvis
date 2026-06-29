"""Gateway configuration — stored in ``config_service`` (the DB, single source).

Config lives in the ``gateways`` category so it is editable from the Settings UI
with no file editing and no restart: a write through ``/api/settings`` fires a
``config_service`` change event, and :class:`GatewayManager` live-reloads
(see ``manager.py``). Tokens are stored encrypted (``is_secret=True``).

Per-platform keys (``<platform>`` is ``telegram`` / ``zalo`` / …):
  ``<platform>_enabled``    "true" / "false"
  ``<platform>_token``      bot token (secret)
  ``<platform>_allow_from`` JSON array of user ids ("[]" = deny all, "[\"*\"]" = all)
  ``<platform>_agent``      agent that answers (defaults to "Jarvis")
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List

logger = logging.getLogger("gateways")


@dataclass(slots=True)
class GatewayConfig:
    enabled: bool = False
    token: str = ""
    allow_from: List = field(default_factory=list)
    agent: str = "Jarvis"


def _parse_allow_from(raw: str | None) -> List:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        logger.warning("Invalid allow_from JSON %r; treating as empty (deny all)", raw)
        return []


def load_gateway_configs() -> Dict[str, GatewayConfig]:
    """Return ``{platform: GatewayConfig}`` for every registered platform.

    Reads from ``config_service``. A platform with no rows yet comes back as a
    disabled default — gateways are entirely opt-in.
    """
    from services.config_service import config_service
    from services.secret_utils import safe_get_or_none

    from .registry import GATEWAY_REGISTRY

    out: Dict[str, GatewayConfig] = {}
    for name in GATEWAY_REGISTRY:
        enabled = (config_service.get("gateways", f"{name}_enabled") or "false").strip().lower() == "true"
        # Token is a secret — soft-fail on a stale ciphertext so one corrupt row
        # can't crash gateway startup (logged, not swallowed).
        token = safe_get_or_none(
            config_service, "gateways", f"{name}_token",
            on_warn=lambda e, n=name: logger.warning("Gateway '%s' token unreadable: %s", n, e),
        ) or ""
        allow_from = _parse_allow_from(config_service.get("gateways", f"{name}_allow_from"))
        agent = config_service.get("gateways", f"{name}_agent") or "Jarvis"
        out[name] = GatewayConfig(
            enabled=enabled,
            token=token.strip(),
            allow_from=allow_from,
            agent=agent,
        )
    return out
