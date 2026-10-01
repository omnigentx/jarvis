"""Regression contract: isolated runner emits per-call values, not cumulative."""

from unittest.mock import MagicMock, patch
from services.spawn_progress_bridge import SpawnProgressBridge


def test_equal_usage_values_can_be_two_distinct_calls():
    bridge = SpawnProgressBridge(progress_manager=MagicMock())
    data = {"input_tokens": 1000, "output_tokens": 50, "model": "test"}
    captured = []
    with patch(
        "services.sse_progress._persist_and_broadcast_token_usage",
        side_effect=lambda *args: captured.append(args[2]),
    ):
        for stamp in (1, 2):
            bridge._handle_token_usage("Dev", data, {"run_id": "r", "timestamp": stamp})
    assert sum(t["input"] for t in captured) == 2000
    assert captured[0]["source_event_id"] != captured[1]["source_event_id"]


def test_smaller_next_call_does_not_disappear():
    bridge = SpawnProgressBridge(progress_manager=MagicMock())
    captured = []
    with patch(
        "services.sse_progress._persist_and_broadcast_token_usage",
        side_effect=lambda *args: captured.append(args[2]),
    ):
        for i in (1000, 500):
            bridge._handle_token_usage(
                "Dev",
                {"input_tokens": i, "output_tokens": 10, "model": "test"},
                {"run_id": "r", "timestamp": i},
            )
    assert sum(t["input"] for t in captured) == 1500
    assert sum(t["output"] for t in captured) == 20


def test_cumulative_format_is_rejected(caplog):
    bridge = SpawnProgressBridge(progress_manager=MagicMock())
    with patch("services.sse_progress._persist_and_broadcast_token_usage") as persist:
        bridge._handle_token_usage(
            "Dev",
            {"usage_kind": "cumulative", "input_tokens": 1000},
            {"run_id": "r", "timestamp": 1},
        )
    persist.assert_not_called()
    assert "Unsupported" in caplog.text
