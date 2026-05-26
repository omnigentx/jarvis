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


class PauseProtected(Exception):
    """Raised by ``resume()`` when a caller-without-force tries to resume
    an agent that's pause-locked by a pending approval. The route handler
    surfaces this as HTTP 409 so the dashboard can guide the user toward
    resolving the approval (approve / reject) instead of manually resuming
    and creating a state mismatch (controller says running, approval still
    pending, subprocess blocked on approval.wait RPC).

    Carries ``agent_name`` and ``approval_id`` so the UI can deep-link to
    the offending approval without another round-trip.
    """

    def __init__(self, agent_name: str, approval_id: str) -> None:
        super().__init__(
            f"Agent {agent_name!r} is paused by pending approval "
            f"{approval_id}. Resolve the approval before resuming."
        )
        self.agent_name = agent_name
        self.approval_id = approval_id


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

        N+1 avoidance: ``_resolve_scope`` already computed the team_name
        for the whole scope. Pass it down to ``_pause_one`` so
        ``_persist_pause`` doesn't re-run a per-member ``find_by_name``
        DB query just to discover the same team affiliation (F4 in the
        PR #48 review). For solo scopes (team_name=None) the per-agent
        helper falls back to its own lookup, since the agent might
        belong to a team even though the scope was specified by name.
        """
        agents, team_name = self._resolve_scope(scope)
        if team_name:
            logger.info("[PAUSE] Team scope: %s → %d agent(s)", team_name, len(agents))
        any_changed = False
        for agent in agents:
            if self._pause_one(agent, team_name=team_name):
                any_changed = True
        return any_changed

    def resume(self, scope: str) -> bool:
        """Resume an agent or a whole team. Mirror of ``pause``.

        Pause-protection: each expanded agent is checked against pending
        approval rows. If ANY agent in scope is currently held by a
        pending approval, the whole call raises ``PauseProtected`` and
        nothing is resumed — the user must resolve the approval first.

        The check uses ``approval_requests.status='pending'`` as the
        single source of truth — no separate flag to keep in sync.
        Multi-approval correctness: when ``approval_service.resolve_approval``
        cascades resume across its ``paused_agents`` list, it catches
        ``PauseProtected`` per-agent so an agent held by a SECOND still-
        pending approval stays paused until that one is also resolved.
        """
        agents, team_name = self._resolve_scope(scope)
        if team_name:
            logger.info("[RESUME] Team scope: %s → %d agent(s)", team_name, len(agents))

        # Fail loud: refuse the entire call if ANY agent in scope is
        # currently pause-locked by an approval. Per-agent partial
        # resume would leave the user wondering "I clicked resume on
        # PM but only Dev/QE resumed" — clearer to refuse and point
        # them at the approval.
        for agent in agents:
            approval_id = self._pending_approval_for(agent)
            if approval_id:
                raise PauseProtected(agent, approval_id)

        any_changed = False
        for agent in agents:
            if self._resume_one(agent):
                any_changed = True
        return any_changed

    def _pending_approval_for(self, agent_name: str) -> Optional[str]:
        """Return the id of a pending approval whose ``paused_agents``
        list contains this agent, or None. Used by the resume guard.

        Source of truth is ``approval_requests`` — the same DB row the
        approvals API and dashboard read. No separate flag to keep in
        sync.
        """
        try:
            from core.database import SessionLocal, ApprovalRequestModel
            import json as _json
            db = SessionLocal()
            try:
                rows = db.query(ApprovalRequestModel.id, ApprovalRequestModel.paused_agents).filter(
                    ApprovalRequestModel.status == "pending",
                ).all()
            finally:
                db.close()
            for row in rows:
                paused = _json.loads(row[1] or "[]")
                if agent_name in paused:
                    return row[0]
        except Exception as e:
            # Be conservative: if DB lookup fails, ALLOW the resume rather
            # than block. The alternative (silently blocking on DB error)
            # would leave the user unable to recover without DB inspection.
            logger.warning("[RESUME] approval check failed for %s: %s", agent_name, e)
        return None

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

    def _pause_one(self, agent_name: str, team_name: Optional[str] = None) -> bool:
        """Pause a single agent. Returns True if newly paused.

        This is the original Phase 1-3 ``pause()`` body, kept private so
        Phase 4's scope-expanding ``pause()`` can call it for each agent
        in a team without re-running scope resolution recursively.

        ``team_name``: caller-supplied team affiliation. When non-None,
        ``_persist_pause`` uses it directly instead of running another
        ``find_by_name`` DB query — eliminates the N+1 noted in
        the PR #48 review (F4). For solo callers (or when the caller
        doesn't know the team), pass ``None`` and the persister falls
        back to its own lookup.
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

        # 4b. Cross-restart persistence — upsert into agent_pause_state
        # so this pause survives a backend restart. ``team_name`` is the
        # caller-resolved value (or None if solo / unknown).
        self._persist_pause(agent_name, team_name=team_name)

        # 5. Terminal ``agent_paused`` emit.
        #
        # In-process idle: emit when there's no active turn — no hook
        # will fire to do it (Phase 2 logic).
        #
        # Subprocess: emit IMMEDIATELY after SIGUSR1 was sent (don't
        # wait for subprocess hook). Rationale: the subprocess hook's
        # agent_paused is the "actually blocked at checkpoint"
        # confirmation, but it can fail to arrive — subprocess dies
        # before reaching checkpoint, spawn_events.sock drops, hook
        # exception, etc. The UI then sticks at "Pausing…" forever
        # even though backend + DB say paused (verified by reload
        # reading the DB). The subprocess emit becomes a duplicate
        # ``agent_paused`` which the FE handler idempotently no-ops.
        # Strategy B's cooperative-tool-completion is NOT affected:
        # subprocess still blocks at its next checkpoint; "Paused" in
        # UI just means "user has decided to pause" matching DB state.
        #
        # In-process active (mid-LLM): don't emit here — the
        # on_pause_cancel retry hook is responsible (after cancelling
        # the LLM task it emits agent_paused before awaiting resume).
        is_subprocess = pid is not None
        is_idle_inproc = pid is None and not self._active.get(agent_name, False)
        if is_subprocess or is_idle_inproc:
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

        # 4b. Drop the agent_pause_state row — pause is no longer active.
        self._persist_resume(agent_name)

        # 5. Terminal ``agent_resumed`` emit — same shape as pause path
        # (subprocess + in-process idle emit immediately; in-process
        # active waits for on_pause_cancel hook to fire on retry path).
        # Reason: subprocess's own resumed emit can fail to arrive
        # (sock drop, subprocess crash mid-resume) — leaving the UI
        # stuck at "Resuming…" forever even though DB says running.
        is_subprocess = pid is not None
        is_idle_inproc = pid is None and not self._active.get(agent_name, False)
        if is_subprocess or is_idle_inproc:
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
        """Mark agent as no longer pause-attached and reset its activity
        tracking.

        Does NOT undo the hook merge — that's the caller's concern
        (typically a snapshot+restore around a per-request hook
        modification). Just clears the sentinel so a subsequent
        ``attach()`` re-wires onto the restored hooks.

        Also resets ``_active`` and ``_current_tasks`` for this agent.
        Reason: those flags are flipped True by ``before_llm_call`` /
        cleared by ``after_turn_complete``. If a request ends
        abnormally (cancelled, exception before the final hook), the
        flags leak past the request. A later manual ``pause()`` then
        sees stale ``_active=True`` and skips the idle-emit branch
        waiting for a hook that will never fire → UI stuck on
        "Pausing…" forever (bug observed 2026-05-24). Resetting on
        detach ties the lifecycle to the request, not the hook chain.
        Pause state (``_paused_agents``, ``_agent_state``,
        ``_events``) is intentionally NOT reset — manual pause must
        survive request-scoped hook teardown.
        """
        if hasattr(agent, "_pause_attached"):
            try:
                delattr(agent, "_pause_attached")
            except AttributeError:
                pass
        name = getattr(agent, "name", None)
        if name:
            self._active.pop(name, None)
            self._current_tasks.pop(name, None)

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
        """Find the PID to signal for an agent.

        ``spawn_registry`` stores the PID returned by
        ``asyncio.create_subprocess_exec(["uv", "run", "python", ...])``
        — that's the **uv launcher's** PID, not the python interpreter
        running ``isolated_runner.main()`` where SIGUSR1 / SIGUSR2
        handlers are installed.

        SIGUSR1 default action is TERMINATE. uv has no handler →
        receiving SIGUSR1 kills uv → orphans the python child → entire
        subprocess agent dies. This is the 2026-05-24 "Jordan dies on
        pause" bug. Reproducer: pause any spawned subprocess, then
        check PID — it's gone.

        Fix: walk children of the registered (uv) PID to find the
        python interpreter. SIGUSR1/SIGUSR2 must go there. If we
        can't find a child (uv hasn't forked yet, race), fall back
        to the recorded PID — at least pause will fail loud (kill the
        uv launcher) instead of silently sending to the wrong target.
        """
        try:
            import services.shared_state as _state
            if _state.registry_db:
                records = _state.registry_db.find_by_name(agent_name)
                for rec in records:
                    if not rec.get("pid"):
                        continue
                    uv_pid = int(rec["pid"])
                    try:
                        os.kill(uv_pid, 0)
                    except (ProcessLookupError, PermissionError):
                        continue
                    python_pid = self._find_python_child(uv_pid)
                    if python_pid is not None:
                        return python_pid
                    # No python child found — covers two cases:
                    #   1) uv spawned a non-python binary (unlikely for
                    #      our spawner, but possible if cmd changes).
                    #   2) Pause called in the ~50ms race window between
                    #      uv launching and python forking — the agent
                    #      just spawned and the user hit pause.
                    # Returning uv_pid here re-introduces the bug this
                    # whole walk exists to prevent (SIGUSR1 → uv →
                    # TERMINATE → agent dies). Refuse instead and let
                    # the caller surface "agent still spawning, retry".
                    logger.warning(
                        "[PAUSE] uv pid=%d has no python child yet; "
                        "refusing to signal uv directly to avoid killing it",
                        uv_pid,
                    )
                    return None
        except Exception as e:
            logger.warning("[PAUSE] DB PID lookup failed: %s", e)

        return None

    @staticmethod
    def _find_python_child(uv_pid: int) -> Optional[int]:
        """Return the PID of the python interpreter forked by ``uv run``.

        ``uv run`` execs the requested binary as a child while uv stays
        the parent. We want to signal the python interpreter where the
        SIGUSR1/SIGUSR2 handlers live, not uv.

        Retries with a short backoff to cover the "agent just spawned"
        race — uv has launched but hasn't yet forked python at the
        moment the user clicks pause. Five attempts × 50ms is enough
        in practice; longer waits would feel like UI lag.

        Falls back to a non-python child only when one exists AND
        ``ps`` couldn't confirm its identity — logs a warning so the
        operator can see "signaled non-python child, may not handle
        SIGUSR1" if something downstream goes wrong.
        """
        import subprocess
        import time as _time

        for attempt in range(5):
            try:
                out = subprocess.run(
                    ["pgrep", "-P", str(uv_pid)],
                    capture_output=True, text=True, timeout=2.0,
                )
                children = [
                    int(p) for p in out.stdout.split() if p.strip().isdigit()
                ]
            except Exception as e:
                logger.warning(
                    "[PAUSE] child walk failed for uv pid=%d: %s", uv_pid, e,
                )
                return None

            for cpid in children:
                try:
                    ps = subprocess.run(
                        ["ps", "-p", str(cpid), "-o", "comm="],
                        capture_output=True, text=True, timeout=2.0,
                    )
                    comm = ps.stdout.strip().lower()
                    if "python" in comm:
                        return cpid
                except Exception:
                    continue

            if children:
                logger.warning(
                    "[PAUSE] uv pid=%d has %d non-python child(ren); "
                    "signaling first one — may not handle SIGUSR1",
                    uv_pid, len(children),
                )
                return children[0]

            # No children yet — retry after a short delay.
            if attempt < 4:
                _time.sleep(0.05)

        return None

    def _emit_sse(self, agent_name: str, event_type: str, state: str) -> None:
        """Broadcast a state-transition SSE event via activity_stream."""
        try:
            from services.activity_stream import activity_stream_manager

            # User-facing messages for each transition.
            messages = {
                "agent_pausing": f"⏸️ {agent_name} pausing…",
                "agent_paused": f"⏸️ {agent_name} paused",
                "agent_resuming": f"▶️ {agent_name} resuming…",
                "agent_resumed": f"▶️ {agent_name} resumed",
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

    def _persist_pause(self, agent_name: str, team_name: Optional[str] = None) -> None:
        """Upsert ``agent_pause_state`` row so the pause survives a
        backend restart. Best-effort — never raise (don't break a
        live pause click because of a DB hiccup).

        ``team_name``: pre-resolved hint from the scope-aware ``pause()``
        caller. When provided, skips the ``_team_of`` DB lookup — this
        avoids N+1 ``find_by_name`` queries when persisting a whole
        team. None falls back to the per-agent lookup (covers
        ``approval_service.restore_pending_on_startup`` and other
        callers that don't pre-resolve scope).
        """
        try:
            from core.database import SessionLocal, AgentPauseStateModel
            db = SessionLocal()
            try:
                row = db.query(AgentPauseStateModel).filter_by(agent_name=agent_name).first()
                resolved_team = team_name if team_name is not None else self._team_of(agent_name)
                if row is None:
                    db.add(AgentPauseStateModel(
                        agent_name=agent_name,
                        paused_at=time.time(),
                        team_name=resolved_team,
                        reason="manual",
                    ))
                else:
                    row.paused_at = time.time()
                    row.team_name = resolved_team
                db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.warning("[PAUSE] persist failed for %s: %s", agent_name, e)

    def _persist_resume(self, agent_name: str) -> None:
        """Delete the agent_pause_state row on resume."""
        try:
            from core.database import SessionLocal, AgentPauseStateModel
            db = SessionLocal()
            try:
                db.query(AgentPauseStateModel).filter_by(agent_name=agent_name).delete()
                db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.warning("[PAUSE] persist-resume failed for %s: %s", agent_name, e)

    def _team_of(self, agent_name: str) -> Optional[str]:
        """Lookup ``agent_name``'s team via spawn_records. None if solo."""
        try:
            import services.shared_state as _state
            if _state.registry_db:
                for rec in _state.registry_db.find_by_name(agent_name):
                    if rec.get("team_name"):
                        return rec["team_name"]
        except Exception:
            pass
        return None

    def restore_on_startup(self) -> int:
        """Re-apply pauses recorded in ``agent_pause_state`` so a manual
        pause (or approval-driven pause that's still pending) survives
        a backend restart. Returns the count of agents restored.

        Garbage collection: subprocess agents whose PID died with the
        previous backend leave a dangling ``agent_pause_state`` row
        that can never be resumed by SIGUSR2 (no process to signal).
        Drop those rows here so they don't accumulate across restart
        cycles and so a future spawn under the same agent_name doesn't
        inherit a stale "paused" state. In-process agents (no
        spawn_record) are always considered live — backend restart =
        Jarvis restart, so restoring its pause is correct.

        Idempotent — safe to call multiple times. Skips agents already
        in ``_paused_agents`` (e.g. if approval_service.restore ran
        first and the user paused that team's orchestrator manually
        too, we don't double-pause).
        """
        try:
            from core.database import SessionLocal, AgentPauseStateModel
            db = SessionLocal()
            try:
                rows = db.query(AgentPauseStateModel).all()
            finally:
                db.close()
        except Exception as e:
            logger.warning("[PAUSE] restore_on_startup: DB read failed: %s", e)
            return 0

        count = 0
        dropped: list[str] = []
        for row in rows:
            agent_name = row.agent_name
            if agent_name in self._paused_agents:
                continue

            # GC: if the persisted pause is orphan (in-process agent
            # whose chat task died with the backend, OR subprocess
            # whose PID is dead), drop the row instead of restoring
            # a pause we can't enforce. The user's mental model is
            # "resume = continue the work" — restoring an orphan
            # pause would resume to nothing, which they correctly
            # called out as defeating pause/resume's purpose.
            if self._is_orphan_pause(agent_name):
                self._persist_resume(agent_name)
                dropped.append(agent_name)
                continue

            # ``_pause_one`` re-runs the full pause flow (SSE + DB upsert
            # + signal). For live subprocesses the signal goes through;
            # for in-process Jarvis the asyncio.Event is recreated and
            # cleared, blocking the next checkpoint as intended.
            if self._pause_one(agent_name):
                count += 1
        if count:
            logger.info("[PAUSE] restored %d agent(s) from agent_pause_state", count)
        if dropped:
            logger.info("[PAUSE] GC'd %d dead-PID pause row(s): %s", len(dropped), dropped)
        return count

    def _is_orphan_pause(self, agent_name: str) -> bool:
        """Return True if a persisted pause for ``agent_name`` is now
        meaningless — i.e. the work it was waiting to resume no longer
        exists. Used by ``restore_on_startup`` to GC stale rows.

        Cases:
        - **In-process agent** (no spawn_record): orphan. The chat
          HTTP request that was paused is gone (backend restart killed
          it). Resume would have nothing to retry. User would see
          paused → resume → idle with no work happening — defeats the
          purpose. Drop the row.
        - **Subprocess with no live PID**: orphan. Process is gone.
          Nobody to SIGUSR2; pause state can't be enforced anyway.
        - **Subprocess with at least one live PID**: NOT orphan. The
          subprocess survived the restart with its event.wait() still
          active. Resume sends SIGUSR2 → real work continues.
        """
        try:
            import services.shared_state as _state
            if not _state.registry_db:
                return False  # can't check — be conservative
            records = _state.registry_db.find_by_name(agent_name)
            if not records:
                # In-process agent (Jarvis & friends) — chat-tied
                # pause is dead after restart.
                return True
            for rec in records:
                pid = rec.get("pid")
                if pid is None:
                    continue
                try:
                    os.kill(int(pid), 0)
                    return False  # subprocess survived
                except (ProcessLookupError, PermissionError):
                    continue
            return True
        except Exception as e:
            logger.warning("[PAUSE] _is_orphan_pause(%s) failed: %s", agent_name, e)
            return False  # conservative — keep the row

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
