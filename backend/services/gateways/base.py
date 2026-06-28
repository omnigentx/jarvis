"""Gateway abstraction — connect external messaging platforms to Jarvis.

A *gateway* bridges one external chat platform (Telegram, Zalo, …) to the
agent runtime. The design splits two concerns so adding a platform is cheap
and the agent-facing logic lives in exactly one place:

  * **Transport** (subclass responsibility): how to *receive* messages from
    the platform (``run``) and how to *send* a reply (``send_text``). This is
    the only part that differs per platform.

  * **Orchestration** (this base class, ``handle_inbound``): allow-list gate →
    typing indicator → hand the message to the agent via the injected
    ``dispatcher`` → deliver the reply → on failure, log loudly *and* tell the
    user (never a silent swallow).

To add a new platform: subclass :class:`BaseGateway` (or
:class:`~services.gateways.bot_api.BotApiGateway` for any Telegram-style Bot
API), implement the transport methods, and register it in
``services.gateways.registry``. Nothing else in the codebase changes.
"""
from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, List, Optional, Sequence

logger = logging.getLogger("gateways")

# A dispatcher runs the agent for one inbound message and returns the reply
# text. The GatewayManager supplies the real implementation (wired to
# session_service); gateways stay ignorant of how the agent is run.
Dispatcher = Callable[["InboundMessage"], Awaitable[str]]


@dataclass(slots=True)
class InboundMessage:
    """A platform message normalized to the minimal shape the agent needs.

    ``chat_id`` and ``user_id`` are kept as strings so every platform (Telegram
    uses ints, Zalo uses strings) shares one representation — the session map
    and allow-list compare strings, never mixed types.
    """
    platform: str
    chat_id: str
    user_id: str
    text: str
    raw: Any = field(default=None, repr=False)  # original payload, for debugging
    # Platform-specific handles for attached media (e.g. Telegram file_ids),
    # resolved to bytes by the gateway's fetch_media() AFTER the allow-list gate
    # so we never download for unauthorized senders.
    media_refs: List[dict] = field(default_factory=list)
    # Downloaded media, ready for the agent: [{filename, content_type, data_b64}].
    files_data: List[dict] = field(default_factory=list)


class BaseGateway(abc.ABC):
    """Abstract base for a messaging gateway.

    Subclasses implement the transport (:meth:`run`, :meth:`send_text`, and
    optionally :meth:`send_typing`). The orchestration in
    :meth:`handle_inbound` is shared and must not be overridden.

    Args:
        name: platform identifier (``"telegram"``, ``"zalo"``).
        dispatcher: async callable that runs the agent and returns reply text.
        allow_from: user ids permitted to drive the agent. ``["*"]`` opts in to
            allowing everyone (explicit and visible in config); an empty list
            denies all (safe default — an unconfigured token cannot be abused).
    """

    def __init__(
        self,
        *,
        name: str,
        dispatcher: Dispatcher,
        allow_from: Optional[Sequence[Any]] = None,
    ) -> None:
        self.name = name
        self._dispatcher = dispatcher
        # Normalize to strings once so membership tests never compare int vs str.
        self._allow_from = {str(u) for u in (allow_from or [])}
        self._open_to_all = "*" in self._allow_from

    # ── Transport: subclasses MUST implement ──────────────────────────────

    @abc.abstractmethod
    async def run(self) -> None:
        """Long-running receive loop. Must exit cleanly on ``CancelledError``."""

    @abc.abstractmethod
    async def send_text(self, chat_id: str, text: str) -> None:
        """Deliver ``text`` to ``chat_id`` on the platform."""

    async def send_typing(self, chat_id: str) -> None:
        """Optional 'typing…' indicator. No-op unless the platform supports it."""
        return None

    async def stop(self) -> None:
        """Release transport resources. Default no-op — the manager also cancels
        the ``run`` task, so a gateway with nothing to close needs no override."""
        return None

    def update_runtime(self, *, allow_from: Optional[Sequence[Any]], dispatcher: Dispatcher) -> None:
        """Apply allow-list / dispatcher changes to a RUNNING gateway in place.

        Lets the manager honour an allow-list or answering-agent edit without
        tearing down and reconnecting the transport — which, for long-polling
        Bot APIs, would otherwise trigger a transient 409 while the old
        connection is still held server-side.
        """
        self._allow_from = {str(u) for u in (allow_from or [])}
        self._open_to_all = "*" in self._allow_from
        self._dispatcher = dispatcher

    async def fetch_media(self, media_refs: List[dict]) -> List[dict]:
        """Download attached media into agent-ready ``files_data`` dicts.

        Default: no media support (returns nothing). Platforms that carry media
        (Telegram photos, …) override this to resolve their refs to bytes. Called
        from :meth:`handle_inbound` AFTER the allow-list gate.
        """
        return []

    # ── Orchestration: shared, do NOT override ────────────────────────────

    def is_allowed(self, user_id: str) -> bool:
        """Allow-list gate. Empty list ⇒ deny all; ``"*"`` ⇒ allow all."""
        return self._open_to_all or str(user_id) in self._allow_from

    async def handle_inbound(self, msg: InboundMessage) -> None:
        """Run one message end-to-end: gate → typing → agent → reply.

        Errors are logged with a traceback AND surfaced to the user as a short
        message — the receive loop keeps running (one bad turn must not kill the
        gateway), but the failure is never swallowed silently.
        """
        if not self.is_allowed(msg.user_id):
            # Not an error — an unauthorized sender. Log at info so an operator
            # can see who to add to allow_from, but do not reply (avoid leaking
            # the bot's existence / inviting probing).
            logger.info(
                "[%s] ignored message from unauthorized user_id=%s chat_id=%s",
                self.name, msg.user_id, msg.chat_id,
            )
            return

        try:
            await self.send_typing(msg.chat_id)
            # Resolve any attached media to bytes (post allow-gate). A media
            # failure must not drop the whole message — fall back to text-only.
            if msg.media_refs:
                try:
                    msg.files_data = await self.fetch_media(msg.media_refs)
                except Exception:
                    logger.exception("[%s] media fetch failed chat_id=%s — "
                                     "continuing text-only", self.name, msg.chat_id)
            reply = await self._dispatcher(msg)
            if reply and reply.strip():
                await self.send_text(msg.chat_id, reply)
            else:
                # The agent returned nothing — tell the user rather than leave
                # them staring at a silent chat.
                await self.send_text(msg.chat_id, "(no response)")
        except Exception:
            logger.exception(
                "[%s] failed handling message chat_id=%s", self.name, msg.chat_id
            )
            # Best-effort user-facing error; if even this send fails it is logged
            # by send_text's own error path, so nothing is hidden.
            try:
                await self.send_text(
                    msg.chat_id,
                    "⚠️ Something went wrong handling your message. Please try again.",
                )
            except Exception:
                logger.exception(
                    "[%s] failed to deliver error notice chat_id=%s",
                    self.name, msg.chat_id,
                )
