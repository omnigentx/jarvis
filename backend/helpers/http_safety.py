"""HTTP fetch helpers with safety bounds.

Used by the story crawl loops where each iteration pulls one chapter
from an arbitrary URL. Without a body cap a hostile (or just bloated)
chapter page can stream gigabytes into RAM + disk; 2000 such chapters
in a single crawl job is the disk-fill scenario.
"""

from __future__ import annotations

import logging
import requests

logger = logging.getLogger(__name__)

# Per-response caps. 10 MB is comfortable headroom for any text chapter
# observed in practice (Vietnamese fiction chapters land at 5-50 KB).
MAX_CHAPTER_BYTES = 10 * 1024 * 1024
WARN_CHAPTER_BYTES = 5 * 1024 * 1024


def get_capped_text(
    url: str,
    *,
    headers: dict | None = None,
    timeout=10,
    max_bytes: int = MAX_CHAPTER_BYTES,
    warn_bytes: int = WARN_CHAPTER_BYTES,
) -> tuple[int, str]:
    """GET ``url`` with a streamed body + byte cap.

    Returns ``(status_code, text)``. Lets the caller handle 429 / non-200
    the same way they would after ``requests.get`` — only the body read
    is changed: streamed, byte-capped, decoded to ``str``.

    * Bytes above ``warn_bytes``: emit a warning, keep reading.
    * Bytes above ``max_bytes``: stop reading; the partial body that fit
      under the cap is returned. The caller treats it like a short page.
    """
    resp = requests.get(url, headers=headers or {}, timeout=timeout, stream=True)
    try:
        chunks: list[bytes] = []
        total = 0
        warned = False
        for chunk in resp.iter_content(8192):
            if not chunk:
                continue
            total += len(chunk)
            if not warned and total > warn_bytes:
                logger.warning(
                    "Response from %s exceeded %d bytes — will cap at %d",
                    url[:80], warn_bytes, max_bytes,
                )
                warned = True
            if total > max_bytes:
                logger.warning(
                    "Response from %s truncated at %d bytes",
                    url[:80], max_bytes,
                )
                break
            chunks.append(chunk)
        encoding = resp.encoding or resp.apparent_encoding or "utf-8"
        return resp.status_code, b"".join(chunks).decode(encoding, errors="replace")
    finally:
        resp.close()
