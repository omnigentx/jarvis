"""Unit tests for meeting room event-driven transcript push.

Verifies that _notify_turn_agent embeds unread transcript and
advances read cursors, and that auto-join works correctly.
"""
import json
import pytest
from unittest.mock import MagicMock, patch, AsyncMock


# ── Test auto-join via create_meeting ──

@pytest.mark.asyncio
async def test_create_meeting_auto_joins_all():
    """create_meeting should auto-join all participants (not just creator)."""
    mock_storage = MagicMock()
    mock_storage.create_meeting = MagicMock()

    with patch("fast_agent.spawn.servers.meeting_room_server._storage", mock_storage):
        with patch("fast_agent.spawn.servers.meeting_room_server._get_my_name", return_value="PM"):
            with patch("fast_agent.spawn.servers.meeting_room_server._get_bus") as mock_bus:
                mock_bus.return_value = MagicMock()
                with patch("fast_agent.spawn.servers.meeting_room_server._auto_wake_if_idle"):
                    with patch("fast_agent.spawn.servers.meeting_room_server._fire_hook"):
                        from fast_agent.spawn.servers.meeting_room_server import create_meeting
                        result = await create_meeting(
                            agenda="Test meeting",
                            participants="PM, Dev, QE",
                            max_rounds=2,
                        )

    result_data = json.loads(result)
    assert result_data["status"] == "started"  # Not "invites_sent"
    assert "PM" in result_data["participants"]
    assert "Dev" in result_data["participants"]
    assert "QE" in result_data["participants"]

    # Verify storage.create_meeting was called with all joined
    call_args = mock_storage.create_meeting.call_args
    _, initial_state = call_args[0][1], call_args[0][2]
    assert set(initial_state["joined"]) == {"PM", "Dev", "QE"}
    assert initial_state["started"] is True


@pytest.mark.asyncio
async def test_create_meeting_auto_includes_creator():
    """Creator (my_name) is the chair — auto-prepended to participants."""
    mock_storage = MagicMock()
    mock_storage.create_meeting = MagicMock()

    with patch("fast_agent.spawn.servers.meeting_room_server._storage", mock_storage):
        with patch("fast_agent.spawn.servers.meeting_room_server._get_my_name", return_value="Cameron [PM]"):
            with patch("fast_agent.spawn.servers.meeting_room_server._get_bus") as mock_bus:
                mock_bus.return_value = MagicMock()
                with patch("fast_agent.spawn.servers.meeting_room_server._auto_wake_if_idle"):
                    with patch("fast_agent.spawn.servers.meeting_room_server._fire_hook"):
                        from fast_agent.spawn.servers.meeting_room_server import create_meeting
                        # Creator passes only OTHERS — should still be added at index 0
                        result = await create_meeting(
                            agenda="Sprint kickoff",
                            participants="Devon [BA], Reese [Dev], Devon [QE]",
                        )

    result_data = json.loads(result)
    parts = result_data["participants"]
    assert parts[0] == "Cameron [PM]", f"Creator must speak first, got order: {parts}"
    assert parts == ["Cameron [PM]", "Devon [BA]", "Reese [Dev]", "Devon [QE]"]

    # Storage state must reflect the same ordered list with creator joined.
    # Post-B1: participants live in state_json (mutable bucket), config_json
    # only carries write-once setup (agenda, created_by, created_at).
    call_args = mock_storage.create_meeting.call_args
    stored_config, initial_state = call_args[0][1], call_args[0][2]
    assert "participants" not in stored_config, (
        "participants must NOT live in config_json (write-once bucket)"
    )
    assert initial_state["participants"][0] == "Cameron [PM]"
    assert "Cameron [PM]" in initial_state["joined"]


@pytest.mark.asyncio
async def test_create_meeting_does_not_duplicate_creator():
    """If creator is already in participants, don't add a duplicate."""
    mock_storage = MagicMock()
    mock_storage.create_meeting = MagicMock()

    with patch("fast_agent.spawn.servers.meeting_room_server._storage", mock_storage):
        with patch("fast_agent.spawn.servers.meeting_room_server._get_my_name", return_value="PM"):
            with patch("fast_agent.spawn.servers.meeting_room_server._get_bus") as mock_bus:
                mock_bus.return_value = MagicMock()
                with patch("fast_agent.spawn.servers.meeting_room_server._auto_wake_if_idle"):
                    with patch("fast_agent.spawn.servers.meeting_room_server._fire_hook"):
                        from fast_agent.spawn.servers.meeting_room_server import create_meeting
                        # Creator explicitly listed mid-list — preserve given position
                        result = await create_meeting(
                            agenda="Test",
                            participants="BA, PM, Dev",
                        )

    parts = json.loads(result)["participants"]
    assert parts.count("PM") == 1
    assert parts == ["BA", "PM", "Dev"], f"Existing position preserved, got: {parts}"


@pytest.mark.asyncio
async def test_create_meeting_truncates_long_agenda():
    """Agenda > 120 chars is truncated + warning surfaced.

    Guards the b61af7db incident: PM stuffed a 50-line markdown brief
    into ``agenda``, breaking the dashboard title clamp.
    """
    long_agenda = (
        "## Project Context\n**Epic:** JXCC-19 - Tools & MCP Servers\n"
        "## Objective\nComprehensive audit of all tools and MCP servers\n"
        "## Scope — extensive list of every tool category goes here, "
        "way past the 120 char limit on purpose to verify truncation"
    )
    assert len(long_agenda) > 120

    mock_storage = MagicMock()
    mock_storage.create_meeting = MagicMock()

    with patch("fast_agent.spawn.servers.meeting_room_server._storage", mock_storage):
        with patch("fast_agent.spawn.servers.meeting_room_server._get_my_name", return_value="PM"):
            with patch("fast_agent.spawn.servers.meeting_room_server._get_bus") as mock_bus:
                mock_bus.return_value = MagicMock()
                with patch("fast_agent.spawn.servers.meeting_room_server._auto_wake_if_idle"):
                    with patch("fast_agent.spawn.servers.meeting_room_server._fire_hook"):
                        from fast_agent.spawn.servers.meeting_room_server import create_meeting
                        result = await create_meeting(
                            agenda=long_agenda,
                            participants="BA, Dev",
                            description="Long-form context belongs HERE, not in agenda.",
                        )

    payload = json.loads(result)
    # Truncated agenda preserved in result + warning explains why.
    assert len(payload["agenda"]) <= 121, (  # 120 + ellipsis "…"
        f"Agenda must be truncated to ≤120 chars (+ ellipsis), got {len(payload['agenda'])}"
    )
    assert payload["agenda"].endswith("…"), "Truncation must use ellipsis marker"
    assert "warning" in payload
    assert "agenda truncated" in payload["warning"]
    assert "description" in payload["warning"], (
        "Warning must hint at the description param so the LLM corrects itself"
    )

    # Storage saw the truncated agenda; description preserved separately.
    stored_config = mock_storage.create_meeting.call_args[0][1]
    assert len(stored_config["agenda"]) <= 121
    assert stored_config["description"] == "Long-form context belongs HERE, not in agenda."


@pytest.mark.asyncio
async def test_create_meeting_short_agenda_no_warning():
    """Short agenda passes through unchanged, no warning field set."""
    mock_storage = MagicMock()
    mock_storage.create_meeting = MagicMock()

    with patch("fast_agent.spawn.servers.meeting_room_server._storage", mock_storage):
        with patch("fast_agent.spawn.servers.meeting_room_server._get_my_name", return_value="PM"):
            with patch("fast_agent.spawn.servers.meeting_room_server._get_bus") as mock_bus:
                mock_bus.return_value = MagicMock()
                with patch("fast_agent.spawn.servers.meeting_room_server._auto_wake_if_idle"):
                    with patch("fast_agent.spawn.servers.meeting_room_server._fire_hook"):
                        from fast_agent.spawn.servers.meeting_room_server import create_meeting
                        result = await create_meeting(
                            agenda="Sprint 1 kickoff",
                            participants="BA, Dev",
                        )

    payload = json.loads(result)
    assert payload["agenda"] == "Sprint 1 kickoff"
    assert "warning" not in payload
    assert "…" not in payload["agenda"]


@pytest.mark.asyncio
async def test_create_meeting_description_persisted():
    """Optional ``description`` parameter lands in config_json."""
    mock_storage = MagicMock()
    mock_storage.create_meeting = MagicMock()

    with patch("fast_agent.spawn.servers.meeting_room_server._storage", mock_storage):
        with patch("fast_agent.spawn.servers.meeting_room_server._get_my_name", return_value="PM"):
            with patch("fast_agent.spawn.servers.meeting_room_server._get_bus") as mock_bus:
                mock_bus.return_value = MagicMock()
                with patch("fast_agent.spawn.servers.meeting_room_server._auto_wake_if_idle"):
                    with patch("fast_agent.spawn.servers.meeting_room_server._fire_hook"):
                        from fast_agent.spawn.servers.meeting_room_server import create_meeting
                        await create_meeting(
                            agenda="Audit",
                            participants="BA, Dev",
                            description="Full project brief with markdown links etc.",
                        )

    stored_config = mock_storage.create_meeting.call_args[0][1]
    assert stored_config["description"] == "Full project brief with markdown links etc."


@pytest.mark.asyncio
async def test_create_meeting_solo_creator_rejected():
    """Need at least 2 participants — creator alone is not enough."""
    mock_storage = MagicMock()
    mock_storage.create_meeting = MagicMock()

    with patch("fast_agent.spawn.servers.meeting_room_server._storage", mock_storage):
        with patch("fast_agent.spawn.servers.meeting_room_server._get_my_name", return_value="PM"):
            with patch("fast_agent.spawn.servers.meeting_room_server._get_bus") as mock_bus:
                mock_bus.return_value = MagicMock()
                with patch("fast_agent.spawn.servers.meeting_room_server._auto_wake_if_idle"):
                    with patch("fast_agent.spawn.servers.meeting_room_server._fire_hook"):
                        from fast_agent.spawn.servers.meeting_room_server import create_meeting
                        result = await create_meeting(
                            agenda="Test",
                            participants="",  # No others — only creator
                        )

    assert "error" in json.loads(result)
    mock_storage.create_meeting.assert_not_called()


# ── Test short meeting IDs ──

@pytest.mark.asyncio
async def test_meeting_id_is_short():
    """Meeting IDs should be 8-char hex (not mtg_ prefix)."""
    mock_storage = MagicMock()
    mock_storage.create_meeting = MagicMock()

    with patch("fast_agent.spawn.servers.meeting_room_server._storage", mock_storage):
        with patch("fast_agent.spawn.servers.meeting_room_server._get_my_name", return_value="PM"):
            with patch("fast_agent.spawn.servers.meeting_room_server._get_bus") as mock_bus:
                mock_bus.return_value = MagicMock()
                with patch("fast_agent.spawn.servers.meeting_room_server._auto_wake_if_idle"):
                    with patch("fast_agent.spawn.servers.meeting_room_server._fire_hook"):
                        from fast_agent.spawn.servers.meeting_room_server import create_meeting
                        result = await create_meeting(
                            agenda="ID test",
                            participants="A, B",
                        )

    result_data = json.loads(result)
    meeting_id = result_data["meeting_id"]
    assert len(meeting_id) == 8
    assert not meeting_id.startswith("mtg_")


# ── Test transcript embedding in notifications ──

def test_notify_turn_embeds_transcript():
    """_notify_turn_agent should embed unread transcript in notifications."""
    mock_bus = MagicMock()
    mock_storage = MagicMock()

    transcript = [
        {"agent": "PM", "round": 1, "message": "Let's start"},
        {"agent": "Dev", "round": 1, "message": "Ready"},
    ]
    mock_storage.get_transcript.return_value = transcript
    mock_storage.get_state.return_value = {"read_cursors": {"QE": 0}}
    mock_storage.acquire_lock.return_value.__enter__ = MagicMock(return_value=MagicMock())
    mock_storage.acquire_lock.return_value.__exit__ = MagicMock(return_value=False)

    with patch("fast_agent.spawn.servers.meeting_room_server._storage", mock_storage):
        with patch("fast_agent.spawn.servers.meeting_room_server._get_bus", return_value=mock_bus):
            with patch("fast_agent.spawn.servers.meeting_room_server._auto_wake_if_idle"):
                from fast_agent.spawn.servers.meeting_room_server import _notify_turn_agent
                _notify_turn_agent("abc12345", "QE", "Sprint review", 1)

    # Verify bus.send was called with transcript embedded
    mock_bus.send.assert_called_once()
    msg_content = mock_bus.send.call_args[1]["content"]
    assert "Meeting [abc12345]" in msg_content
    assert "[PM]" in msg_content
    assert "Let's start" in msg_content
    assert "[Dev]" in msg_content
    assert "Ready" in msg_content
    assert "2 new messages" in msg_content


def test_notify_turn_advances_cursor():
    """_notify_turn_agent should advance read cursor for the agent."""
    mock_bus = MagicMock()
    mock_storage = MagicMock()

    transcript = [
        {"agent": "PM", "round": 1, "message": "msg1"},
        {"agent": "Dev", "round": 1, "message": "msg2"},
        {"agent": "PM", "round": 2, "message": "msg3"},
    ]
    mock_storage.get_transcript.return_value = transcript

    state = {"read_cursors": {"QE": 1}}  # QE has read 1 message
    mock_storage.get_state.return_value = state
    mock_conn = MagicMock()
    mock_storage.acquire_lock.return_value.__enter__ = MagicMock(return_value=mock_conn)
    mock_storage.acquire_lock.return_value.__exit__ = MagicMock(return_value=False)

    with patch("fast_agent.spawn.servers.meeting_room_server._storage", mock_storage):
        with patch("fast_agent.spawn.servers.meeting_room_server._get_bus", return_value=mock_bus):
            with patch("fast_agent.spawn.servers.meeting_room_server._auto_wake_if_idle"):
                from fast_agent.spawn.servers.meeting_room_server import _notify_turn_agent
                _notify_turn_agent("abc12345", "QE", "Test", 2)

    # Verify cursor was advanced to len(transcript) = 3
    update_call = mock_storage.update_state.call_args
    updated_state = update_call[0][1]
    assert updated_state["read_cursors"]["QE"] == 3


def test_notify_turn_only_unread():
    """If agent has already read all messages, notification shows 'no new messages'."""
    mock_bus = MagicMock()
    mock_storage = MagicMock()

    transcript = [{"agent": "PM", "round": 1, "message": "old"}]
    mock_storage.get_transcript.return_value = transcript
    mock_storage.get_state.return_value = {"read_cursors": {"QE": 1}}  # Already read all
    mock_storage.acquire_lock.return_value.__enter__ = MagicMock(return_value=MagicMock())
    mock_storage.acquire_lock.return_value.__exit__ = MagicMock(return_value=False)

    with patch("fast_agent.spawn.servers.meeting_room_server._storage", mock_storage):
        with patch("fast_agent.spawn.servers.meeting_room_server._get_bus", return_value=mock_bus):
            with patch("fast_agent.spawn.servers.meeting_room_server._auto_wake_if_idle"):
                from fast_agent.spawn.servers.meeting_room_server import _notify_turn_agent
                _notify_turn_agent("abc12345", "QE", "Test", 2)

    msg_content = mock_bus.send.call_args[1]["content"]
    assert "0 new messages" in msg_content
    assert "no new messages" in msg_content


# ── Test meeting-ended notifications ──

def test_notify_meeting_ended_includes_transcript():
    """_notify_meeting_ended should include full transcript."""
    mock_bus = MagicMock()
    mock_storage = MagicMock()
    mock_storage.get_transcript.return_value = [
        {"agent": "PM", "round": 1, "message": "Done"},
    ]

    with patch("fast_agent.spawn.servers.meeting_room_server._storage", mock_storage):
        with patch("fast_agent.spawn.servers.meeting_room_server._get_bus", return_value=mock_bus):
            with patch("fast_agent.spawn.servers.meeting_room_server._auto_wake_if_idle"):
                from fast_agent.spawn.servers.meeting_room_server import _notify_meeting_ended
                _notify_meeting_ended("abc12345", "Dev", "Sprint review")

    msg_content = mock_bus.send.call_args[1]["content"]
    assert "Meeting [abc12345] has ended" in msg_content
    assert "[PM]" in msg_content
    assert "Done" in msg_content


# ── Test removed tools ──

def test_join_meeting_not_available():
    """join_meeting should no longer be importable as a tool."""
    import fast_agent.spawn.servers.meeting_room_server as mod
    assert not hasattr(mod, "join_meeting") or not callable(getattr(mod, "join_meeting", None))


def test_wait_for_my_turn_not_available():
    """wait_for_my_turn should no longer be importable as a tool."""
    import fast_agent.spawn.servers.meeting_room_server as mod
    assert not hasattr(mod, "wait_for_my_turn") or not callable(getattr(mod, "wait_for_my_turn", None))


def test_get_transcript_not_available():
    """get_transcript should no longer be importable as a tool."""
    import fast_agent.spawn.servers.meeting_room_server as mod
    assert not hasattr(mod, "get_transcript") or not callable(getattr(mod, "get_transcript", None))


# ── Impersonation refusal in speak() / skip_turn() ──
#
# Production incident 2026-05-20 (agile-team_ccd1adb9): Taylor [PM]
# force-skipped 6 teammates in a single round by calling
# ``skip_turn(agent_name="<teammate>", reason="PM force-advancing...")``
# six times. The MCP server only validated "is agent_name the current
# speaker?" — not "is the caller actually that agent". The transcript
# falsely attributed identical placeholder responses to BA / SA / Dev /
# Designer / QE / DSO, breaking the audit value of the meeting record.
#
# Fix: _assert_self_identity() pins ``agent_name`` to the caller's
# spawn-time TEAM_MY_NAME (read via _get_my_name). A mismatch returns
# an error JSON instead of writing to the transcript.


def _identity_check(caller_env_name: str, param_agent_name: str):
    """Drive _assert_self_identity directly with the env name patched in."""
    with patch(
        "fast_agent.spawn.servers.meeting_room_server._get_my_name",
        return_value=caller_env_name,
    ):
        from fast_agent.spawn.servers.meeting_room_server import _assert_self_identity
        return _assert_self_identity(param_agent_name)


def test_speak_refuses_impersonation_via_self_identity_check():
    """Caller env=Taylor [PM] passes agent_name=Sawyer [BA] → REJECT."""
    resolved, err = _identity_check("Taylor [PM]", "Sawyer [BA]")
    assert resolved == ""
    assert err is not None
    err_data = json.loads(err)
    assert "Impersonation refused" in err_data["error"]
    assert err_data["caller"] == "Taylor [PM]"
    assert err_data["claimed_agent_name"] == "Sawyer [BA]"


def test_skip_turn_refuses_impersonation_via_self_identity_check():
    """Same contract for skip_turn — both share _assert_self_identity."""
    resolved, err = _identity_check("Taylor [PM]", "Reagan [SA]")
    assert resolved == ""
    assert err is not None
    assert json.loads(err)["error"].startswith("Impersonation refused")


def test_identity_check_allows_self_call_with_matching_agent_name():
    """Passing your own name explicitly is fine (no impersonation)."""
    resolved, err = _identity_check("Taylor [PM]", "Taylor [PM]")
    assert err is None
    assert resolved == "Taylor [PM]"


def test_identity_check_is_case_insensitive_and_strips_whitespace():
    """Match is case-insensitive and trims surrounding whitespace —
    the LLM occasionally lowercases or pads role tags."""
    resolved, err = _identity_check("Taylor [PM]", "  taylor [pm]  ")
    assert err is None
    assert resolved == "  taylor [pm]  "  # caller's spelling preserved, just normalized for comparison


def test_identity_check_auto_detects_when_param_empty():
    """Empty agent_name param → falls back to caller's TEAM_MY_NAME.
    Preserves the legacy convenience contract."""
    resolved, err = _identity_check("Taylor [PM]", "")
    assert err is None
    assert resolved == "Taylor [PM]"


def test_identity_check_treats_agent_sentinel_as_no_identity():
    """When TEAM_MY_NAME/TEAM_MY_ROLE both unset, _get_my_name returns
    the literal ``"agent"`` sentinel — that's not a real identity, so
    we don't have anything to compare against. Allow the param-supplied
    name through (no impersonation possible without a real caller)."""
    resolved, err = _identity_check("agent", "Sawyer [BA]")
    assert err is None
    assert resolved == "Sawyer [BA]"


def test_identity_check_pm_force_skip_pattern_all_six_blocked():
    """End-to-end replay of the 2026-05-20 incident: PM tries to skip
    all 6 teammates' turns. Every one must be refused."""
    pm_env = "Taylor [PM]"
    teammates = [
        "Sawyer [BA]",
        "Reagan [SA]",
        "Eden [Dev]",
        "Devon [Designer]",
        "Kai [QE]",
        "Kai [DSO]",
    ]
    blocked = 0
    for mate in teammates:
        _, err = _identity_check(pm_env, mate)
        if err and "Impersonation refused" in json.loads(err)["error"]:
            blocked += 1
    assert blocked == len(teammates), (
        f"Expected all {len(teammates)} impersonation attempts to be "
        f"blocked; only {blocked} were."
    )
