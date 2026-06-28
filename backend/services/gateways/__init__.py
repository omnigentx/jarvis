"""External messaging gateways (Telegram, Zalo, …).

See ``README.md`` in this package for the architecture and how to add a
platform. Public surface:

  * :class:`~services.gateways.manager.GatewayManager` — lifecycle + dispatch.
  * :class:`~services.gateways.base.BaseGateway` — implement to add a platform.
  * :data:`~services.gateways.registry.GATEWAY_REGISTRY` — register it.
"""
from .base import BaseGateway, InboundMessage
from .manager import GatewayManager
from .registry import GATEWAY_REGISTRY

__all__ = ["BaseGateway", "InboundMessage", "GatewayManager", "GATEWAY_REGISTRY"]
