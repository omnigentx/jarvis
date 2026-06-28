"""Shared transport for Telegram-style Bot APIs (Telegram, Zalo).

Both Telegram and Zalo expose nearly identical HTTP Bot APIs:

  * URL shape ``{base}/bot{token}/{method}`` with a JSON POST body.
  * Response envelope ``{"ok": bool, "result": ..., "error_code", "description"}``.
  * ``sendMessage`` takes ``{chat_id, text}``.

They differ only in:

  * base URL,
  * the long-poll model (Telegram returns an *array* of updates keyed by an
    ``update_id`` offset; Zalo returns a *single* update and signals "no
    updates" with HTTP-style ``error_code`` 408),
  * the per-update JSON field paths,
  * whether a typing indicator method exists.

``BotApiGateway`` implements everything common; a concrete platform overrides
the four hook methods below. This is why adding another Telegram-clone is ~20
lines (see :mod:`services.gateways.telegram` / :mod:`services.gateways.zalo`).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, List, Optional, Sequence

import httpx

from .base import BaseGateway, Dispatcher, InboundMessage

logger = logging.getLogger("gateways")

# Telegram/Zalo hard cap is 4096 chars; stay under it with headroom for the
# "(1/3)" style continuation prefix we add when splitting.
_MAX_CHARS = 3900
_POLL_TIMEOUT_S = 30  # long-poll: server holds the request open this long
# Pause before retrying after a benign 409 self-overlap (see run()).
_CONFLICT_RETRY_DELAY = 1.0


class BotApiError(Exception):
    """A non-ok Bot API response. ``error_code`` 408 means 'poll timed out'."""

    def __init__(self, message: str, error_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.error_code = error_code

    @property
    def is_poll_timeout(self) -> bool:
        return self.error_code == 408


def chunk_text(text: str, limit: int = _MAX_CHARS) -> List[str]:
    """Split ``text`` into platform-sized chunks, preferring line boundaries.

    Kept module-level and pure so it is trivially unit-testable without any
    network or gateway instance.
    """
    if len(text) <= limit:
        return [text]
    chunks: List[str] = []
    remaining = text
    while len(remaining) > limit:
        # Prefer to break at the last newline within the window; fall back to a
        # hard cut so a single very long line still gets delivered.
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


class BotApiGateway(BaseGateway):
    """Long-polling gateway for a Telegram-style Bot API.

    Args mirror :class:`BaseGateway` plus the platform endpoint config.
    """

    # Subclasses set these.
    api_base: str = ""
    # Method name used for the typing indicator, or None if unsupported.
    typing_method: Optional[str] = None

    def __init__(
        self,
        *,
        name: str,
        token: str,
        dispatcher: Dispatcher,
        allow_from: Optional[Sequence[Any]] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        super().__init__(name=name, dispatcher=dispatcher, allow_from=allow_from)
        self._token = token
        # Allow injection of a pre-built client for tests (no real network).
        self._client = client or httpx.AsyncClient(timeout=_POLL_TIMEOUT_S + 15)
        self._owns_client = client is None
        self._stop = asyncio.Event()
        # Live status surfaced to the Settings UI (read via GatewayManager.status).
        self.connected = False
        self.bot_username: Optional[str] = None
        self.last_error: Optional[str] = None

    # ── HTTP plumbing ─────────────────────────────────────────────────────

    def _url(self, method: str) -> str:
        return f"{self.api_base}/bot{self._token}/{method}"

    def _redact(self, msg: str) -> str:
        """Strip the bot token from any message before it is stored/shown.

        Defence in depth: error strings (httpx URLs, timeouts) can embed the
        full ``bot<token>`` URL; that token must never reach a log or the UI.
        """
        if self._token and self._token in msg:
            return msg.replace(self._token, "***")
        return msg

    async def _call(self, method: str, body: Optional[dict] = None) -> Any:
        """POST to the Bot API and unwrap the ``{ok, result}`` envelope.

        We parse the JSON envelope ourselves instead of ``raise_for_status()``:
        Telegram/Zalo return a proper ``{ok, error_code, description}`` body even
        on 4xx (e.g. 409 Conflict), so this surfaces the real reason — and,
        critically, never raises an ``httpx`` error whose message embeds the
        token-bearing URL.
        """
        resp = await self._client.post(self._url(method), json=body or {})
        try:
            data = resp.json()
        except ValueError:
            # Non-JSON error body — report the status without leaking the URL.
            raise BotApiError(f"{self.name} HTTP {resp.status_code}", error_code=resp.status_code)
        if not data.get("ok", False):
            raise BotApiError(
                data.get("description") or f"{self.name} API error: {method}",
                error_code=data.get("error_code") or resp.status_code,
            )
        return data.get("result")

    # ── Platform hooks: subclasses override ───────────────────────────────

    async def _poll(self) -> List[InboundMessage]:
        """Fetch the next batch of updates and normalize them.

        Returns an empty list on a poll timeout (no new messages).
        """
        raise NotImplementedError

    @staticmethod
    def _display_name(get_me_result: dict) -> str:
        """Human-readable bot name from a ``getMe`` result. Override per platform."""
        return (
            get_me_result.get("username")
            or get_me_result.get("name")
            or get_me_result.get("first_name")
            or "bot"
        )

    @classmethod
    async def probe(cls, token: str) -> dict:
        """Validate a token via ``getMe`` without starting the gateway.

        Returns ``{"ok": True, "name": <bot name>}`` or
        ``{"ok": False, "error": <message>}``. Used by the Test-connection
        endpoint so the user can verify a token before saving it.
        """
        def _safe(msg: str) -> str:
            return msg.replace(token, "***") if token and token in msg else msg

        client = httpx.AsyncClient(timeout=15)
        try:
            resp = await client.post(f"{cls.api_base}/bot{token}/getMe", json={})
            # Parse the envelope directly — Telegram returns {ok, description} even
            # on 401, and raise_for_status would leak the token-bearing URL.
            try:
                data = resp.json()
            except ValueError:
                return {"ok": False, "error": f"HTTP {resp.status_code}"}
            if not data.get("ok"):
                return {"ok": False, "error": _safe(data.get("description") or "invalid token")}
            return {"ok": True, "name": cls._display_name(data.get("result") or {})}
        except Exception as e:  # network/timeout — surface to the UI, token-stripped
            return {"ok": False, "error": _safe(str(e))}
        finally:
            await client.aclose()

    # ── Transport API (BaseGateway) ───────────────────────────────────────

    async def run(self) -> None:
        """Long-poll loop with exponential backoff on transient errors."""
        logger.info("[%s] gateway started (long-polling)", self.name)
        # Identify the bot up-front so the UI can show "Connected as @name"
        # immediately. A failure here (bad token) is non-fatal — the poll loop
        # below will surface the same error and keep the last_error fresh.
        try:
            result = await self._call("getMe")
            self.bot_username = self._display_name(result or {})
            self.connected = True
            self.last_error = None
            logger.info("[%s] connected as %s", self.name, self.bot_username)
        except Exception as e:
            self.connected = False
            self.last_error = self._redact(str(e))
            logger.warning("[%s] getMe failed (will keep retrying): %s", self.name, self.last_error)

        backoff = 1.0
        while not self._stop.is_set():
            try:
                messages = await self._poll()
                self.connected = True
                self.last_error = None
                backoff = 1.0  # reset after any successful poll
                for msg in messages:
                    # Serialize per-message handling; the agent itself is
                    # globally serialized downstream (session_service lock), so
                    # awaiting here keeps ordering and avoids interleaved turns.
                    await self.handle_inbound(msg)
                # Cooperative yield: real getUpdates blocks server-side for
                # ~30s, but a misbehaving (or mocked) server that returns
                # instantly on empty would otherwise spin this loop hot and
                # starve the event loop. One yield per iteration keeps the
                # scheduler fair at negligible cost.
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                break
            except BotApiError as e:
                if e.is_poll_timeout:
                    continue  # normal: no updates within the long-poll window
                if e.error_code == 409:
                    # BENIGN self-conflict. Telegram returns 409 "terminated by
                    # other getUpdates request" on the poll immediately following
                    # a successful long-poll timeout — it hasn't fully released
                    # OUR previous getUpdates yet. Verified with a single sole
                    # client: 200/409 strictly alternate (delay + no-keepalive
                    # don't change it), and every 409 is followed by a 200, so the
                    # bot stays reachable and updates still arrive on the good
                    # polls. Do NOT flip connected / set last_error (that made the
                    # UI flap red) and do NOT grow backoff — just pause briefly and
                    # retry; the next poll succeeds.
                    logger.debug("[%s] getUpdates 409 self-overlap — brief retry", self.name)
                    await self._sleep_backoff(_CONFLICT_RETRY_DELAY)
                    continue
                logger.warning("[%s] API error while polling: %s", self.name, self._redact(str(e)))
                self.connected = False
                self.last_error = self._redact(str(e))
                await self._sleep_backoff(backoff)
                backoff = min(backoff * 2, 60)
            except Exception as e:
                logger.exception("[%s] poll loop error; backing off %.0fs",
                                 self.name, backoff)
                self.connected = False
                self.last_error = self._redact(str(e))
                await self._sleep_backoff(backoff)
                backoff = min(backoff * 2, 60)
        logger.info("[%s] gateway stopped", self.name)

    async def _sleep_backoff(self, seconds: float) -> None:
        """Sleep that wakes immediately if stop() is requested."""
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def send_text(self, chat_id: str, text: str) -> None:
        for chunk in chunk_text(text):
            await self._call("sendMessage", {"chat_id": chat_id, "text": chunk})

    async def send_typing(self, chat_id: str) -> None:
        if not self.typing_method:
            return
        try:
            await self._call(self.typing_method, {"chat_id": chat_id, "action": "typing"})
        except Exception:
            # Typing is cosmetic — never let it break message handling.
            logger.debug("[%s] typing indicator failed", self.name, exc_info=True)

    async def stop(self) -> None:
        self._stop.set()
        if self._owns_client:
            await self._client.aclose()
