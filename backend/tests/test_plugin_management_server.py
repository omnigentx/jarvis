"""MCP schema never exposes caller selection; transport identity is required."""

from types import SimpleNamespace
from unittest.mock import Mock
import inspect
from tools import plugin_management_server as server


def test_missing_caller_cannot_request_any_plugin_operation(monkeypatch):
    monkeypatch.setattr(server, "caller_from_ctx", lambda _: "")
    rpc = Mock()
    monkeypatch.setattr(server, "rpc_call", rpc)
    assert server.plugin_list().structuredContent["status"] == 403
    assert server.plugin_add("openai/plugins", "a" * 40).structuredContent["status"] == 403
    assert server.plugin_activate("candidate").structuredContent["status"] == 403
    rpc.assert_not_called()


def test_identity_is_stamped_and_not_part_of_tool_arguments(monkeypatch):
    ctx = SimpleNamespace(request_context=SimpleNamespace(meta={"caller_agent": "PM"}))
    monkeypatch.setenv("TEAM_SESSION_ID", "team")
    rpc = Mock(return_value={"status": "needs_approval"})
    monkeypatch.setattr(server, "rpc_call", rpc)
    result = server.plugin_add("openai/plugins", "a" * 40, target_agent="Dev", ctx=ctx)
    assert result.structuredContent["status"] == "needs_approval"
    args = rpc.call_args.args
    assert args[0] == "plugin.add"
    assert args[1]["caller_agent"] == "PM"
    assert args[1]["session_id"] == "team"
    assert "caller_agent" not in inspect.signature(server.plugin_add).parameters


def test_disconnect_is_actionable_not_ready(monkeypatch):
    monkeypatch.setattr(server, "caller_from_ctx", lambda _: "Jarvis")

    def disconnect(*_, **__):
        raise ConnectionResetError("disconnected")

    monkeypatch.setattr(server, "rpc_call", disconnect)
    result = server.plugin_activate("candidate")
    assert result.structuredContent["status"] == 503
    assert "disconnected" in result.structuredContent["error"]
