"""Jarvis catalog mutations require content-bound user review."""
from __future__ import annotations

import pytest

from services import approval_gate
from services import mcp_rpc_handlers as rpc


@pytest.mark.asyncio
async def test_catalog_review_redacts_env_but_binds_full_payload(monkeypatch):
    requests = []

    def rejected(**kwargs):
        requests.append(kwargs["content_md"])
        return False, "user rejected"

    monkeypatch.setattr(approval_gate, "request_approval", rejected)
    first = await rpc._approve_catalog_change(
        "custom", "create", {"command": "python", "env": {"TOKEN": "alpha"}}
    )
    second = await rpc._approve_catalog_change(
        "custom", "create", {"command": "python", "env": {"TOKEN": "beta"}}
    )
    assert first["status"] == second["status"] == 403
    assert "alpha" not in requests[0] and "beta" not in requests[1]
    assert requests[0] != requests[1]
