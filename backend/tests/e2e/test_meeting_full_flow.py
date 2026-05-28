"""E2E: PM-as-creator drives a full meeting through to verdict.

Guards the meeting-flow contract that broke production (b61af7db incident):

* Creator (PM) is auto-prepended to ``participants`` so they speak first.
* Round-robin order matches ``participants`` exactly: PM → BA → Dev → QE → PM.
* PM's second turn carrying ``[DECISION] VERDICT: PASS`` ends the meeting and
  fires ``meeting_ended`` notifications to **every** participant including PM.
* Each ``speak()`` enforces "current speaker only" — if turn order is wrong
  the test fails loudly with the actual error JSON.

Real components used: ``meeting_room_server`` tools (create_meeting, speak),
``JsonFileMeetingStorage`` (real workspace dir), ``MessageBus`` (real inbox
files). Only ``_auto_wake_if_idle`` and ``uuid.uuid4`` are patched — the
former relies on a live AgentChannel socket, the latter to make meeting_id
deterministic so fixtures can reference it.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from tests.e2e.harness import ToolCallRecorder, build_scripted_agent


FIXTURES = Path(__file__).parent / "fixtures"
MEETING_ID = "b61af7db"  # mirrors fixtures' hardcoded meeting_id


@pytest.fixture
def real_meeting_env(tmp_path, monkeypatch):
    """Wire real JsonFileMeetingStorage + real MessageBus into meeting_room_server.

    Yields ``(storage, bus)`` for direct inspection.
    """
    from fast_agent.spawn.message_bus import MessageBus
    from fast_agent.spawn.servers.meeting_storage import SqliteMeetingStorage
    import fast_agent.spawn.servers.meeting_room_server as mrs

    workspace = tmp_path / "workspace"
    messages_dir = tmp_path / "messages"
    workspace.mkdir()
    messages_dir.mkdir()

    # Production uses SqliteMeetingStorage (see meeting_room_server.__main__).
    # JsonFileMeetingStorage is only the library default for standalone use
    # and lacks the ``_conn`` kwarg the production speak() code path requires.
    storage = SqliteMeetingStorage(db_path=str(tmp_path / "meetings.db"))
    bus = MessageBus(messages_dir=str(messages_dir))

    monkeypatch.setattr(mrs, "_storage", storage)
    monkeypatch.setattr(mrs, "_get_bus", lambda: bus)
    # AgentChannel socket / spawn registry not available in test process —
    # no-op auto-wake (the inbox write itself is what we verify).
    monkeypatch.setattr(mrs, "_auto_wake_if_idle", lambda *a, **kw: None)
    # Fallback name when an explicit one isn't passed (fixtures pass explicit).
    monkeypatch.setattr(mrs, "_get_my_name", lambda: "system")

    # Deterministic meeting_id so fixture YAML can hardcode "b61af7db".
    fake_uuid = type("FakeUUID", (), {"hex": MEETING_ID + "0" * 24})()
    monkeypatch.setattr(uuid, "uuid4", lambda: fake_uuid)

    return storage, bus


def _last_inbox_msg_of_type(bus, agent_name, message_type):
    """Pop the most recent message of ``message_type`` from ``agent_name`` inbox."""
    msgs = bus.read_inbox(agent_name)
    matches = [m for m in msgs if m.message_type == message_type]
    assert matches, (
        f"Expected at least one {message_type!r} in {agent_name!r} inbox, "
        f"got types={[m.message_type for m in msgs]}"
    )
    return matches[-1]


@pytest.mark.asyncio
async def test_pm_creates_speaks_first_then_verdict_ends_meeting(real_meeting_env):
    """Full happy-path: creator speaks first, round-robin completes, PM verdict ends."""
    storage, bus = real_meeting_env

    from fast_agent.spawn.servers.meeting_room_server import create_meeting, speak

    pm_recorder = ToolCallRecorder()
    pm_agent = await build_scripted_agent(
        fixture_path=FIXTURES / "meeting_pm_full_flow.yaml",
        tools=[create_meeting, speak],
        agent_name="Cameron [PM]",
        recorder=pm_recorder,
    )
    ba_agent = await build_scripted_agent(
        fixture_path=FIXTURES / "meeting_ba_speak_once.yaml",
        tools=[speak],
        agent_name="Devon [BA]",
    )
    dev_agent = await build_scripted_agent(
        fixture_path=FIXTURES / "meeting_dev_speak_once.yaml",
        tools=[speak],
        agent_name="Reese [Dev]",
    )
    qe_agent = await build_scripted_agent(
        fixture_path=FIXTURES / "meeting_qe_speak_once.yaml",
        tools=[speak],
        agent_name="Devon [QE]",
    )

    # ── Step 1: PM kicks off (create_meeting + speak round 1) ──
    await pm_agent.generate("Run the kickoff for the audit project.")

    config = storage.get_config(MEETING_ID)
    state = storage.get_state(MEETING_ID)
    assert config is not None, "PM's create_meeting tool call did not persist a meeting"

    # Post-B1: participants live in state_json (mutable bucket).
    # config_json holds only write-once metadata.
    assert "participants" not in config, (
        "participants must NOT be in config_json — design contract"
    )
    assert state["participants"] == [
        "Cameron [PM]",
        "Devon [BA]",
        "Reese [Dev]",
        "Devon [QE]",
    ], (
        f"Creator must be at index 0 (auto-prepend). Got: {state['participants']}"
    )
    assert config["created_by"] == "Cameron [PM]"

    # PM's first speak() must succeed — implicit proof PM is current_speaker[0].
    transcript = storage.get_transcript(MEETING_ID)
    assert len(transcript) == 1, f"After PM kickoff, expected 1 turn; got {len(transcript)}"
    assert transcript[0]["agent"] == "Cameron [PM]"
    assert transcript[0]["round"] == 1

    # ── Step 2: BA reads YOUR_TURN, speaks ──
    ba_turn_msg = _last_inbox_msg_of_type(bus, "Devon [BA]", "meeting_turn")
    assert MEETING_ID in ba_turn_msg.content, "YOUR_TURN must reference meeting_id"
    await ba_agent.generate(ba_turn_msg.content)

    transcript = storage.get_transcript(MEETING_ID)
    assert len(transcript) == 2 and transcript[1]["agent"] == "Devon [BA]"

    # ── Step 3: Dev's turn ──
    dev_turn_msg = _last_inbox_msg_of_type(bus, "Reese [Dev]", "meeting_turn")
    await dev_agent.generate(dev_turn_msg.content)

    transcript = storage.get_transcript(MEETING_ID)
    assert len(transcript) == 3 and transcript[2]["agent"] == "Reese [Dev]"

    # ── Step 4: QE's turn closes round 1 ──
    qe_turn_msg = _last_inbox_msg_of_type(bus, "Devon [QE]", "meeting_turn")
    await qe_agent.generate(qe_turn_msg.content)

    transcript = storage.get_transcript(MEETING_ID)
    assert len(transcript) == 4 and transcript[3]["agent"] == "Devon [QE]"

    # State must show round wrapped to 2 with PM up next.
    state = storage.get_state(MEETING_ID)
    assert state["current_round"] == 2 and state["current_turn"] == 0
    assert not state["ended"], "Meeting should still be open at start of round 2"

    # ── Step 5: PM speaks with [DECISION] VERDICT — meeting ends ──
    pm_turn_msg = _last_inbox_msg_of_type(bus, "Cameron [PM]", "meeting_turn")
    # The third meeting_turn for PM = round 2 cue (first was kickoff self-notify).
    await pm_agent.generate(pm_turn_msg.content)

    state = storage.get_state(MEETING_ID)
    transcript = storage.get_transcript(MEETING_ID)

    assert state["ended"] is True, f"Meeting should be ended; state={state}"
    assert state["outcome"] == "verdict_pass", (
        f"Verdict PASS should map to outcome 'verdict_pass'; got {state['outcome']!r}"
    )

    # Full transcript shape — proves round-robin order held end-to-end.
    speakers = [(t["agent"], t["round"]) for t in transcript]
    assert speakers == [
        ("Cameron [PM]", 1),
        ("Devon [BA]", 1),
        ("Reese [Dev]", 1),
        ("Devon [QE]", 1),
        ("Cameron [PM]", 2),
    ], f"Round-robin order broke. Got: {speakers}"

    # PM's verdict message survived intact (not stripped/mutated by speak()).
    assert "[DECISION]" in transcript[-1]["message"]
    assert "VERDICT: PASS" in transcript[-1]["message"]

    # ── Step 6: meeting_ended notification went to ALL participants (PM included) ──
    for participant in [
        "Cameron [PM]",
        "Devon [BA]",
        "Reese [Dev]",
        "Devon [QE]",
    ]:
        ended = _last_inbox_msg_of_type(bus, participant, "meeting_ended")
        assert MEETING_ID in ended.content, (
            f"{participant} meeting_ended must reference meeting_id"
        )
        # Transcript embedded so PM can synthesize without a second tool call.
        assert "Welcome team" in ended.content, (
            f"{participant} meeting_ended should embed full transcript "
            f"(PM kickoff line missing). Got: {ended.content[:200]}"
        )

    # ── Step 7: PM tool-call sequence matches the fixture exactly ──
    pm_recorder.assert_matches([
        ("create_meeting", {
            "agenda": "Sprint 1 kickoff",
            "participants": "Devon [BA], Reese [Dev], Devon [QE]",
            "max_rounds": 2,
            "my_name": "Cameron [PM]",
            "workspace_dir": "",
        }),
        ("speak", {
            "meeting_id": MEETING_ID,
            "message": "Welcome team. Goal today: align on the audit plan and produce action items by end of meeting.",
            "agent_name": "Cameron [PM]",
        }),
        ("speak", {
            "meeting_id": MEETING_ID,
            "message": "[DECISION] VERDICT: PASS — Plan accepted. BA owns story creation, Dev runs tests, QE compiles audit report.",
            "agent_name": "Cameron [PM]",
        }),
    ])


@pytest.mark.asyncio
async def test_speak_rejects_wrong_speaker(real_meeting_env):
    """Negative control: BA cannot speak when it's PM's turn.

    Proves the round-robin enforcement is real (not just fixture coincidence).
    Without the auto-prepend fix this would fail differently — BA might be
    participants[0] and the test would silently pass with the wrong contract.
    """
    storage, bus = real_meeting_env

    from fast_agent.spawn.servers.meeting_room_server import create_meeting, speak

    # Use a tiny fixture that just creates the meeting — PM doesn't speak.
    create_only_path = FIXTURES / "_meeting_pm_create_only.yaml"
    create_only_path.write_text(
        "turns:\n"
        "  - tool_calls:\n"
        "      c_create:\n"
        "        name: create_meeting\n"
        "        arguments:\n"
        "          agenda: \"Test\"\n"
        "          participants: \"Devon [BA]\"\n"
        "          max_rounds: 2\n"
        "          my_name: \"Cameron [PM]\"\n"
        "          workspace_dir: \"\"\n"
        "  - content: \"created\"\n",
        encoding="utf-8",
    )

    pm_agent = await build_scripted_agent(
        fixture_path=create_only_path,
        tools=[create_meeting, speak],
        agent_name="Cameron [PM]",
    )

    await pm_agent.generate("Create the meeting.")
    create_only_path.unlink()

    state = storage.get_state(MEETING_ID)
    assert state["participants"] == ["Cameron [PM]", "Devon [BA]"]

    # Direct call to speak as BA — PM is current_speaker, BA should be rejected.
    # Simulates BA's spawn-isolated process making the call: TEAM_MY_NAME=BA
    # so the identity check passes; the "Not your turn" check (which is the
    # contract under test here) is the one that must reject.
    import json
    import os

    _prior = os.environ.get("TEAM_MY_NAME")
    os.environ["TEAM_MY_NAME"] = "Devon [BA]"
    try:
        result = json.loads(
            await speak(
                meeting_id=MEETING_ID,
                message="BA jumping the queue.",
                agent_name="Devon [BA]",
            )
        )
    finally:
        if _prior is None:
            os.environ.pop("TEAM_MY_NAME", None)
        else:
            os.environ["TEAM_MY_NAME"] = _prior
    assert "error" in result, f"Expected rejection, got: {result}"
    assert "Not your turn" in result["error"]
    assert "Cameron [PM]" in result["error"], (
        f"Error must name the actual current speaker; got: {result['error']}"
    )

    # Transcript must remain empty — rejected speak() must not write anything.
    assert storage.get_transcript(MEETING_ID) == []
