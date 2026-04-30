"""Unit tests for delta-based token tracking in SpawnProgressBridge."""
import pytest
from unittest.mock import MagicMock, patch


def _make_bridge():
    """Create a SpawnProgressBridge instance for testing."""
    from services.spawn_progress_bridge import SpawnProgressBridge
    pm = MagicMock()
    return SpawnProgressBridge(progress_manager=pm)


def test_first_event_reports_full_values():
    """First token event should report full values as delta (no previous)."""
    bridge = _make_bridge()
    data = {
        "input_tokens": 1000,
        "output_tokens": 500,
        "model": "gpt-4o",
        "cache_hit_tokens": 100,
        "cache_read_tokens": 0,
        "cache_write_tokens": 50,
        "reasoning_tokens": 0,
    }
    raw = {"run_id": "run-1"}

    with patch("services.sse_progress._persist_and_broadcast_token_usage") as mock_persist:
        bridge._handle_token_usage("agent-1", data, raw)

    mock_persist.assert_called_once()
    tokens = mock_persist.call_args[0][2]
    assert tokens["input"] == 1000
    assert tokens["output"] == 500
    assert tokens["total"] == 1500
    assert tokens["cache_hit"] == 100


def test_second_event_reports_only_delta():
    """Second token event should report only the difference from first."""
    bridge = _make_bridge()
    raw = {"run_id": "run-1"}

    # First event
    data1 = {"input_tokens": 1000, "output_tokens": 500, "model": "gpt-4o",
             "cache_hit_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0}
    with patch("services.sse_progress._persist_and_broadcast_token_usage"):
        bridge._handle_token_usage("agent-1", data1, raw)

    # Second event (cumulative from subprocess)
    data2 = {"input_tokens": 2500, "output_tokens": 1200, "model": "gpt-4o",
             "cache_hit_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0}
    with patch("services.sse_progress._persist_and_broadcast_token_usage") as mock_persist:
        bridge._handle_token_usage("agent-1", data2, raw)

    tokens = mock_persist.call_args[0][2]
    assert tokens["input"] == 1500  # 2500 - 1000
    assert tokens["output"] == 700   # 1200 - 500
    assert tokens["total"] == 2200


def test_duplicate_event_skipped():
    """If cumulative values haven't changed, event should be skipped."""
    bridge = _make_bridge()
    raw = {"run_id": "run-1"}

    data = {"input_tokens": 1000, "output_tokens": 500, "model": "gpt-4o",
            "cache_hit_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0}

    with patch("services.sse_progress._persist_and_broadcast_token_usage"):
        bridge._handle_token_usage("agent-1", data, raw)

    # Same event again (duplicate)
    with patch("services.sse_progress._persist_and_broadcast_token_usage") as mock_persist:
        bridge._handle_token_usage("agent-1", data, raw)

    mock_persist.assert_not_called()


def test_different_agents_tracked_separately():
    """Each agent:run_id combination should have its own accumulator."""
    bridge = _make_bridge()
    raw = {"run_id": "run-1"}

    data = {"input_tokens": 1000, "output_tokens": 500, "model": "gpt-4o",
            "cache_hit_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0}

    with patch("services.sse_progress._persist_and_broadcast_token_usage"):
        bridge._handle_token_usage("agent-1", data, raw)

    # Same data for different agent should be full (not delta)
    with patch("services.sse_progress._persist_and_broadcast_token_usage") as mock_persist:
        bridge._handle_token_usage("agent-2", data, raw)

    tokens = mock_persist.call_args[0][2]
    assert tokens["input"] == 1000  # Full, not delta
    assert tokens["output"] == 500


def test_reset_clamp_to_zero():
    """If cumulative drops (process restart), delta should clamp to 0."""
    bridge = _make_bridge()
    raw = {"run_id": "run-1"}

    data1 = {"input_tokens": 5000, "output_tokens": 2000, "model": "gpt-4o",
             "cache_hit_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0}
    with patch("services.sse_progress._persist_and_broadcast_token_usage"):
        bridge._handle_token_usage("agent-1", data1, raw)

    # Cumulative drops (agent restarted with new run_id but same key)
    data2 = {"input_tokens": 100, "output_tokens": 50, "model": "gpt-4o",
             "cache_hit_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0}

    with patch("services.sse_progress._persist_and_broadcast_token_usage") as mock_persist:
        bridge._handle_token_usage("agent-1", data2, raw)

    # With max(0, ...) clamping, delta should be 0 → event skipped
    # Actually: 100 - 5000 = -4900, clamped to 0
    # And 50 - 2000 = -1950, clamped to 0
    # Both delta_input and delta_output are 0, so event is skipped
    mock_persist.assert_not_called()
