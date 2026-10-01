"""Exercise actual MCP serialization, not only Python return values."""

import json
from unittest.mock import Mock
import pytest
from mcp.types import CallToolResult
from tools import plugin_management_server as server


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,expected",
    [
        (
            {
                "error": "Only the team orchestrator may manage a subordinate",
                "status": 403,
            },
            True,
        ),
        ({"error": "bridge unavailable", "status": 503}, True),
        ({"status": "needs_approval"}, False),
        ({"status": "ready", "skills": ["example"]}, False),
    ],
)
async def test_wire_result_error_semantics(monkeypatch, payload, expected):
    monkeypatch.setattr(server, "caller_from_ctx", lambda _: "Dev")
    monkeypatch.setattr(server, "rpc_call", Mock(return_value=payload))
    result = await server.mcp.call_tool("plugin_activate", {"identity": "example"})
    if not isinstance(result, CallToolResult):
        result = CallToolResult(content=result)
    assert result.isError is expected
    assert json.loads(result.content[0].text) == payload
