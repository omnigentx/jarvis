"""Zalo gateway — long-polling via the official Zalo Bot API.

Zalo's Bot API mirrors Telegram's shape (``{base}/bot{token}/{method}``, the
``{ok, result}`` envelope, ``sendMessage{chat_id, text}``) with two quirks:

  * ``getUpdates`` returns a *single* update object, not an array, and has no
    ``update_id`` offset cursor.
  * "no updates within the poll window" comes back as ``error_code`` 408, which
    :class:`BotApiGateway` already treats as a normal (non-error) timeout.

Token: register a bot at https://bot.zaloplatforms.com, put the token in
``fastagent.secrets.yaml`` under ``gateways.zalo.token``.

API reference: https://bot.zaloplatforms.com/docs
"""
from __future__ import annotations

import base64
import logging
from typing import List

from .base import InboundMessage
from .bot_api import BotApiGateway

logger = logging.getLogger("gateways")

_TEXT_EVENT = "message.text.received"
_IMAGE_EVENT = "message.image.received"
# Cap inbound media; Zalo image URLs are CDN links we fetch directly.
_MAX_MEDIA_BYTES = 20 * 1024 * 1024
# Zalo's CDN serves photos as ``image/jpg`` — a non-standard mime that the LLM
# vision path (and fast-agent's supported-type check) rejects. Map to the
# canonical ``image/jpeg`` so the image isn't silently dropped.
_IMAGE_MIME_NORMALIZE = {"image/jpg": "image/jpeg"}


class ZaloGateway(BotApiGateway):
    api_base = "https://bot-api.zaloplatforms.com"
    typing_method = None  # Zalo Bot API exposes no typing indicator

    def __init__(self, **kwargs) -> None:
        super().__init__(name="zalo", **kwargs)

    @staticmethod
    def _display_name(get_me_result: dict) -> str:
        """Zalo's getMe returns the bot name under ``display_name`` /
        ``account_name`` (NOT ``name``/``username`` like Telegram), e.g.
        ``{"display_name": "Bot OmnigentxJarvis", "account_name": "bot.WbZlgxnx"}``."""
        return (
            get_me_result.get("display_name")
            or get_me_result.get("account_name")
            or "bot"
        )

    async def _poll(self) -> List[InboundMessage]:
        # Zalo wants the timeout as a string and returns one update per call.
        update = await self._call("getUpdates", {"timeout": "30"})
        if not update:
            return []
        event = update.get("event_name")
        msg = update.get("message") or {}
        chat = msg.get("chat") or {}
        if "id" not in chat:
            return []
        sender = msg.get("from") or {}

        media_refs: List[dict] = []
        if event == _IMAGE_EVENT:
            # Verified against the live API: an image arrives as a public CDN URL
            # in `photo_url` (NOT `photo` as some older docs claim), with the
            # caption in `caption` (NOT `text` like Telegram).
            photo = msg.get("photo_url") or msg.get("photo")
            if isinstance(photo, str) and photo:
                media_refs.append({"url": photo, "filename": "photo.jpg"})
            text = msg.get("caption") or ""
        elif event == _TEXT_EVENT:
            text = msg.get("text") or ""
        else:
            return []  # sticker / unsupported

        if not text and not media_refs:
            return []
        return [InboundMessage(
            platform="zalo",
            chat_id=str(chat["id"]),
            user_id=str(sender.get("id", chat["id"])),
            text=text,
            raw=update,
            media_refs=media_refs,
        )]

    async def fetch_media(self, media_refs: List[dict]) -> List[dict]:
        """Download Zalo images directly from their CDN URL → base64."""
        out: List[dict] = []
        for ref in media_refs:
            url = ref.get("url")
            if not url:
                continue
            try:
                # Stream so the size cap actually BOUNDS the work: reject on the
                # Content-Length up front, and abort mid-read if the body crosses
                # the cap anyway (header missing or under-reported) — never buffer
                # an unbounded CDN response just to drop it afterwards.
                async with self._client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    declared = int(resp.headers.get("content-length") or 0)
                    if declared > _MAX_MEDIA_BYTES:
                        logger.warning("[zalo] skipping media > 20MB (content-length=%d)",
                                       declared)
                        continue
                    chunks: List[bytes] = []
                    total = 0
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > _MAX_MEDIA_BYTES:
                            break
                        chunks.append(chunk)
                    if total > _MAX_MEDIA_BYTES:
                        logger.warning("[zalo] skipping media > 20MB")
                        continue
                    ct = (resp.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
                    ct = _IMAGE_MIME_NORMALIZE.get(ct, ct) or "image/jpeg"
                    out.append({
                        "filename": ref.get("filename", "photo.jpg"),
                        "content_type": ct,
                        "data_b64": base64.b64encode(b"".join(chunks)).decode("ascii"),
                    })
            except Exception:
                logger.exception("[zalo] failed to download media url")
        return out
