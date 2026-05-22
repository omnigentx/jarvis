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

Team-wide pause (Phase 4)
-------------------------
``pause(scope)`` accepts either an agent name or a team name. Resolution
goes through ``_resolve_scope``:

  - If ``scope`` is a team name (any spawn_record has that team_name),
    expand to the full membership set. Orchestrator is included because
    the template's orchestrator role is itself a member in the registry.
  - If ``scope`` is an agent name and that agent belongs to a team
    (spawn_record.team_name set), expand to the full team. Implements
    user requirement #4: pausing any member pauses the whole team.
  - Otherwise pause just the single agent (covers in-process Jarvis and
    solo spawns).

Late-joiner protection: ``spawn_progress_bridge`` consults
``is_team_paused`` when registering a new spawn record. If the team
is paused, the controller immediately pauses the joiner so it can't
slip past the pause window between spawn and first checkpoint.

Later phases extend this module with:
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

    def pause(self, scope: str) -> bool:
        """Pause an agent or a whole team.

        ``scope`` can be either an agent name or a team name. Resolution
        rules are documented in ``_resolve_scope``. Any agent in the
        resolved set goes through the same per-agent transition:
        ``running → pausing → paused`` (or idle-emit shortcut).

        Returns True if at least one agent was newly paused.
        """
        agents, team_name = self._resolve_scope(scope)
        if team_name:
            logger.info("[PAUSE] Team scope: %s → %d agent(s)", team_name, len(agents))
        any_changed = False
        for agent in agents:
            if self._pause_one(agent):
                any_changed = True
        return any_changed

    def resume(self, scope: str) -> bool:
        """Resume an agent or a whole team. Mirror of ``pause``."""
        agents, team_name = self._resolve_scope(scope)
        if team_name:
            logger.info("[RESUME] Team scope: %s → %d agent(s)", team_name, len(agents))
        any_changed = False
        for agent in agents:
            if self._resume_one(agent):
                any_changed = True
        return any_changed

    def is_team_paused(self, team_name: str) -> bool:
        """Return True if any agent in the named team is currently paused.

        Used by the late-joiner hook in ``spawn_progress_bridge`` to
        auto-pause new members spawned into a team while the team is
        paused — prevents the gap between spawn registration and first
        ``before_llm_call`` checkpoint from being a free-running window.
        """
        agents, resolved = self._resolve_scope(team_name)
        # Only valid when ``team_name`` actually expanded to a team.
        if resolved != team_name:
            return False
        return any(a in self._paused_agents for a in agents)

    def _pause_one(self, agent_name: str) -> bool:
        """Pause a single agent. Returns True if newly paused.

        This is the original Phase 1-3 ``pause()`` body, kept private so
        Phase 4's scope-expanding ``pause()`` can call it for each agent
        in a team without re-running scope resolution recursively.
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
        # leaves tool calls running to completion).
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

    def _resume_one(self, agent_name: str) -> bool:
        """Resume a single agent. Returns True if newly resumed."""
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

    def _resolve_scope(self, scope: str) -> tuple[set[str], Optional[str]]:
        """Expand ``scope`` to ``(agent_set, team_name)``.

        Resolution order:
          1. If ``scope`` matches a team_name (any spawn_record has
             ``team_name == scope``) → return all members + scope as
             team_name. Orchestrator is included because the template's
             orchestrator role is itself a member in the registry.
          2. If ``scope`` matches an agent_name and that agent belongs
             to a team (``spawn_record.team_name`` set) → return the
             full team membership PLUS the original scope (in case the
             caller refers to an agent not yet in the registry — late
             joiner) + the team_name.
          3. Otherwise (solo agent, in-process Jarvis, unknown name) →
             return ``({scope}, None)``.

        Returned ``team_name`` is ``None`` if scope is solo, allowing
        callers to distinguish "real team" from "just this agent".
        """
        try:
            import services.shared_state as _state
            if _state.registry_db is None:
                return ({scope}, None)

            # (1) scope might be a team_name
            members = _state.registry_db.find_by_team_name(scope)
            if members:
                names = {m.get("agent_name") for m in members if m.get("agent_name")}
                names.discard(None)
                return (names, scope)

            # (2) scope might be an agent in a team — look up its team_name
            records = _state.registry_db.find_by_name(scope)
            team_name: Optional[str] = None
            for rec in records:
                if rec.get("team_name"):
                    team_name = rec["team_name"]
                    break
            if team_name:
                members = _state.registry_db.find_by_team_name(team_name)
                names = {m.get("agent_name") for m in members if m.get("agent_name")}
                names.discard(None)
                # Defensive: include the original scope in case the
                # late-joiner hook calls us before the new spawn_record
                # has fully landed in the team query.
                names.add(scope)
                return (names, team_name)
        except Exception as e:
            logger.warning("[PAUSE] scope resolve failed for %r: %s", scope, e)

        # (3) Solo agent (Jarvis, ad-hoc spawn) — pause just this one.
        return ({scope}, None)

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

    def attach(self, agent: Any) -> None:
        """Auto-wire pause hooks onto ``agent.tool_runner_hooks``.

        Single entry point replacing the previously scattered "merge
        pause hooks into agent" pattern across ``routes/chat.py``,
        ``routes/inject.py``, and the subprocess hook chain. After this
        call, the agent participates in:

          - Cooperative checkpoint blocking at before_llm_call /
            before_tool_call (Phase 2 state machine).
          - Instant LLM cancel on pause via current-task tracking
            + on_pause_cancel retry contract (Phase 3).
          - Team-wide pause propagation through ``_resolve_scope`` when
            the controller's ``pause(team_name)`` is called (Phase 4).

        Idempotent: a sentinel attribute ``_pause_attached`` on the
        agent prevents double-merging if attach is called twice on the
        same agent without an intervening hook reset. Callers that
        snapshot+restore ``tool_runner_hooks`` per request (chat.py,
        inject.py) should also call ``detach(agent)`` on restore so a
        later attach takes effect again.

        ``agent.name`` is used as the controller key so subsequent
        ``pause_controller.pause(agent.name)`` and ``pause(team_name)``
        calls reach this attached agent.
        """
        if getattr(agent, "_pause_attached", False):
            return

        name = getattr(agent, "name", None)
        if not name:
            logger.warning("[PAUSE] attach(): agent has no .name attribute — skipping")
            return

        from services.sse_progress import merge_hooks

        pause_hooks = self.create_pause_hooks(name)
        existing = getattr(agent, "tool_runner_hooks", None)
        if existing is None:
            agent.tool_runner_hooks = pause_hooks
        else:
            agent.tool_runner_hooks = merge_hooks(existing, pause_hooks)

        agent._pause_attached = True
        logger.debug("[PAUSE] attached hooks for agent %s", name)

    def detach(self, agent: Any) -> None:
        """Mark agent as no longer pause-attached.

        Does NOT undo the hook merge — that's the caller's concern
        (typically a snapshot+restore around a per-request hook
        modification). Just clears the sentinel so a subsequent
        ``attach()`` re-wires onto the restored hooks.
        """
        if hasattr(agent, "_pause_attached"):
            try:
                delattr(agent, "_pause_attached")
            except AttributeError:
                pass

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
