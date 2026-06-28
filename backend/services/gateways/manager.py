"""GatewayManager — lifecycle, agent-facing dispatch, and live config reload.

Two jobs:
  * Turn an :class:`InboundMessage` into an agent run (the single bridge to the
    runtime): resolve the chat's session, call ``resume_and_send``, persist the
    (possibly new) binding.
  * Track config: subscribe to ``config_service`` so edits made in the Settings
    UI take effect WITHOUT a restart — a burst of per-key change events is
    debounced into one stop-all → reload-config → start-all cycle.

Started/stopped from ``server.py``'s lifespan, after ``state.agent_app`` exists.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Dict, List, Optional

from . import session_map
from .base import BaseGateway, InboundMessage
from .config import GatewayConfig, load_gateway_configs
from .registry import GATEWAY_REGISTRY

logger = logging.getLogger("gateways")

# Coalesce the burst of per-key events from one bulk settings save into a single
# reload, fired this long after the last event.
_RELOAD_DEBOUNCE_S = 0.3


class GatewayManager:
    def __init__(self, agent_app) -> None:
        self.agent_app = agent_app
        self._gateways: Dict[str, BaseGateway] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._unsubscribe: Optional[Callable[[], None]] = None
        self._reload_handle: Optional[asyncio.TimerHandle] = None
        # Last config applied per platform — lets reload restart ONLY what
        # actually changed (avoids needless reconnects / transient 409s).
        self._applied: Dict[str, GatewayConfig] = {}
        # Serialize reloads. _apply() awaits _stop_one(); without this lock two
        # overlapping reloads (e.g. several quick Saves) could interleave a
        # stop and a start and ORPHAN a poller — two pollers on one bot then
        # 409 each other forever ("terminated by other getUpdates request").
        self._reload_lock = asyncio.Lock()

    # ── Agent-facing dispatch (the single bridge to the runtime) ──────────

    def _make_dispatcher(self, agent_name: str) -> Callable:
        async def dispatch(msg: InboundMessage) -> str:
            return await self._dispatch(msg, agent_name)
        return dispatch

    async def _dispatch(self, msg: InboundMessage, agent_name: str) -> str:
        """Run the agent for one message and return its reply text.

        First chance for a slash command (``/new``, ``/agent``, ``/help``) — if
        it's one, we reply to it and never touch the agent. Otherwise the
        load-bearing bit is session resolution: look up the chat's bound session
        (may be stale/None), let ``resume_and_send`` decide existence and hand
        back the authoritative id, then persist THAT — so the binding can never
        silently point at a dead session.

        The answering agent is per-chat: ``/agent`` stores a choice in the
        binding; falls back to the gateway's configured ``agent_name``.
        """
        from services.shared_state import session_service

        effective_agent = session_map.get_agent(msg.platform, msg.chat_id) or agent_name

        # Slash command? Handle and return — do not run the agent.
        reply = self._try_command(msg, effective_agent)
        if reply is not None:
            return reply

        bound_session = session_map.lookup(msg.platform, msg.chat_id)

        # Tag the turn so token usage is attributed to this chat (same
        # ContextVar the chat/voice/cron paths set before sending).
        from services.sse_progress import current_run_id
        token = current_run_id.set(f"{msg.platform}:{msg.chat_id}")
        try:
            reply, real_session = await session_service.resume_and_send(
                self.agent_app, msg.text, bound_session,
                files_data=(msg.files_data or None), agent_name=effective_agent,
            )
        finally:
            current_run_id.reset(token)

        session_map.upsert(msg.platform, msg.chat_id, real_session, effective_agent)
        return str(reply) if reply is not None else ""

    def _try_command(self, msg: InboundMessage, effective_agent: str) -> Optional[str]:
        """Run a slash command if the text is one; else return None."""
        from . import commands

        agent_names = list(getattr(self.agent_app, "_agents", {}).keys()) or ["Jarvis"]
        ctx = commands.CommandContext(
            current_agent=effective_agent,
            agent_names=agent_names,
            reset_conversation=lambda: session_map.delete(msg.platform, msg.chat_id),
            set_agent=lambda name: session_map.set_agent(msg.platform, msg.chat_id, name),
        )
        return commands.handle(msg.text, ctx)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self, configs: Optional[Dict[str, GatewayConfig]] = None) -> None:
        """Launch enabled gateways and begin watching config for live edits."""
        self._loop = asyncio.get_running_loop()
        if self._unsubscribe is None:
            from services.config_service import config_service
            self._unsubscribe = config_service.subscribe(self._on_config_change)
        configs = configs if configs is not None else load_gateway_configs()
        for name in GATEWAY_REGISTRY:
            cfg = configs.get(name, GatewayConfig())
            self._applied[name] = cfg
            if cfg.enabled:
                self._start_one(name, cfg)
        if not self._tasks:
            logger.info("No messaging gateways enabled.")

    def _running(self, name: str) -> bool:
        task = self._tasks.get(name)
        return bool(task and not task.done())

    def _start_one(self, name: str, cfg: GatewayConfig) -> None:
        # Defensive: never overwrite a live task without cancelling it, or the
        # old poller keeps running orphaned (→ self-409). Should not trigger once
        # reloads are serialized, but cheap insurance against a logic slip.
        stale = self._tasks.get(name)
        if stale and not stale.done():
            logger.warning("Gateway '%s' start requested while still running — "
                           "cancelling the stale poller first", name)
            stale.cancel()
        cls = GATEWAY_REGISTRY.get(name)
        if cls is None:
            logger.warning("Gateway '%s' is enabled but not registered; skipping", name)
            return
        if not cfg.token:
            logger.warning("Gateway '%s' is enabled but has no token; skipping", name)
            return
        if not cfg.allow_from:
            logger.warning(
                "Gateway '%s' has empty allow_from — it will ignore ALL messages "
                "until you add user ids (or [\"*\"] to allow all).", name,
            )
        gw = cls(
            token=cfg.token,
            dispatcher=self._make_dispatcher(cfg.agent),
            allow_from=cfg.allow_from,
        )
        self._gateways[name] = gw
        self._tasks[name] = asyncio.create_task(gw.run())
        logger.info("Gateway '%s' started (agent=%s)", name, cfg.agent)

    async def _stop_one(self, name: str) -> None:
        gw = self._gateways.pop(name, None)
        task = self._tasks.pop(name, None)
        if gw:
            try:
                await gw.stop()
            except Exception:
                logger.exception("Error stopping gateway '%s'", name)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Gateway '%s' task ended with error", name)

    async def stop(self) -> None:
        """Full shutdown: stop watching config and stop every gateway."""
        if self._unsubscribe:
            self._unsubscribe()
            self._unsubscribe = None
        if self._reload_handle:
            self._reload_handle.cancel()
            self._reload_handle = None
        for name in list(set(self._gateways) | set(self._tasks)):
            await self._stop_one(name)

    # ── Live config reload ────────────────────────────────────────────────

    def _on_config_change(self, event) -> None:
        """config_service listener (may run on any thread) — schedule a reload.

        Only ``gateways`` edits matter; everything else is ignored. The actual
        reload runs on the gateway event loop, debounced, so a bulk save's
        many events collapse into one restart.
        """
        if getattr(event, "category", None) != "gateways":
            return
        loop = self._loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(self._schedule_reload)

    def _schedule_reload(self) -> None:
        if self._reload_handle:
            self._reload_handle.cancel()
        self._reload_handle = self._loop.call_later(
            _RELOAD_DEBOUNCE_S, lambda: asyncio.create_task(self._reload())
        )

    async def _reload(self) -> None:
        self._reload_handle = None
        # Serialize: two overlapping _apply() runs could orphan a poller.
        async with self._reload_lock:
            await self._apply(load_gateway_configs())

    async def _apply(self, configs: Dict[str, GatewayConfig]) -> None:
        """Reconcile running gateways with the latest config, MINIMALLY.

        Restart a poller only when it must reconnect — an ``enabled`` flip or a
        token change. An allow-list / answering-agent edit is applied in place
        so a benign save never drops the long-poll connection (which would cause
        a transient 409 while the old connection is still held server-side).
        """
        for name in GATEWAY_REGISTRY:
            new = configs.get(name, GatewayConfig())
            old = self._applied.get(name, GatewayConfig())
            self._applied[name] = new

            if not new.enabled:
                if self._running(name) or name in self._gateways:
                    await self._stop_one(name)
                    logger.info("Gateway '%s' disabled — stopped", name)
                continue

            if not self._running(name):
                self._start_one(name, new)
            elif new.token != old.token:
                logger.info("Gateway '%s' token changed — reconnecting", name)
                await self._stop_one(name)
                self._start_one(name, new)
            elif new.allow_from != old.allow_from or new.agent != old.agent:
                gw = self._gateways.get(name)
                if gw is not None:
                    gw.update_runtime(
                        allow_from=new.allow_from,
                        dispatcher=self._make_dispatcher(new.agent),
                    )
                    logger.info(
                        "Gateway '%s' updated in place (allow-list/agent) — no reconnect",
                        name,
                    )

    # ── Status (for the Settings UI) ──────────────────────────────────────

    def status(self) -> List[dict]:
        """Per-platform config + live runtime status."""
        configs = load_gateway_configs()
        rows: List[dict] = []
        for name in GATEWAY_REGISTRY:
            cfg = configs.get(name, GatewayConfig())
            gw = self._gateways.get(name)
            task = self._tasks.get(name)
            rows.append({
                "platform": name,
                "enabled": cfg.enabled,
                "running": bool(task and not task.done()),
                "connected": bool(getattr(gw, "connected", False)) if gw else False,
                "bot_username": getattr(gw, "bot_username", None) if gw else None,
                "last_error": getattr(gw, "last_error", None) if gw else None,
                "agent": cfg.agent,
                "allow_count": len(cfg.allow_from),
                "has_token": bool(cfg.token),
            })
        return rows
