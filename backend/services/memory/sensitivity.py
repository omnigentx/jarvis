"""Secret/sensitivity detection (spec §13, §21). Run BEFORE persistence and
embedding so secrets never land in memory or the search index. Pure functions.
"""
from __future__ import annotations

import re

from services.memory.models import Sensitivity

# High-signal secret patterns. Conservative — false positives only force an
# approval, they don't lose data.
_SECRET_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),               # OpenAI-style
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                  # AWS access key id
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),              # GitHub PAT
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"),           # Google API key
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),      # Slack token
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),  # JWT
    re.compile(r"(?i)\b(?:password|passwd|secret|api[_-]?key|token)\s*[:=]\s*\S{6,}"),
]

# Personal data (PII) → the SENSITIVE tier (below SECRET): the memory is still
# persisted, but it can never be auto-pinned and the UI can flag it. Patterns
# are deliberately conservative — a false positive only forces approval / blocks
# auto-pin, it never loses data, and a false negative (PII recalled into a
# prompt) is the failure we most want to avoid. Local-format phone numbers (no
# country code) are intentionally NOT matched to keep false positives near zero.
_PII_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),   # email
    re.compile(r"\+\d[\d .()-]{7,}\d"),                                   # intl phone (+country)
    re.compile(r"\b\d{4}[ -]\d{4}[ -]\d{4}[ -]\d{4}\b"),                  # 16-digit card (grouped)
]


def detect_secrets(text: str) -> list[str]:
    """Return the matched secret-like substrings (possibly empty)."""
    found: list[str] = []
    for rx in _SECRET_PATTERNS:
        found.extend(m.group(0) for m in rx.finditer(text or ""))
    return found


def has_secret(text: str) -> bool:
    return bool(detect_secrets(text))


def detect_pii(text: str) -> list[str]:
    """Return matched PII substrings (email / intl phone / grouped card)."""
    found: list[str] = []
    for rx in _PII_PATTERNS:
        found.extend(m.group(0) for m in rx.finditer(text or ""))
    return found


def has_pii(text: str) -> bool:
    return bool(detect_pii(text))


def classify_sensitivity(text: str) -> str:
    """Coarse sensitivity label. SECRET (credentials) outranks SENSITIVE (PII)
    outranks NORMAL."""
    if has_secret(text):
        return Sensitivity.SECRET.value
    if has_pii(text):
        return Sensitivity.SENSITIVE.value
    return Sensitivity.NORMAL.value
