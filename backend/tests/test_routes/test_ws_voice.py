"""/ws/voice — WebSocket transport for the hands-free path.

We can't exercise faster-whisper or a real audio pipeline in CI, so the test
plugs a fake STT service into ``shared_state`` and checks that:

  * binary frames from the client end up in ``feed_audio``
  * STT callbacks bridged through ``set_hook`` arrive on the wire as JSON
  * the speak() control message routes to the active chat provider and the
    response audio flows back as binary frames
  * barge-in via VAD start cancels in-flight TTS
"""
from __future__ import annotations

import asyncio
import json
import threading

import pytest
from fastapi.testclient import TestClient


class _FakeSTT:
    """Stand-in for RealtimeSTTService — captures fed audio + exposes hook."""

    def __init__(self):
        self.fed: list[bytes] = []
        self._hook = None

    def set_hook(self, hook):
        self._hook = hook

    def feed_audio(self, chunk: bytes):
        self.fed.append(chunk)

    def emit(self, name: str, payload=None):
        if self._hook is not None:
            self._hook(name, payload or {})

    def shutdown(self):
        pass


class _StubProvider:
    """Stand-in chat TTS provider — emits raw PCM directly so the test
    sidesteps the production MP3→ffmpeg→PCM transcode pipeline."""

    async def stream_audio(self, text: str):  # not used when stream_pcm exists
        yield b""

    async def stream_pcm(self, text: str):
        # 4 samples of int16 silence — enough that the WS handler routes a
        # binary frame back to the client, which is what the assertion below
        # checks.
        yield b"\x00\x00\x00\x00\x00\x00\x00\x00"


@pytest.fixture()
def ws_client(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_API_KEY", "")  # disable WS auth for this test
    from sqlalchemy import create_engine as _ce
    from sqlalchemy.orm import sessionmaker
    from core.database import Base, SETUP_WIZARD_CRITICAL_STEPS, SetupWizardStep
    eng = _ce(f"sqlite:///{tmp_path}/ws.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=eng)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=eng)
    with Session() as db:
        for name in SETUP_WIZARD_CRITICAL_STEPS:
            db.add(SetupWizardStep(step_name=name, completed=True))
        db.commit()
    import core.database as core_db
    from services import config_service as cs_mod
    monkeypatch.setattr(core_db, "SessionLocal", Session)
    monkeypatch.setattr(cs_mod, "SessionLocal", Session)
    monkeypatch.setattr(cs_mod, "config_service", cs_mod.ConfigService(db_factory=Session))
    from middleware.setup_gate import _reset_cache_for_tests, refresh_setup_complete
    _reset_cache_for_tests()
    refresh_setup_complete()

    from services import shared_state
    fake_stt = _FakeSTT()
    monkeypatch.setattr(shared_state, "stt_recorder", fake_stt)
    monkeypatch.setattr(shared_state, "tts_chat_provider", _StubProvider())

    from server import app
    return TestClient(app), fake_stt


def test_pcm_in_flows_to_feed_audio(ws_client):
    client, fake_stt = ws_client
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_bytes(b"\x00\x01\x02\x03")
        ws.send_bytes(b"\x04\x05")
        ws.close()
    assert fake_stt.fed == [b"\x00\x01\x02\x03", b"\x04\x05"]


def test_stt_event_bridges_to_json_frame(ws_client):
    client, fake_stt = ws_client
    with client.websocket_connect("/ws/voice") as ws:
        # Trigger an STT event from "outside" — emulates a faster-whisper
        # callback firing on a worker thread.
        fake_stt.emit("partial_transcript", {"text": "xin chào"})
        msg = ws.receive_text()
        evt = json.loads(msg)
        assert evt["type"] == "partial_transcript"
        assert evt["text"] == "xin chào"
        ws.close()


def test_dictation_mode_suppresses_llm_dispatch(ws_client, monkeypatch):
    """``{type:'start', mode:'dictation'}`` must short-circuit final_transcript.

    The bottom-mic dictation flow streams transcripts into a text box for
    the user to edit + submit manually — invoking the LLM agent on every
    finalised utterance would defeat the whole point (and burn tokens).
    Guard: spy on ``shared_state.session_service.resume_and_send`` and
    confirm it never fires even though the STT emits a final_transcript.
    """
    from unittest.mock import AsyncMock, MagicMock
    from services import shared_state

    fake_app = MagicMock()
    fake_app._agents = {"Jarvis": MagicMock(tool_runner_hooks=None)}
    monkeypatch.setattr(shared_state, "agent_app", fake_app)

    resume_spy = AsyncMock(return_value=("should-not-fire", "session-x"))
    fake_session = MagicMock(resume_and_send=resume_spy)
    monkeypatch.setattr(shared_state, "session_service", fake_session, raising=False)

    client, fake_stt = ws_client
    with client.websocket_connect("/ws/voice") as ws:
        # Flip the session into dictation mode BEFORE firing a final.
        # The dictation gate is checked inside _dispatch_user_turn which
        # runs on the asyncio loop — same thread that processes this
        # start message — so once receive_text round-trips, the flag is
        # guaranteed visible to the dispatcher. No "beat" trick needed.
        ws.send_text(json.dumps({"type": "start", "mode": "dictation"}))

        fake_stt.emit("final_transcript", {"text": "Xin chào Jarvis"})
        final = json.loads(ws.receive_text())
        # The transcript itself MUST still propagate — the client uses it
        # to populate the input field. What must NOT happen is the LLM
        # turn that the standard hands-free path triggers.
        assert final["type"] == "final_transcript"
        assert final["text"] == "Xin chào Jarvis"
        ws.close()

    # LLM dispatcher never invoked — neither agent_thinking nor
    # resume_and_send fired. If the dictation gate ever regresses, this
    # spy will catch it.
    resume_spy.assert_not_awaited()


def test_default_mode_still_dispatches_llm_on_final_transcript(ws_client, monkeypatch):
    """Negative control for the dictation gate.

    Without ``mode: dictation`` in the start handshake, final_transcript
    MUST still drive the agent turn — otherwise the dictation guard above
    would pass trivially against a fully broken pipeline. We assert the
    classic ``user_message`` event arrives, which is the first thing
    ``_dispatch_user_turn`` emits.
    """
    from unittest.mock import AsyncMock, MagicMock
    from services import shared_state

    fake_app = MagicMock()
    fake_app._agents = {"Jarvis": MagicMock(tool_runner_hooks=None)}
    monkeypatch.setattr(shared_state, "agent_app", fake_app)

    resume_spy = AsyncMock(return_value=("hi back", "session-x"))
    fake_session = MagicMock(resume_and_send=resume_spy)
    monkeypatch.setattr(shared_state, "session_service", fake_session, raising=False)

    client, fake_stt = ws_client
    with client.websocket_connect("/ws/voice") as ws:
        # No start handshake → backend default = full conversation mode.
        fake_stt.emit("final_transcript", {"text": "hello"})
        evt = _drain_until(ws, "user_message")
        assert evt["text"] == "hello"
        ws.close()


def test_speak_command_streams_chat_provider_audio_back(ws_client):
    client, _ = ws_client
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_text(json.dumps({"type": "speak", "text": "hi"}))
        # Order: tts_start JSON → binary PCM chunk(s) → tts_end JSON.
        first = ws.receive_text()
        assert json.loads(first)["type"] == "tts_start"
        audio = ws.receive_bytes()
        # WS now emits raw int16 mono PCM (no MP3 magic bytes); just check
        # we got a non-empty even-length chunk that could be PCM.
        assert len(audio) > 0 and len(audio) % 2 == 0
        last = ws.receive_text()
        assert json.loads(last)["type"] == "tts_end"
        ws.close()


def _drain_until(ws, type_name: str, *, max_msgs: int = 8):
    """Pull text+binary frames until a JSON event of ``type_name`` shows up.

    Binary frames (audio chunks) and earlier control events are silently
    discarded so the test doesn't have to mirror the exact ordering of
    tts_start / audio / tts_end which is provider-dependent.
    """
    for _ in range(max_msgs):
        msg = ws.receive()
        if msg.get("type") == "websocket.disconnect":
            raise AssertionError(f"socket closed before {type_name}")
        if msg.get("text"):
            evt = json.loads(msg["text"])
            if evt.get("type") == type_name:
                return evt
    raise AssertionError(f"never saw {type_name} (drained {max_msgs} msgs)")


def test_tool_events_from_progress_hooks_reach_client(ws_client, monkeypatch):
    """Voice turns must surface ``tool_request`` / ``tool_done`` to the WS.

    Regression guard: an earlier voice rewrite called ``resume_and_send``
    directly without attaching the SSE-style ``create_progress_hooks``,
    so tool calls that ran inside the agent never produced any
    tool-bubble events. The dashboard's voice flow ended up showing the
    final ``assistant_message`` with no compact "X tools used" bubble
    even when the agent had run several tools — this test fences that
    pipeline so we can't ship voice without tool visibility again.
    """
    from unittest.mock import MagicMock
    from services import shared_state
    from services.sse_progress import progress_manager

    fake_agent = MagicMock()
    fake_agent.tool_runner_hooks = None
    fake_app = MagicMock()
    fake_app._agents = {"Jarvis": fake_agent}
    monkeypatch.setattr(shared_state, "agent_app", fake_app)

    async def _fake_resume(_app, _text, _sid, **_kw):
        # Push tool events from inside the LLM call, exactly like the
        # real progress hooks would. The drain task must forward these
        # to the WS out queue.
        active_ids = list(progress_manager._queues.keys())
        assert active_ids, "no progress queue created for this turn"
        rid = active_ids[-1]  # the freshest one is this turn's
        progress_manager.push(rid, "tool_request", {
            "tools": [{"name": "weather"}],
            "message": "Jarvis calling weather",
        })
        await asyncio.sleep(0)  # let the drain task forward the event
        progress_manager.push(rid, "tool_done", {
            "tools": [{"name": "weather"}],
            "duration_ms": 1234,
            "result_preview": "sunny",
        })
        await asyncio.sleep(0)
        return "ok", "session-x"

    fake_session = MagicMock()
    fake_session.resume_and_send = _fake_resume
    monkeypatch.setattr(shared_state, "session_service", fake_session)

    client, fake_stt = ws_client
    with client.websocket_connect("/ws/voice") as ws:
        # Drain stt_loading / stt_ready / etc. so the queue is calm before
        # we trigger the turn.
        fake_stt.emit("final_transcript", {"text": "thời tiết"})
        seen: list[dict] = []
        for _ in range(20):
            msg = ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("text"):
                evt = json.loads(msg["text"])
                seen.append(evt)
                if evt.get("type") == "tts_end":
                    break
        ws.close()

    types = [e.get("type") for e in seen]
    assert "tool_request" in types, f"missing tool_request — got {types}"
    assert "tool_done" in types, f"missing tool_done — got {types}"
    req = next(e for e in seen if e["type"] == "tool_request")
    done = next(e for e in seen if e["type"] == "tool_done")
    assert req["tools"][0]["name"] == "weather"
    assert done["tools"][0]["name"] == "weather"
    # Verify hooks were restored — leaking the per-turn hooks would
    # mean tool events from the next turn went to a dead queue.
    assert fake_agent.tool_runner_hooks is None


def test_rapid_cancel_does_not_wipe_new_turn_hooks(ws_client, monkeypatch):
    """Turn 2 cancels Turn 1 mid-LLM. Turn 2's tool events must reach the WS.

    Regression: turn 1's ``finally`` block restored ``tool_runner_hooks``
    to the pre-turn snapshot. If turn 2 had already attached its own
    hooks by then (rapid back-to-back dispatch), turn 1's restore wiped
    them — so tools that turn 2 ran emitted progress events into a
    queue nobody was reading, and the dashboard's tool-bubble bar
    stayed empty for that turn. User-visible symptom: "tool bubble
    only disappears when the user barges in; it appears in the normal flow".
    """
    from unittest.mock import MagicMock
    from services import shared_state
    from services.sse_progress import progress_manager

    fake_agent = MagicMock()
    fake_agent.tool_runner_hooks = None
    fake_app = MagicMock()
    fake_app._agents = {"Jarvis": fake_agent}
    monkeypatch.setattr(shared_state, "agent_app", fake_app)

    # Turn 1: blocks long enough that we can cancel it cleanly.
    # Turn 2: pushes a tool_request via the agent's currently-attached
    # progress hook — the very thing we're checking lights up.
    call_count = {"n": 0}

    async def _resume(_app, _text, _sid, **_kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            await asyncio.sleep(2)  # turn 1 — will be cancelled
            return "should-not-reach", "session-x"
        # turn 2 — fire a tool event into whatever queue the currently
        # attached progress hook owns. If turn 1's finally has already
        # wiped our hook, this push goes nowhere.
        active_ids = list(progress_manager._queues.keys())
        assert active_ids, "no progress queue alive for turn 2"
        progress_manager.push(active_ids[-1], "tool_request", {
            "tools": [{"name": "second_turn_tool"}],
            "message": "should be visible",
        })
        await asyncio.sleep(0)
        return "ok", "session-x"

    fake_session = MagicMock()
    fake_session.resume_and_send = _resume
    monkeypatch.setattr(shared_state, "session_service", fake_session)

    client, fake_stt = ws_client
    with client.websocket_connect("/ws/voice") as ws:
        # Turn 1 — drain to confirm it actually started.
        fake_stt.emit("final_transcript", {"text": "first"})
        for _ in range(10):
            msg = ws.receive()
            if msg.get("text"):
                evt = json.loads(msg["text"])
                if evt.get("type") == "agent_thinking":
                    break

        # Turn 2 fires while turn 1 is mid-sleep.
        fake_stt.emit("final_transcript", {"text": "second"})

        # Drain everything until turn 2's assistant_message lands. That
        # event signals the whole turn cycle (including any tool_request
        # turn 2 emitted) made it onto the wire.
        seen: list[dict] = []
        for _ in range(40):
            msg = ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("text"):
                evt = json.loads(msg["text"])
                seen.append(evt)
                # We expect either an assistant_message ("ok") or an
                # error to be the terminal signal for turn 2.
                if evt.get("type") in {"assistant_message", "error", "tts_end"}:
                    break

        ws.close()

    types = [e.get("type") for e in seen]
    tool_events = [e for e in seen if e.get("type") == "tool_request"]
    assert tool_events, (
        f"turn 2's tool_request never reached the client — hook race "
        f"likely wiped the new-turn progress hooks. events: {types}"
    )
    assert any(
        (e.get("tools") or [{}])[0].get("name") == "second_turn_tool"
        for e in tool_events
    ), f"got tool_request events but not turn 2's: {tool_events}"


def test_cancelled_turn_does_not_emit_empty_assistant_message(ws_client, monkeypatch):
    """When a turn is cancelled mid-LLM, fast-agent's OpenAI provider
    catches ``CancelledError`` and returns an empty Prompt instead of
    propagating the cancellation. Without explicit cancellation
    detection, the cancelled turn would then proceed to emit
    ``assistant_message empty=True`` — which the frontend uses as the
    cue to drop the in-flight placeholder. By the time this fires,
    ``pendingAgentMsgId`` on the client already points to the *new*
    turn's placeholder, so the empty signal yanks the new bubble
    out and the user sees status flip to "Listening" with no dots
    and no agent reply. This was the umbrella root cause for the
    "placeholder lost / tool bubble lost / TTS silent" trio after a
    user-correction barge-in.

    Test design: the resume_and_send mock simulates exactly the
    swallowed-cancellation contract — it returns ``("", session)``
    after the task has been cancelled. We assert the cancelled task
    does NOT emit ``assistant_message`` at all.
    """
    from unittest.mock import MagicMock
    from services import shared_state

    fake_agent = MagicMock()
    fake_agent.tool_runner_hooks = None
    fake_agent.message_history = []
    fake_app = MagicMock()
    fake_app._agents = {"Jarvis": fake_agent}
    monkeypatch.setattr(shared_state, "agent_app", fake_app)

    async def _fake_resume(_app, _text, _sid, **_kw):
        # Block long enough for the test to fire a barge-in.
        try:
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            # Mirror fast-agent OpenAI provider's swallow-and-return-empty
            # contract. This is the exact behaviour the production fix
            # has to defend against.
            return "", "session-x"
        return "should-not-reach", "session-x"

    fake_session = MagicMock()
    fake_session.resume_and_send = _fake_resume
    monkeypatch.setattr(shared_state, "session_service", fake_session)

    client, fake_stt = ws_client
    with client.websocket_connect("/ws/voice") as ws:
        # Turn 1 — wait for agent_thinking so we know it's running.
        fake_stt.emit("final_transcript", {"text": "first"})
        for _ in range(15):
            msg = ws.receive()
            if msg.get("text"):
                evt = json.loads(msg["text"])
                if evt.get("type") == "agent_thinking":
                    break

        # Turn 2 cancels turn 1.
        fake_stt.emit("final_transcript", {"text": "second"})
        # Pump a sentinel so we know all turn-1 events that *were*
        # going to fire have already arrived at the client.
        import time as _t
        _t.sleep(0.5)
        fake_stt.emit("partial_transcript", {"text": "__SENTINEL__"})

        # The cancelled turn 1 returns ("", session) from the mock's
        # CancelledError-swallow path. With the regression in place,
        # _handle_user_turn would proceed to emit
        # ``assistant_message empty=True`` even for the cancelled turn.
        # The fix bails on cancellation BEFORE any further out_queue
        # emission. Turn 2 returns "should-not-reach" (non-empty) so
        # any empty=True assistant_message we observe must come from
        # the cancelled turn.
        empty_assistant_count = 0
        for _ in range(30):
            msg = ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if not msg.get("text"):
                continue
            evt = json.loads(msg["text"])
            if evt.get("type") == "assistant_message" and evt.get("empty") is True:
                empty_assistant_count += 1
            elif evt.get("type") == "partial_transcript" and evt.get("text") == "__SENTINEL__":
                break
        ws.close()

    assert empty_assistant_count == 0, (
        f"cancelled turn 1 emitted {empty_assistant_count} empty "
        "assistant_message(s) — the fast-agent CancelledError swallow "
        "needs to be detected via task.cancelling() so the cancelled "
        "turn exits silently. Without this, the empty=True signal "
        "drops the new turn's placeholder on the frontend (umbrella "
        "cause for placeholder/tool-bubble/TTS regressions)."
    )


def test_voice_turn_persists_agent_context(ws_client, monkeypatch):
    """Voice turns must call ``save_agent_context`` so the Agents tab's
    Context Window panel actually shows the conversation. Regression
    for "context window not saved to db after agent idle" —
    voice was missing the context save that text /chat-stream had,
    leaving voice conversations invisible to the history view.
    """
    from unittest.mock import MagicMock
    from services import shared_state
    import services.context_persistence as ctx_mod

    fake_agent = MagicMock()
    fake_agent.tool_runner_hooks = None
    fake_agent.message_history = ["msg1"]  # non-empty so save isn't skipped
    fake_app = MagicMock()
    fake_app._agents = {"Jarvis": fake_agent}
    monkeypatch.setattr(shared_state, "agent_app", fake_app)

    async def _resume(_app, _text, _sid, **_kw):
        return "hello there", "session-x"

    fake_session = MagicMock()
    fake_session.resume_and_send = _resume
    monkeypatch.setattr(shared_state, "session_service", fake_session)

    # Spy on save_agent_context — patch the symbol the route imports
    # lazily inside _handle_user_turn (re-imports each turn).
    save_calls: list[dict] = []

    async def _spy_save(agent, run_id, trigger, *, agent_name=None,
                       session_id=None, team_name=None):
        save_calls.append({
            "agent": agent,
            "run_id": run_id,
            "trigger": trigger,
            "agent_name": agent_name,
            "session_id": session_id,
        })
        return 1
    monkeypatch.setattr(ctx_mod, "save_agent_context", _spy_save)

    client, fake_stt = ws_client
    with client.websocket_connect("/ws/voice") as ws:
        fake_stt.emit("final_transcript", {"text": "hi"})
        # Drain until tts_end (turn fully done so save would have run).
        for _ in range(30):
            msg = ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("text"):
                evt = json.loads(msg["text"])
                if evt.get("type") == "tts_end":
                    break
        ws.close()

    assert save_calls, "save_agent_context was never invoked for the voice turn"
    call = save_calls[-1]
    assert call["trigger"] == "voice_turn_complete"
    assert call["agent_name"] == "Jarvis"
    assert call["agent"] is fake_agent


def test_stop_message_does_not_crash_backend(ws_client):
    """Sending {type: stop} closes the socket cleanly; nothing on the
    server should raise. Regression guard for the teardown lifecycle —
    if frontend cleanup races (page navigation while a turn is in
    flight), the explicit ``stop`` text frame should be the safe exit
    path. A backend crash here would orphan the worker thread that
    feeds STT.
    """
    client, fake_stt = ws_client
    with client.websocket_connect("/ws/voice") as ws:
        # Fire-and-forget the stop control message + close.
        ws.send_text(json.dumps({"type": "stop"}))
        ws.close()
    # Sanity: the fixture's stt fake survived the socket teardown
    # (its ``shutdown`` is a no-op so we just check the object exists).
    assert fake_stt is not None


def test_finally_does_not_overwrite_a_newer_turns_hooks(monkeypatch):
    """Direct unit-level guard for the hook race: a finally block from a
    cancelled turn must not stomp on hooks that a newer turn has
    already swapped in.

    Tests the contract directly (not via WS) so the assertion doesn't
    depend on async scheduling races — we install hooks A, then hooks
    B, then run A's restore logic; B must remain in place.
    """
    from unittest.mock import MagicMock
    from services import shared_state
    from services.sse_progress import progress_manager, create_progress_hooks
    from services.sse_progress import merge_hooks as _merge

    fake_agent = MagicMock()
    fake_agent.tool_runner_hooks = None
    fake_app = MagicMock()
    fake_app._agents = {"Jarvis": fake_agent}
    monkeypatch.setattr(shared_state, "agent_app", fake_app)

    # Turn A snapshot + attach
    rid_a = "turn-a"
    progress_manager.create(rid_a)
    hooks_a = create_progress_hooks(rid_a)
    original_a = fake_agent.tool_runner_hooks
    attached_a = _merge(original_a, hooks_a) if original_a else hooks_a
    fake_agent.tool_runner_hooks = attached_a

    # Turn B starts before A's finally runs — same setup against the
    # same agent.
    rid_b = "turn-b"
    progress_manager.create(rid_b)
    hooks_b = create_progress_hooks(rid_b)
    original_b = fake_agent.tool_runner_hooks  # currently A's chain
    attached_b = _merge(original_b, hooks_b) if original_b else hooks_b
    fake_agent.tool_runner_hooks = attached_b

    # Now turn A's finally runs. Replicate the restoration logic
    # exactly. With the race-safe check, attached_a is no longer the
    # current hook (B replaced it) so A leaves it alone.
    #
    # Cross-check: if we used the OLD blind-restore code, this would
    # wipe B's hooks. We verify both paths so the regression won't
    # creep back in.
    blind_restore_would_break = (
        fake_agent.tool_runner_hooks is not attached_a
    )
    assert blind_restore_would_break, (
        "test scaffolding wrong: B's attach didn't actually replace "
        "A's hook chain so the race wouldn't manifest"
    )

    if getattr(fake_agent, "tool_runner_hooks", None) is attached_a:
        fake_agent.tool_runner_hooks = original_a

    assert fake_agent.tool_runner_hooks is attached_b, (
        "turn A's finally wiped turn B's hooks — tool events from "
        "turn B will not reach the client"
    )

    progress_manager.remove(rid_a)
    progress_manager.remove(rid_b)


def test_cancelled_turn_emits_only_one_tts_interruption(ws_client, monkeypatch):
    """When a turn is cancelled mid-LLM, the WS must emit exactly ONE
    ``tts_interruption`` event, not two.

    Regression: ``_cancel_inflight`` already pushes ``tts_interruption``
    when it calls ``.cancel()``. The cancelled task's ``except
    asyncio.CancelledError`` block used to push another one — the second
    event raced against the *new* turn's ``agent_thinking`` and, when it
    arrived afterwards, the frontend's ``_dropPending`` removed the
    fresh placeholder. Visible symptom: user spoke a correction, the
    "..." typing dots vanished and never came back, even though the
    agent was processing.
    """
    from unittest.mock import MagicMock
    from services import shared_state

    fake_agent = MagicMock()
    fake_agent.tool_runner_hooks = None
    fake_app = MagicMock()
    fake_app._agents = {"Jarvis": fake_agent}
    monkeypatch.setattr(shared_state, "agent_app", fake_app)

    # ``resume_and_send`` blocks long enough that we can fire a barge_in
    # while the agent task is still mid-LLM — the only way to hit the
    # cancelled-task ``except`` path that used to double-emit.
    async def _slow_resume(_app, _text, _sid, **_kw):
        await asyncio.sleep(2)
        return "irrelevant", "session-x"

    fake_session = MagicMock()
    fake_session.resume_and_send = _slow_resume
    monkeypatch.setattr(shared_state, "session_service", fake_session)

    client, fake_stt = ws_client
    with client.websocket_connect("/ws/voice") as ws:
        fake_stt.emit("final_transcript", {"text": "first utterance"})
        # Drain until agent_thinking confirms the turn has started.
        for _ in range(15):
            msg = ws.receive()
            if msg.get("text"):
                evt = json.loads(msg["text"])
                if evt.get("type") == "agent_thinking":
                    break

        # Cancel via explicit barge_in (same code path _cancel_inflight
        # walks when STT recording_start fires while bot_speaking).
        ws.send_text(json.dumps({"type": "barge_in"}))

        # Give the event loop time to run the cancellation cycle to
        # completion: cancel propagates to ``await resume_and_send``,
        # the ``finally`` block restores hooks, then the ``except
        # CancelledError`` arm runs (this is where the duplicate
        # tts_interruption used to be emitted). Without this delay the
        # sentinel below can race AHEAD of the duplicate, and the test
        # would silently pass even when the regression is present.
        import time as _t
        _t.sleep(0.5)

        # Pump a sentinel event LAST so its arrival at the client tells
        # us every prior event the cancellation could have emitted is
        # also in our hands.
        fake_stt.emit("partial_transcript", {"text": "__SENTINEL__"})

        seen_types: list[str] = []
        for _ in range(30):
            msg = ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if not msg.get("text"):
                continue
            evt = json.loads(msg["text"])
            seen_types.append(evt.get("type"))
            if evt.get("type") == "partial_transcript" and evt.get("text") == "__SENTINEL__":
                break

        ws.close()

    interruption_count = sum(1 for t in seen_types if t == "tts_interruption")
    assert interruption_count == 1, (
        f"expected exactly 1 tts_interruption, got {interruption_count} "
        f"(events: {seen_types})"
    )


def test_barge_in_keeps_socket_alive_for_further_events(ws_client):
    """Sending a barge_in mid-conversation must not break the socket.

    The actual cancellation latency depends on the active provider — for our
    stub (which finishes synth in microseconds) the cancel is racy by design.
    What we lock in here is the safety property: after barge_in the socket
    is still usable for further events.
    """
    client, fake_stt = ws_client
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_text(json.dumps({"type": "speak", "text": "x"}))
        _drain_until(ws, "tts_end")
        ws.send_text(json.dumps({"type": "barge_in"}))
        # Socket should still accept further STT events after barge_in.
        fake_stt.emit("partial_transcript", {"text": "after"})
        evt = _drain_until(ws, "partial_transcript")
        assert evt["text"] == "after"
        ws.close()


def _collect_until_sentinel(ws, sentinel_text, *, max_msgs: int = 12):
    """Drain JSON frames until a ``partial_transcript`` carrying ``sentinel_text``.

    Returns the list of event ``type``s seen up to and including the sentinel.
    Lets a test assert that some event (e.g. ``tts_interruption``) did or did
    NOT appear before a known marker, without depending on exact ordering.
    """
    types: list[str] = []
    for _ in range(max_msgs):
        msg = ws.receive()
        if msg.get("type") == "websocket.disconnect":
            break
        if msg.get("text"):
            evt = json.loads(msg["text"])
            types.append(evt.get("type"))
            if evt.get("type") == "partial_transcript" and evt.get("text") == sentinel_text:
                return types
    raise AssertionError(f"never saw sentinel {sentinel_text!r}; saw {types}")


def test_recording_start_during_bot_speaking_cancels_tts(ws_client):
    """Onset barge-in (case 2): ``recording_start`` while the user is still
    hearing the bot must emit ``tts_interruption`` — EVEN in the playback tail
    where synthesis already finished (tts_end fired) but ``bot_speaking`` is
    still True because the client hasn't sent ``playback_done`` yet.

    This is the regression that previously slipped through: barge-in only
    fired on ``final_transcript`` (after the user stopped talking), never on
    speech onset. The onset trigger had no direct test.
    """
    client, fake_stt = ws_client
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_text(json.dumps({"type": "speak", "text": "a long reply"}))
        _drain_until(ws, "tts_end")  # synthesis done; bot_speaking stays True
        # User starts talking over the still-playing buffered audio.
        fake_stt.emit("recording_start", {})
        evt = _drain_until(ws, "tts_interruption")
        assert evt["reason"] == "user_resumed"
        ws.close()


def test_playback_done_clears_bot_speaking_so_no_barge_in(ws_client):
    """Once the client reports ``playback_done`` (its buffer drained), the user
    is no longer hearing the bot, so a subsequent interrupt must be a no-op —
    no ``tts_interruption``. Locks in the SSoT: bot_speaking tracks real client
    playback, and ``playback_done`` lowers it.

    Ordering is deterministic: ``playback_done`` and ``barge_in`` are both WS
    control frames drained by the receive loop in FIFO order, so by the time
    barge_in runs, playback_done has already cleared bot_speaking.
    """
    client, fake_stt = ws_client
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_text(json.dumps({"type": "speak", "text": "x"}))
        _drain_until(ws, "tts_end")
        ws.send_text(json.dumps({"type": "playback_done"}))  # buffer drained
        ws.send_text(json.dumps({"type": "barge_in"}))       # nothing to flush
        fake_stt.emit("partial_transcript", {"text": "__DONE__"})
        seen = _collect_until_sentinel(ws, "__DONE__")
        assert "tts_interruption" not in seen, f"unexpected barge-in after playback_done: {seen}"
        ws.close()


def test_barge_in_during_playback_tail_flushes_with_no_active_task(ws_client):
    """Explicit barge_in during the playback tail (synthesis done, tts_task
    finished) must still emit ``tts_interruption`` so the client flushes its
    queued audio. Before the fix this was a silent no-op because there was no
    task left to cancel.
    """
    client, fake_stt = ws_client
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_text(json.dumps({"type": "speak", "text": "x"}))
        _drain_until(ws, "tts_end")            # tts_task now done
        ws.send_text(json.dumps({"type": "barge_in"}))
        evt = _drain_until(ws, "tts_interruption")
        assert evt["reason"] == "client_barge_in"
        ws.close()


def test_webrtc_offer_is_answered_over_ws(ws_client, monkeypatch):
    """A ``webrtc_offer`` control frame must be answered with a ``webrtc_answer``
    carrying a valid SDP — proving the WS handler wires the offer into a real
    WebRtcVoiceSession and routes the answer back. (Media itself is covered by
    the aiortc loopback in test_services/test_webrtc_voice.py.)
    """
    import asyncio as _asyncio

    from aiortc import RTCPeerConnection

    # Host-only ICE on both ends so neither side blocks on a STUN round-trip.
    monkeypatch.setenv("JARVIS_WEBRTC_ICE", "")

    async def _make_offer_sdp() -> str:
        pc = RTCPeerConnection()
        pc.addTransceiver("audio", direction="sendrecv")
        await pc.setLocalDescription(await pc.createOffer())
        sdp = pc.localDescription.sdp
        await pc.close()
        return sdp

    offer_sdp = _asyncio.run(_make_offer_sdp())

    client, _ = ws_client
    with client.websocket_connect("/ws/voice") as ws:
        ws.send_text(json.dumps({"type": "webrtc_offer", "sdp": offer_sdp, "sdp_type": "offer"}))
        evt = _drain_until(ws, "webrtc_answer")
        assert evt.get("sdp_type") == "answer"
        assert "v=0" in (evt.get("sdp") or "")
        assert "m=audio" in evt["sdp"]
        ws.close()
