"""
Pause Manager — controls pause/resume for both in-process and subprocess agents.

Architecture:
  - In-process agents: asyncio.Event per agent (cleared=paused, set=running)
  - Subprocess agents: Unix signals (SIGUSR1=pause, SIGUSR2=resume) via PID
  - State sync: ActivityStreamManager SSE broadcast + spawn_records DB update

Usage::

    from services.pause_manager import pause_manager

    pause_manager.pause("Minh - Dev")
    pause_manager.resume("Minh - Dev")
    hooks = pause_manager.create_pause_hooks("jarvis")
"""

import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Any, Optional

from fast_agent.agents.tool_runner import ToolRunnerHooks

logger = logging.getLogger(__name__)


class PauseManager:
    """Singleton managing pause state for all agents.

    For in-process agents, uses asyncio.Event (await event.wait() blocks when cleared).
    For subprocess agents, sends Unix signals (SIGUSR1/SIGUSR2) to the process.
    """

    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}
        self._paused_agents: set[str] = set()

    def _get_event(self, agent_name: str) -> asyncio.Event:
        """Get or create an asyncio.Event for the given agent (default: set = not paused)."""
        if agent_name not in self._events:
            evt = asyncio.Event()
            evt.set()  # Not paused by default
            self._events[agent_name] = evt
        return self._events[agent_name]

    def is_paused(self, agent_name: str) -> bool:
        """Check if an agent is currently paused."""
        return agent_name in self._paused_agents

    def get_all_paused(self) -> list[str]:
        """Return a list of all currently paused agent names."""
        return list(self._paused_agents)

    def pause(self, agent_name: str) -> bool:
        """Pause an agent.

        Returns True if the agent was not already paused.
        """
        if agent_name in self._paused_agents:
            logger.info("[PAUSE] Agent %s already paused", agent_name)
            return False

        self._paused_agents.add(agent_name)

        # 1. Clear in-process event (blocks hook's await event.wait())
        event = self._get_event(agent_name)
        event.clear()

        # 2. Send SIGUSR1 to subprocess (if it exists)
        pid = self._find_pid(agent_name)
        if pid:
            try:
                os.kill(pid, signal.SIGUSR1)
                logger.info("[PAUSE] Sent SIGUSR1 to %s (pid=%d)", agent_name, pid)
            except ProcessLookupError:
                logger.warning("[PAUSE] Process %d not found for %s", pid, agent_name)
            except PermissionError:
                logger.warning("[PAUSE] Permission denied sending signal to %s (pid=%d)", agent_name, pid)

        # 3. Broadcast SSE + update DB
        self._broadcast_state_change(agent_name, "paused")

        logger.info("[PAUSE] Agent %s paused", agent_name)
        return True

    def resume(self, agent_name: str) -> bool:
        """Resume a paused agent.

        Returns True if the agent was actually paused.
        """
        if agent_name not in self._paused_agents:
            logger.info("[RESUME] Agent %s not paused", agent_name)
            return False

        self._paused_agents.discard(agent_name)

        # 1. Set in-process event (unblocks hook's await event.wait())
        event = self._get_event(agent_name)
        event.set()

        # 2. Send SIGUSR2 to subprocess (if it exists)
        pid = self._find_pid(agent_name)
        if pid:
            try:
                os.kill(pid, signal.SIGUSR2)
                logger.info("[RESUME] Sent SIGUSR2 to %s (pid=%d)", agent_name, pid)
            except ProcessLookupError:
                logger.warning("[RESUME] Process %d not found for %s", pid, agent_name)
            except PermissionError:
                logger.warning("[RESUME] Permission denied sending signal to %s (pid=%d)", agent_name, pid)

        # 3. Broadcast SSE + update DB
        self._broadcast_state_change(agent_name, "resumed")

        logger.info("[RESUME] Agent %s resumed", agent_name)
        return True

    def create_pause_hooks(self, agent_name: str) -> ToolRunnerHooks:
        """Create ToolRunnerHooks that pause at checkpoints.

        The before_llm_call hook awaits the event — returns immediately if not
        paused, blocks until resume() is called if paused.
        """
        event = self._get_event(agent_name)

        async def on_before_llm_call(runner: Any, messages: Any) -> None:
            if not event.is_set():
                logger.info("[PAUSE] Agent %s blocked at LLM checkpoint", agent_name)
                await event.wait()
                logger.info("[PAUSE] Agent %s unblocked, continuing", agent_name)

        async def on_before_tool_call(runner: Any, request: Any) -> None:
            if not event.is_set():
                logger.info("[PAUSE] Agent %s blocked at tool checkpoint", agent_name)
                await event.wait()
                logger.info("[PAUSE] Agent %s unblocked, continuing", agent_name)

        return ToolRunnerHooks(
            before_llm_call=on_before_llm_call,
            before_tool_call=on_before_tool_call,
        )

    def cleanup(self, agent_name: str) -> None:
        """Remove pause state for an agent (e.g., when agent is destroyed)."""
        self._paused_agents.discard(agent_name)
        evt = self._events.pop(agent_name, None)
        if evt and not evt.is_set():
            evt.set()  # Unblock any waiting coroutine

    # ── Private helpers ──

    def _find_pid(self, agent_name: str) -> Optional[int]:
        """Find subprocess PID for an agent from spawn_records DB."""
        try:
            import services.shared_state as _state
            if _state.registry_db:
                # Use find_by_name (searches ALL records, including paused)
                # list_running() filters out paused agents, which breaks resume()
                records = _state.registry_db.find_by_name(agent_name)
                for rec in records:
                    if rec.get("pid"):
                        pid = int(rec["pid"])
                        # Verify process is alive
                        try:
                            os.kill(pid, 0)
                            return pid
                        except (ProcessLookupError, PermissionError):
                            continue
        except Exception as e:
            logger.warning("[PAUSE] DB PID lookup failed: %s", e)

        return None

    def _broadcast_state_change(self, agent_name: str, new_state: str) -> None:
        """Broadcast pause/resume state via SSE + update DB."""
        try:
            from services.activity_stream import activity_stream_manager

            event_type = "agent_paused" if new_state == "paused" else "agent_resumed"
            icon = "⏸️" if new_state == "paused" else "▶️"

            activity_stream_manager.broadcast({
                "agent_name": agent_name,
                "event_type": event_type,
                "message": f"{icon} {agent_name} {'paused' if new_state == 'paused' else 'resumed'}",
                "timestamp": time.time(),
                "data": {"status": new_state},
            })
        except Exception as e:
            logger.warning("[PAUSE] Failed to broadcast state change: %s", e)

        # Update spawn_records DB.
        # Use find_by_name (not list_running) — list_running filters status to
        # ('running', 'pending') so on the resume path the agent's row (still
        # marked 'paused' from the previous pause call) is excluded → upsert
        # is skipped → DB status stays 'paused' forever after resume. Mirrors
        # the same fix already applied to ``_find_pid`` (see line 164).
        try:
            import services.shared_state as _state
            if _state.registry_db:
                records = _state.registry_db.find_by_name(agent_name)
                for rec in records:
                    if rec.get("run_id"):
                        db_status = "paused" if new_state == "paused" else "running"
                        _state.registry_db.upsert_record(
                            rec["run_id"],
                            {"status": db_status},
                        )
                        break
        except Exception as e:
            logger.warning("[PAUSE] Failed to update DB status: %s", e)


# Module-level singleton
pause_manager = PauseManager()
