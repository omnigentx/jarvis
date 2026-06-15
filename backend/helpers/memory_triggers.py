"""Retrieval-intent signals.

English-only by design (the repo's code language is English — CLAUDE.md §7).
Detection is still MULTILINGUAL: the primary signal is embedding similarity to
the English prototype sentences below (BAAI/bge-m3 maps every language into one
shared space, so a user writing in Vietnamese/French/Japanese still matches the
English prototypes — verified empirically). The plain-substring lexicon is only
a cheap English fast-path / degraded fallback when embeddings are unavailable;
it never needs per-language phrase lists.

Identifier regexes are language-agnostic and always apply.
"""
from __future__ import annotations

import re

# Retrieval targets (which memory kinds a signal points at).
TARGET_EPISODIC = "episodic"
TARGET_PINNED = "pinned"
TARGET_SEMANTIC = "semantic"
TARGET_PROCEDURAL = "procedural"
TARGET_COMMUNICATIONS = "communications"
TARGET_EXTERNAL = "external"   # fresh info → external provider, NOT memory

# ── Embedding prototypes (PRIMARY, multilingual via BGE-M3) ──────────────
# Canonical English sentences per intent. intent_gate.py embeds these once and
# matches the user message by cosine similarity, so detection works in ANY
# language without enumerating phrases per language.
#
# ── English fast-path / degraded fallback lexicon (substring match) ──────
# Used only when embeddings are unavailable. English-only on purpose.
_PHRASE_TARGETS: list[tuple[str, str]] = [
    ("last time", TARGET_EPISODIC), ("previously", TARGET_EPISODIC),
    ("have we ever", TARGET_EPISODIC), ("earlier you", TARGET_EPISODIC),
    ("remember", TARGET_PINNED), ("my preference", TARGET_PINNED),
    ("from now on", TARGET_PINNED), ("always use", TARGET_PINNED),
    ("usual workflow", TARGET_PROCEDURAL), ("how do we normally", TARGET_PROCEDURAL),
    ("how do we usually", TARGET_PROCEDURAL),
    ("email from", TARGET_COMMUNICATIONS), ("in the meeting", TARGET_COMMUNICATIONS),
    ("we decided", TARGET_SEMANTIC), ("the decision", TARGET_SEMANTIC),
    ("latest", TARGET_EXTERNAL), ("current price", TARGET_EXTERNAL),
    ("right now", TARGET_EXTERNAL), ("today's", TARGET_EXTERNAL),
]

# Exact-identifier patterns → BM25-first retrieval (language-agnostic).
_IDENTIFIER_RES: list[re.Pattern] = [
    re.compile(r"\b[\w./-]+\.(py|js|ts|vue|json|yaml|yml|md|sql|sh)\b"),  # file paths
    re.compile(r"\b[A-Z]{2,}-\d+\b"),                                     # TICKET-123
    re.compile(r"\b\w+\.\w+\.\w+\b"),                                     # a.b.c symbol/route
    re.compile(r"\b(error|exception|traceback|errno)\b", re.I),          # error text
    re.compile(r"\b/api/[\w/{}.-]+"),                                     # routes
    re.compile(r"`[^`]+`"),                                               # `inline code`
]


def classify_targets(query: str, *, overrides: dict | None = None) -> set[str]:
    """English substring fast-path / degraded fallback. Returns retrieval
    targets a query points at (possibly empty). The embedding gate
    (intent_gate.py) is the primary, multilingual mechanism."""
    q = (query or "").lower()
    targets: set[str] = set()
    for phrase, target in _PHRASE_TARGETS:
        if phrase in q:
            targets.add(target)
    # Optional per-deployment hints: {target: [phrases]}. Power-user escape
    # hatch; the embedding gate makes this unnecessary in normal use.
    for target, phrases in (overrides or {}).items():
        for phrase in phrases:
            if phrase.lower() in q:
                targets.add(target)
    return targets


def has_exact_identifier(query: str) -> bool:
    """True if the query contains a hard identifier that favors BM25."""
    return any(rx.search(query or "") for rx in _IDENTIFIER_RES)
