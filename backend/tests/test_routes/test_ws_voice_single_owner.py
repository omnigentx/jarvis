"""Regression coverage for the single-owner voice STT session."""

import json
import threading
from unittest.mock import MagicMock

from starlette.websockets import WebSocketDisconnect

from tests.test_routes.test_ws_voice import ws_client


def test_busy_socket_does_not_disturb_owner_and_owner_can_reconnect(ws_client):
    client, stt = ws_client
    stt.resume = MagicMock()
    stt.pause = MagicMock()

    with client.websocket_connect("/ws/voice") as owner:
        owner_hook = stt._hook
        with client.websocket_connect("/ws/voice") as rejected:
            error = json.loads(rejected.receive_text())
            assert error["type"] == "error"
            assert "active" in error["detail"]
            try:
                rejected.receive_text()
            except WebSocketDisconnect as exc:
                assert exc.code == 1013
            else:
                raise AssertionError("busy socket did not close")

        assert stt._hook is owner_hook
        assert stt.pause.call_count == 0
        owner.send_bytes(b"\x00\x00")
        owner.close()

    assert stt.fed == [b"\x00\x00"]
    with client.websocket_connect("/ws/voice") as next_owner:
        assert stt._hook is not None
        next_owner.close()


def test_lease_is_held_until_owner_cleanup_finishes(ws_client):
    client, stt = ws_client
    cleanup_started = threading.Event()
    allow_cleanup = threading.Event()
    stt.resume = MagicMock()

    def slow_pause():
        cleanup_started.set()
        assert allow_cleanup.wait(3), "test did not release cleanup"

    stt.pause = MagicMock(side_effect=slow_pause)
    try:
        with client.websocket_connect("/ws/voice") as owner:
            owner.close()
            assert cleanup_started.wait(2)
            with client.websocket_connect("/ws/voice") as rejected:
                assert json.loads(rejected.receive_text())["type"] == "error"
                rejected.close()
    finally:
        allow_cleanup.set()


def test_stt_hook_startup_failure_reports_error_then_allows_reconnect(
    ws_client, monkeypatch
):
    client, stt = ws_client
    original_set_hook = stt.set_hook
    failed = False

    def flaky_set_hook(hook):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("synthetic STT hook failure")
        return original_set_hook(hook)

    monkeypatch.setattr(stt, "set_hook", flaky_set_hook)
    with client.websocket_connect("/ws/voice") as failed_socket:
        error = json.loads(failed_socket.receive_text())
        assert error["type"] == "error"
        assert "STT" in error["detail"]

    with client.websocket_connect("/ws/voice") as next_owner:
        assert stt._hook is not None
        next_owner.close()


def test_stt_factory_failure_reports_error_then_allows_reconnect(
    ws_client, monkeypatch
):
    from services import runtime_config, shared_state

    client, stt = ws_client
    monkeypatch.setattr(shared_state, "stt_recorder", None)

    def fail(_):
        raise RuntimeError("synthetic STT factory failure")

    monkeypatch.setattr(runtime_config, "apply_voice_stt_config", fail)
    with client.websocket_connect("/ws/voice") as failed_socket:
        events = []
        try:
            for _ in range(3):
                events.append(json.loads(failed_socket.receive_text()))
        except WebSocketDisconnect:
            pass
        assert any(event.get("type") == "error" for event in events), events

    monkeypatch.setattr(shared_state, "stt_recorder", stt)
    with client.websocket_connect("/ws/voice") as next_owner:
        assert stt._hook is not None
        next_owner.close()
