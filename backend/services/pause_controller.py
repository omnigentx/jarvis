"""
Pause Controller — single source of truth for agent pause/resume.

Lifecycle (Phase 3 — three-state machine + instant LLM pause via task cancel):

    running ──pause()──► pausing ──[hook at checkpoint]──► paused
       ▲                                                     │
       │                                                     │
       └──[hook resumes from await]── resuming ◄──resume()───┘

Why three states (and four SSE events)?
  - ``pausing`` means *the request was received* but the agent is still
    finishing an in-flight LLM/tool call. Frontend renders a spinner.
  - ``paused`` is broadcast *from inside the checkpoint hook*, the
    instant the agent is actually about to ``await``. This is the bug
    fix versus the previous design which broadcast ``agent_paused``
    from ``pause()`` directly — UI would say "Paused" while the agent
    was still streaming an LLM response.
  - ``resuming`` is the mirror state, emitted from ``resume()`` so the
    UI shows a "Resuming…" spinner until the hook wakes back up.
  - ``agent_resumed`` is broadcast from the hook after ``await`` returns
    — i.e. when the agent has genuinely resumed running.

Idle-state edge case::

    pause() on an agent that's not in any in-flight turn → the hook
    will never fire (until the next chat message). We track activity
    via ``_active[name]`` (set True between ``before_llm_call`` and
    ``after_turn_complete``) and emit ``agent_paused`` directly from
    ``pause()`` when ``_active`` is False. Same for resume in reverse.
    Without this the UI would show "Pausing…" indefinitely for idle
    agents, which is functionally incorrect — they ARE paused.

Public API (unchanged from Phase 1)::

    pause_controller.pause("Minh - Dev")
    pause_controller.resume("Minh - Dev")
    pause_controller.is_paused(name)        # boolean
    pause_controller.state_of(name)         # 'running'|'pausing'|'paused'|'resuming'
    hooks = pause_controller.create_pause_hooks("jarvis")

Instant pause for in-flight LLM calls
-------------------------------------
Cooperative pause (block at the next ``before_llm_call`` / ``before_tool_call``
checkpoint) leaves the agent running until the current step finishes —
acceptable for tools (typically <2s, side-effect-bearing so safer to let
finish) but painful for LLM streams (often 10-60s on long responses).

Strategy "B" from the design discussion: cancel mid-LLM-call, leave
tools alone. Implementation:

  1. The pause hook captures ``asyncio.current_task()`` in
     ``_current_tasks[agent_name]`` when ``before_llm_call`` fires.
     The same task ref is used by ``pause()`` to interrupt the LLM stream.
  2. ``pause()`` checks ``_current_tasks[name]``. If a task is registered,
     it calls ``task.cancel()`` *in addition* to clearing the event.
  3. The cancellation surfaces inside ``_tool_runner_llm_step`` as
     ``asyncio.CancelledError``. The tool_runner has a retry loop
     wrapping the LLM call: when the new ``on_pause_cancel`` hook
     (provided by this controller) returns True, the runner calls
     ``task.uncancel()`` and reissues the LLM call with the unchanged
     ``_delta_messages``.
  4. On resume, ``event.set()`` releases the on_pause_cancel hook (which
     was awaiting ``event.wait()``). The retry then proceeds with the
     same state — satisfying requirement #3 ("resume from prior state").

Tool calls are not cancelled — the ``_current_tasks`` ref is cleared on
``after_llm_call`` (i.e. before any tool-call phase begins). Pause during
tool-call falls back to cooperative blocking at the next checkpoint.

Later phases extend this module with:
  - Phase 4: team-wide pause + late-joiner hook
  - Phase 5: ``attach(agent, team_name)`` auto-wiring helper
  - Phase 6: restart recovery for manual pauses
"""

import asyncio
import logging
import os
import signal
import time
from typing import Any, Optional

from fast_agent.agents.tool_runner import ToolRunnerHooks

logger = logging.getLogger(__name__)


# Public state constants — keep stringly-typed so SSE payloads stay
# JSON-friendly without an enum→str dance.
STATE_RUNNING = "running"
STATE_PAUSING = "pausing"
STATE_PAUSED = "paused"
STATE_RESUMING = "resuming"


class PauseController:
    """Singleton managing pause state for all agents.

    For in-process agents, uses asyncio.Event (await event.wait() blocks
    when cleared). For subprocess agents, sends Unix signals
    (SIGUSR1/SIGUSR2) to the process; the subprocess runs its own
    mirror of this state machine (see ``fast_agent.spawn.pause_signal_handler``).
    """

    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}
        self._paused_agents: set[str] = set()
        # State machine — see module docstring for the transitions.
        self._agent_state: dict[str, str] = {}
        # Activity tracking — True between ``before_llm_call`` and
        # ``after_turn_complete``. Used to know whether pause/resume
        # needs to emit the terminal event itself (idle agent) or wait
        # for the hook to do it (active agent).
        self._active: dict[str, bool] = {}
        # Task ref of the in-flight LLM call. Set by the ``before_llm_call``
        # hook (captures ``asyncio.current_task()``), cleared by
        # ``after_llm_call``. ``pause()`` cancels this task to interrupt
        # long-running LLM streams — see module docstring "Instant pause".
        self._current_tasks: dict[str, asyncio.Task] = {}

    def _get_event(self, agent_name: str) -> asyncio.Event:
        """Get or create an asyncio.Event for the given agent (default: set = not paused)."""
        if agent_name not in self._events:
            evt = asyncio.Event()
            evt.set()  # Not paused by default
            self._events[agent_name] = evt
        return self._events[agent_name]

    def is_paused(self, agent_name: str) -> bool:
        """Check if an agent is currently paused (or transitioning to paused)."""
        return agent_name in self._paused_agents

    def state_of(self, agent_name: str) -> str:
        """Return current state from the machine. Defaults to ``running``."""
        return self._agent_state.get(agent_name, STATE_RUNNING)

    def get_all_paused(self) -> list[str]:
        """Return a list of all currently paused agent names."""
        return list(self._paused_agents)

    def pause(self, agent_name: str) -> bool:
        """Pause an agent.

        Transitions ``running → pausing`` immediately. If the agent has
        an in-flight LLM/tool call, the hook will transition
        ``pausing → paused`` once it reaches a checkpoint. If the agent
        is idle (no active turn), this method also emits the terminal
        ``paused`` event itself.

        Returns True if the agent was not already paused.
        """
        if agent_name in self._paused_agents:
            logger.info("[PAUSE] Agent %s already paused", agent_name)
            return False

        self._paused_agents.add(agent_name)
        self._agent_state[agent_name] = STATE_PAUSING

        # 1. Broadcast transitional state — UI renders "Pausing…" spinner.
        self._emit_sse(agent_name, "agent_pausing", STATE_PAUSING)

        # 2. Clear in-process event (blocks hook's await event.wait()).
        event = self._get_event(agent_name)
        event.clear()

        # 2b. Instant-pause: cancel the in-flight LLM call if we have a
        # registered task ref. ``_current_tasks`` is populated by the
        # ``before_llm_call`` hook and cleared by ``after_llm_call``, so a
        # ref existing here means we *are* mid-LLM (not mid-tool — strategy B
        # leaves tool calls running to completion). The cancel surfaces
        # inside ``_tool_runner_llm_step`` as CancelledError; the runner's
        # retry loop calls ``on_pause_cancel`` (defined below in
        # ``create_pause_hooks``) which awaits resume and signals retry.
        task = self._current_tasks.get(agent_name)
        if task is not None and not task.done():
            task.cancel()
            logger.info("[PAUSE] Cancelled in-flight LLM task for %s", agent_name)

        # 3. Send SIGUSR1 to subprocess (if it exists). Subprocess runs
        # its own state machine and will emit ``agent_paused`` itself.
        pid = self._find_pid(agent_name)
        if pid:
            try:
                os.kill(pid, signal.SIGUSR1)
                logger.info("[PAUSE] Sent SIGUSR1 to %s (pid=%d)", agent_name, pid)
            except ProcessLookupError:
                logger.warning("[PAUSE] Process %d not found for %s", pid, agent_name)
            except PermissionError:
                logger.warning("[PAUSE] Permission denied sending signal to %s (pid=%d)", agent_name, pid)

        # 4. Persist DB status (single 'paused' value — DB doesn't need
        # the transient pausing/resuming nuance).
        self._update_db_status(agent_name, "paused")

        # 5. Idle-agent terminal transition. Only applies to in-process
        # agents; subprocess agents emit their own paused event from
        # the subprocess hook chain. We gate on ``pid is None`` so we
        # don't double-emit for subprocess.
        if pid is None and not self._active.get(agent_name, False):
            self._agent_state[agent_name] = STATE_PAUSED
            self._emit_sse(agent_name, "agent_paused", STATE_PAUSED)

        logger.info("[PAUSE] Agent %s pausing (active=%s, subprocess=%s)",
                    agent_name, self._active.get(agent_name, False), pid is not None)
        return True

    def resume(self, agent_name: str) -> bool:
        """Resume a paused agent.

        Transitions ``paused → resuming`` immediately, then the hook
        completes the transition ``resuming → running`` when ``await
        event.wait()`` returns. For idle agents (no hook to wake),
        this method emits the terminal ``running`` event itself.

        Returns True if the agent was actually paused.
        """
        if agent_name not in self._paused_agents:
            logger.info("[RESUME] Agent %s not paused", agent_name)
            return False

        self._paused_agents.discard(agent_name)
        self._agent_state[agent_name] = STATE_RESUMING

        # 1. Broadcast transitional state — UI renders "Resuming…" spinner.
        self._emit_sse(agent_name, "agent_resuming", STATE_RESUMING)

        # 2. Set in-process event (unblocks hook's await event.wait()).
        event = self._get_event(agent_name)
        event.set()

        # 3. Send SIGUSR2 to subprocess (if it exists). Subprocess emits
        # ``agent_resumed`` from its own hook chain when the await wakes.
        pid = self._find_pid(agent_name)
        if pid:
            try:
                os.kill(pid, signal.SIGUSR2)
                logger.info("[RESUME] Sent SIGUSR2 to %s (pid=%d)", agent_name, pid)
            except ProcessLookupError:
                logger.warning("[RESUME] Process %d not found for %s", pid, agent_name)
            except PermissionError:
                logger.warning("[RESUME] Permission denied sending signal to %s (pid=%d)", agent_name, pid)

        # 4. Persist DB status back to running.
        self._update_db_status(agent_name, "running")

        # 5. Idle-agent terminal transition (mirror of pause path).
        if pid is None and not self._active.get(agent_name, False):
            self._agent_state[agent_name] = STATE_RUNNING
            self._emit_sse(agent_name, "agent_resumed", STATE_RUNNING)

        logger.info("[RESUME] Agent %s resuming (active=%s, subprocess=%s)",
                    agent_name, self._active.get(agent_name, False), pid is not None)
        return True

    def create_pause_hooks(self, agent_name: str) -> ToolRunnerHooks:
        """Create ToolRunnerHooks that drive the state machine.

        Hook responsibilities:
          - ``before_llm_call``: cooperative checkpoint + register the
            current task for instant-pause cancellation + flip ``_active``.
          - ``after_llm_call``: clear the registered task ref so a pause
            mid-tool doesn't cancel the wrong task.
          - ``before_tool_call``: cooperative checkpoint only (no cancel
            — strategy B leaves tools alone).
          - ``after_turn_complete``: flip ``_active`` to False, clean up
            task ref.
          - ``on_pause_cancel``: the LLM call inside the tool_runner just
            took CancelledError. If we initiated the cancel via ``pause()``,
            await resume and return True (the runner retries the LLM
            call with the same delta_messages). Otherwise return False
            (genuine cancel — propagate).
        """
        event = self._get_event(agent_name)

        async def on_before_llm_call(runner: Any, messages: Any) -> None:
            self._active[agent_name] = True
            # Register the running task so ``pause()`` can interrupt the
            # LLM call it's about to make. Capture happens here (rather
            # than wrapping the LLM call) because hooks are already in
            # the call site and we don't have to touch tool_runner.
            try:
                self._current_tasks[agent_name] = asyncio.current_task()
            except RuntimeError:
                pass  # not in a task context — skip cancel support
            if not event.is_set():
                self._agent_state[agent_name] = STATE_PAUSED
                self._emit_sse(agent_name, "agent_paused", STATE_PAUSED)
                logger.info("[PAUSE] Agent %s blocked at LLM checkpoint", agent_name)
                await event.wait()
                self._agent_state[agent_name] = STATE_RUNNING
                self._emit_sse(agent_name, "agent_resumed", STATE_RUNNING)
                logger.info("[PAUSE] Agent %s unblocked, continuing", agent_name)

        async def on_after_llm_call(runner: Any, message: Any) -> None:
            # LLM call finished — pause() must not cancel anything after
            # this point until the next ``before_llm_call``.
            self._current_tasks.pop(agent_name, None)

        async def on_before_tool_call(runner: Any, request: Any) -> None:
            if not event.is_set():
                self._agent_state[agent_name] = STATE_PAUSED
                self._emit_sse(agent_name, "agent_paused", STATE_PAUSED)
                logger.info("[PAUSE] Agent %s blocked at tool checkpoint", agent_name)
                await event.wait()
                self._agent_state[agent_name] = STATE_RUNNING
                self._emit_sse(agent_name, "agent_resumed", STATE_RUNNING)
                logger.info("[PAUSE] Agent %s unblocked, continuing", agent_name)

        async def on_after_turn_complete(runner: Any, message: Any) -> None:
            self._active[agent_name] = False
            self._current_tasks.pop(agent_name, None)

        async def on_pause_cancel(runner: Any) -> bool:
            """Called by tool_runner when a CancelledError surfaces inside
            the LLM call. Return True if it was our doing (= we're paused
            and the agent should wait for resume then retry the LLM call).
            """
            if not event.is_set():
                # Emit terminal paused state — the cancellation interrupted
                # the LLM call we were about to await, so this is the moment
                # the agent is genuinely "paused" rather than "pausing".
                self._agent_state[agent_name] = STATE_PAUSED
                self._emit_sse(agent_name, "agent_paused", STATE_PAUSED)
                logger.info("[PAUSE] Agent %s blocked after LLM-cancel", agent_name)
                await event.wait()
                self._agent_state[agent_name] = STATE_RUNNING
                self._emit_sse(agent_name, "agent_resumed", STATE_RUNNING)
                logger.info("[PAUSE] Agent %s unblocked, retrying LLM call", agent_name)
                return True
            return False

        return ToolRunnerHooks(
            before_llm_call=on_before_llm_call,
            after_llm_call=on_after_llm_call,
            before_tool_call=on_before_tool_call,
            after_turn_complete=on_after_turn_complete,
            on_pause_cancel=on_pause_cancel,
        )

    def cleanup(self, agent_name: str) -> None:
        """Remove pause state for an agent (e.g., when agent is destroyed)."""
        self._paused_agents.discard(agent_name)
        self._agent_state.pop(agent_name, None)
        self._active.pop(agent_name, None)
        self._current_tasks.pop(agent_name, None)
        evt = self._events.pop(agent_name, None)
        if evt and not evt.is_set():
            evt.set()  # Unblock any waiting coroutine

    # ── Private helpers ──

    def _find_pid(self, agent_name: str) -> Optional[int]:
        """Find subprocess PID for an agent from spawn_records DB."""
        try:
            import services.shared_state as _state
            if _state.registry_db:
                # Use find_by_name (searches ALL records, including paused).
                # list_running() filters out paused agents, which breaks
                # resume() — see ``_update_db_status`` for incident notes.
                records = _state.registry_db.find_by_name(agent_name)
                for rec in records:
                    if rec.get("pid"):
                        pid = int(rec["pid"])
                        try:
                            os.kill(pid, 0)
                            return pid
                        except (ProcessLookupError, PermissionError):
                            continue
        except Exception as e:
            logger.warning("[PAUSE] DB PID lookup failed: %s", e)

        return None

    def _emit_sse(self, agent_name: str, event_type: str, state: str) -> None:
        """Broadcast a state-transition SSE event via activity_stream."""
        try:
            from services.activity_stream import activity_stream_manager

            # User-facing messages for each transition.
            messages = {
                "agent_pausing": f"⏸️ {agent_name} đang tạm dừng…",
                "agent_paused": f"⏸️ {agent_name} đã tạm dừng",
                "agent_resuming": f"▶️ {agent_name} đang tiếp tục…",
                "agent_resumed": f"▶️ {agent_name} đã tiếp tục",
            }

            activity_stream_manager.broadcast({
                "agent_name": agent_name,
                "event_type": event_type,
                "message": messages.get(event_type, f"{agent_name}: {state}"),
                "timestamp": time.time(),
                "data": {"status": state},
            })
        except Exception as e:
            logger.warning("[PAUSE] Failed to broadcast SSE: %s", e)

    def _update_db_status(self, agent_name: str, db_status: str) -> None:
        """Upsert spawn_records.status. DB sees only ``paused`` / ``running`` —
        the ``pausing`` / ``resuming`` transients are SSE-only.

        Uses ``find_by_name`` (not ``list_running``) — list_running filters
        status to ``('running', 'pending')`` so on the resume path the
        agent's row (still marked 'paused' from the previous pause call)
        is excluded → upsert is skipped → DB status stays 'paused' forever
        after resume. Mirrors the same fix in ``_find_pid``.
        """
        try:
            import services.shared_state as _state
            if _state.registry_db:
                records = _state.registry_db.find_by_name(agent_name)
                for rec in records:
                    if rec.get("run_id"):
                        _state.registry_db.upsert_record(
                            rec["run_id"],
                            {"status": db_status},
                        )
                        break
        except Exception as e:
            logger.warning("[PAUSE] Failed to update DB status: %s", e)


# Module-level singleton
pause_controller = PauseController()
