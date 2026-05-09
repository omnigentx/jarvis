"""RPC handlers for approval-server — registered with RuntimeRpcServer.

Replaces the old HTTP+Bearer round-trip from ``tools/approval_server.py``
with direct in-process calls to :mod:`approval_service`. Trust model is
file-system permissions on the UDS socket (same as ``skill_rpc_handlers``
and ``mcp_rpc_handlers``), so no API key is involved.

Methods:

* ``approval.create`` — req/resp; ~30 s default timeout. Creates the
  approval row, pauses team agents, broadcasts SSE.
* ``approval.get`` — req/resp; fetch detail incl. comments.
* ``approval.wait`` — long-poll; **timeout=None** because human approval
  takes minutes to days. Subscribes to the in-process pub/sub in
  :mod:`approval_service` and unblocks when the user resolves via the
  dashboard.
"""
from __future__ import annotations

import logging
from typing import Any

from services.approval_service import approval_service
from services.runtime_rpc import RuntimeRpcServer

logger = logging.getLogger("approval_rpc")


def _approval_create(**params: Any) -> dict:
    """Wrapper around ``approval_service.create_approval`` that returns
    the new approval dict. Accepts the same ``data`` shape the HTTP
    route accepted, so the MCP tool can forward its kwargs verbatim.
    """
    return approval_service.create_approval(params)


def _approval_get(*, approval_id: str) -> dict:
    """Fetch full approval detail incl. inline comments. Returns an
    error envelope if not found — matches the convention used by
    ``runtime_rpc_client.call`` so MCP tools can surface 404 to the LLM
    without a Python exception.
    """
    record = approval_service.get_approval(approval_id)
    if record is None:
        return {"error": f"Approval {approval_id} not found", "status": 404}
    return record


async def _approval_wait(*, approval_id: str) -> dict:
    """Block until approval reaches a terminal state.

    Long-poll: registered with ``timeout=None`` so the dispatcher does
    not enforce the default 30 s deadline. Connection close on the
    client side cancels the awaited future via the surrounding RPC
    server loop.
    """
    try:
        return await approval_service.wait_for_resolution(approval_id)
    except KeyError:
        return {"error": f"Approval {approval_id} not found", "status": 404}


_METHODS: dict[str, tuple[Any, Any]] = {
    # method-name → (handler, timeout-override)
    "approval.create": (_approval_create, None),  # default 30 s
    "approval.get": (_approval_get, None),  # default 30 s
    "approval.wait": (_approval_wait, "unbounded"),  # opt out of deadline
}


def register(server: RuntimeRpcServer) -> None:
    """Register all approval methods on the given RPC server. Called at
    backend boot from ``server.py`` lifespan, next to
    ``skill_rpc_handlers.register`` and ``mcp_rpc_handlers.register``.
    """
    from services.runtime_rpc import DEFAULT_HANDLER_TIMEOUT

    for name, (handler, override) in _METHODS.items():
        if override == "unbounded":
            server.register(name, handler, timeout=None)
        else:
            server.register(name, handler, timeout=DEFAULT_HANDLER_TIMEOUT)
    logger.info("[approval_rpc] registered %d approval methods", len(_METHODS))
