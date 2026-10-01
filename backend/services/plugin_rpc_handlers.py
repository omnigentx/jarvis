"""Runtime RPC adapter; no HTTP credentials or human approvals exposed to agents."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from services.plugins.agent_management import activate, add, inventory
from services.plugins.lifecycle import PluginStateError
from services.plugins.package import PackageError
from services.runtime_rpc import RuntimeRpcServer

logger = logging.getLogger(__name__)


async def invoke(
    operation: Callable[..., Awaitable[dict[str, Any]]], params: dict[str, Any]
) -> dict[str, Any]:
    try:
        return await operation(**params)
    except PermissionError as exc:
        return {"error": str(exc), "status": 403}
    except PackageError as exc:
        return {"error": str(exc), "status": 422}
    except PluginStateError as exc:
        return {"error": str(exc), "status": 409}
    except TimeoutError:
        return {
            "error": "Plugin operation timed out; verify state before retrying",
            "status": 504,
        }
    except httpx.HTTPError:
        return {"error": "Plugin source could not be retrieved", "status": 502}
    except Exception:
        logger.exception("[plugins] Agent operation failed")
        return {"error": "Plugin operation failed", "status": 500}


async def list_plugins(**params: Any) -> dict[str, Any]:
    return await invoke(inventory, params)


async def add_plugin(**params: Any) -> dict[str, Any]:
    return await invoke(add, params)


async def activate_plugin(**params: Any) -> dict[str, Any]:
    return await invoke(activate, params)


def register(server: RuntimeRpcServer) -> None:
    server.register("plugin.list", list_plugins)
    server.register("plugin.add", add_plugin, timeout=240)
    server.register("plugin.activate", activate_plugin, timeout=90)
