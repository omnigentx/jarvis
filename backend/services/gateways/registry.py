"""Registry of available gateway platforms.

Adding a platform = implement a :class:`~services.gateways.base.BaseGateway`
subclass and add one line here. The manager and config loader discover it
automatically — nothing else changes.
"""
from __future__ import annotations

from typing import Dict, Type

from .base import BaseGateway
from .telegram import TelegramGateway
from .zalo import ZaloGateway

GATEWAY_REGISTRY: Dict[str, Type[BaseGateway]] = {
    "telegram": TelegramGateway,
    "zalo": ZaloGateway,
}
