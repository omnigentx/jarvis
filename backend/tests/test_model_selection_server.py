"""Transport failures must return actionable MCP results to the agent."""

from tools import model_selection_server as server


def test_model_tools_report_midcall_socket_disconnect(monkeypatch):
    monkeypatch.setattr(server, "caller_from_ctx", lambda _ctx: "Jarvis")

    def disconnected(_method, _params):
        raise ConnectionResetError("socket disconnected")

    monkeypatch.setattr(server, "rpc_call", disconnected)
    assert "socket disconnected" in server.model_get()["error"]
    assert "socket disconnected" in server.model_set("openai.coding-agent", 0)["error"]
