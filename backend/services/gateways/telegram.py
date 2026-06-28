"""Telegram gateway — long-polling via the Bot API.

Telegram's ``getUpdates`` returns an *array* of updates and uses an
``update_id`` cursor: pass ``offset = max(update_id) + 1`` so each update is
delivered exactly once and never replayed after a restart hiccup.

Handles text and photos (incl. images sent as documents). A photo's caption
becomes the message text, and the largest available size is downloaded and
handed to the agent as multimodal input.

Token: create a bot with @BotFather, put the token in ``fastagent.secrets.yaml``
under ``gateways.telegram.token`` (see the example file).
"""
from __future__ import annotations

import base64
import logging
from typing import List, Optional

from .base import InboundMessage
from .bot_api import BotApiGateway

logger = logging.getLogger("gateways")

# getFile works for files up to 20 MB; skip anything larger than this rather
# than attempt a download that the Bot API will reject.
_MAX_MEDIA_BYTES = 20 * 1024 * 1024


class TelegramGateway(BotApiGateway):
    api_base = "https://api.telegram.org"
    typing_method = "sendChatAction"

    def __init__(self, **kwargs) -> None:
        super().__init__(name="telegram", **kwargs)
        self._offset = 0  # update_id cursor; 0 means "from the beginning"

    async def _poll(self) -> List[InboundMessage]:
        updates = await self._call(
            "getUpdates",
            {"offset": self._offset, "timeout": 30, "allowed_updates": ["message"]},
        )
        messages: List[InboundMessage] = []
        for upd in updates or []:
            # Always advance the cursor, even for updates we skip (stickers,
            # edited messages, …) — otherwise getUpdates replays them forever.
            self._offset = max(self._offset, int(upd["update_id"]) + 1)
            inbound = self._to_inbound(upd)
            if inbound is not None:
                messages.append(inbound)
        return messages

    def _to_inbound(self, upd: dict) -> Optional[InboundMessage]:
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        if "id" not in chat:
            return None
        sender = msg.get("from") or {}

        # A photo message carries `caption` (not `text`) + a `photo` array of
        # sizes; the last entry is the largest. An image can also arrive as a
        # `document` with an image mime type.
        media_refs: List[dict] = []
        photos = msg.get("photo") or []
        if photos:
            largest = photos[-1]
            media_refs.append({
                "file_id": largest["file_id"],
                "content_type": "image/jpeg",  # Telegram re-encodes photos to JPEG
                "filename": "photo.jpg",
                "file_size": largest.get("file_size", 0),
            })
        doc = msg.get("document") or {}
        if doc and str(doc.get("mime_type", "")).startswith("image/"):
            media_refs.append({
                "file_id": doc["file_id"],
                "content_type": doc["mime_type"],
                "filename": doc.get("file_name") or "image",
                "file_size": doc.get("file_size", 0),
            })

        text = msg.get("text") or msg.get("caption") or ""
        # Nothing we can act on (no text, no image) → skip (sticker, location…).
        if not text and not media_refs:
            return None

        return InboundMessage(
            platform="telegram",
            chat_id=str(chat["id"]),
            user_id=str(sender.get("id", chat["id"])),
            text=text,
            raw=upd,
            media_refs=media_refs,
        )

    async def fetch_media(self, media_refs: List[dict]) -> List[dict]:
        """Resolve Telegram file_ids to base64 bytes via getFile + file download."""
        out: List[dict] = []
        for ref in media_refs:
            if ref.get("file_size", 0) and ref["file_size"] > _MAX_MEDIA_BYTES:
                logger.warning("[telegram] skipping media > 20MB (%s bytes)", ref["file_size"])
                continue
            try:
                info = await self._call("getFile", {"file_id": ref["file_id"]})
                file_path = (info or {}).get("file_path")
                if not file_path:
                    continue
                # File download uses a DIFFERENT path: /file/bot<token>/<file_path>
                url = f"{self.api_base}/file/bot{self._token}/{file_path}"
                resp = await self._client.get(url)
                resp.raise_for_status()
                out.append({
                    "filename": ref["filename"],
                    "content_type": ref["content_type"],
                    "data_b64": base64.b64encode(resp.content).decode("ascii"),
                })
            except Exception:
                logger.exception("[telegram] failed to download media file_id=%s",
                                 ref.get("file_id"))
        return out
