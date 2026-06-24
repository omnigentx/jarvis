#!/usr/bin/env python3
"""Scan tracked source for Vietnamese text (OSS English-first check, CLAUDE.md §7).

Flags every line containing a Vietnamese-specific letter, bucketed by area. Skips
vendored trees, binaries, lockfiles, and locales/*.json (legitimately bilingual).
Use the output to drive docs/oss-i18n-backlog.md. Not all hits are bugs — see the
"KEEP" section of that backlog for legitimate Vietnamese (crawl markers, stopwords,
TTS samples, bilingual UI).

    python3 scripts/scan_vietnamese.py            # summary + prod-source lines
    python3 scripts/scan_vietnamese.py --all      # every hit, every bucket
"""
from __future__ import annotations

import collections
import os
import re
import subprocess
import sys

# Vietnamese-specific letters (precomposed + base). One match → the line is Vietnamese.
_VN = ("ăâđêôơưĂÂĐÊÔƠƯàáảãạằắẳẵặầấẩẫậèéẻẽẹềếểễệìíỉĩịòóỏõọồốổỗộờớởỡợ"
       "ùúủũụừứửữựỳýỷỹỵÀÁẢÃẠẰẮẲẴẶẦẤẨẪẬÈÉẺẼẸỀẾỂỄỆÌÍỈĨỊÒÓỎÕỌỒỐỔỖỘ"
       "ỜỚỞỠỢÙÚỦŨỤỪỨỬỮỰỲÝỶỸỴ")
_RX = re.compile("[" + _VN + "]")

_SKIP_DIR = ("backend/.venv/", "node_modules/", "backend/mcp-atlassian/",
             "backend/fast-agent/", "backend/.fast-agent/", ".git/")
_SKIP_SUF = (".lock", ".png", ".jpg", ".jpeg", ".pdf", ".ico", ".woff", ".woff2",
             ".ttf", ".webp", ".svg", ".map")


def _skip(path: str) -> bool:
    if any(s in path for s in _SKIP_DIR):
        return True
    if path.endswith(_SKIP_SUF):
        return True
    if "/locales/" in path:  # bilingual UI JSON — legit
        return True
    return False


def _bucket(path: str) -> str:
    base = os.path.basename(path)
    if ("/tests/" in path or "/test_" in path or "/fixtures/" in path
            or base.startswith("test_")
            or path.endswith((".test.js", ".test.ts", ".spec.ts"))):
        return "TEST/FIXTURE"
    if path.endswith(".md") or path.startswith("docs/"):
        return "DOCS"
    if "/scripts/" in path:
        return "SCRIPTS"
    if path.endswith((".py", ".vue", ".js", ".ts")):
        return "PROD SOURCE"
    return "CONFIG/OTHER"


def main() -> int:
    show_all = "--all" in sys.argv
    files = subprocess.check_output(["git", "ls-files"]).decode().splitlines()
    counts: collections.Counter = collections.Counter()
    hits: list[tuple[str, str, int, str]] = []  # bucket, file, lineno, text

    for path in files:
        if _skip(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                lines = fh.readlines()
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for i, line in enumerate(lines, 1):
            if _RX.search(line):
                b = _bucket(path)
                counts[b] += 1
                hits.append((b, path, i, line.rstrip()))

    print("=== Vietnamese lines by bucket ===")
    for b, c in counts.most_common():
        print(f"{c:5d}  {b}")
    print(f"\nTOTAL: {sum(counts.values())} lines / "
          f"{len({(b, f) for b, f, _, _ in hits})} files\n")

    wanted = None if show_all else {"PROD SOURCE"}
    print("=== PROD SOURCE lines ===" if not show_all else "=== all lines ===")
    for b, path, i, text in hits:
        if wanted is None or b in wanted:
            print(f"{path}:{i}: {text[:160]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
