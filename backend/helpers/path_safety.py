"""Path-safety helpers — small, focused traversal guards.

The story crawl / TTS-pregen pipeline accepts directory and filename
components that originate from web-scraped page titles or DB rows
populated by LLM tool calls. Anything that joins those into a path
must guard against ``..`` and absolute paths sneaking through.

``safe_story_path`` is the one helper everyone should use. It:

* rejects empty / non-string parts,
* rejects ``.`` and ``..`` exactly (the dot-traversal escape),
* rejects any part containing a path separator (``/`` or ``\\``),
* resolves the final candidate path and re-asserts it stays under
  the base directory — catches symlink games and other edge cases
  the simple checks above miss.

Vietnamese diacritics and other Unicode are preserved. Callers that
want a *cosmetic* clean (e.g. strip ``?`` and ``*`` for Windows
filesystems) should run their own ``re.sub`` BEFORE calling this
helper; it is intentionally not opinionated about presentation.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Union

_BAD_CHARS = {"/", "\\", "\x00"}


def safe_story_path(base: Union[Path, str], *parts: str) -> Path:
    """Safely join ``parts`` under ``base``.

    Returns the resolved :class:`~pathlib.Path` on success.

    Raises :class:`ValueError` when any part is empty, ``.``, ``..``,
    contains a path separator, or when the resolved candidate escapes
    ``base`` (e.g. via symlink).
    """
    base_path = Path(base).resolve()

    for p in parts:
        if not isinstance(p, str) or not p:
            raise ValueError(f"Empty or non-string path component: {p!r}")
        if p in (".", ".."):
            raise ValueError(f"Path traversal component: {p!r}")
        if any(c in p for c in _BAD_CHARS):
            raise ValueError(f"Path separator in component: {p!r}")

    candidate = base_path.joinpath(*parts).resolve()
    # Allow equality (parts == empty after rejection above, would not reach
    # here) only on the base itself — and in practice we always have ≥1 part.
    if base_path != candidate and base_path not in candidate.parents:
        raise ValueError(f"Path escapes sandbox: {candidate} not under {base_path}")

    return candidate
