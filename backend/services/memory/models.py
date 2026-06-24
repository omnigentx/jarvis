"""Memory domain enums and validation — ONE definition shared by the write
and read sides (no string literals scattered across services)."""
from __future__ import annotations

import re
from enum import Enum


class MemoryType(str, Enum):
    """Durable memory kinds. Working context is NOT a stored type."""
    PINNED = "pinned"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"


class MemoryStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"
    EXPIRED = "expired"
    DELETED = "deleted"
    PENDING_APPROVAL = "pending_approval"


class Authority(str, Enum):
    """Source authority. Assistant statements alone are never sufficient."""
    TOOL_VERIFIED = "tool_verified"
    USER_CONFIRMED = "user_confirmed"
    AGENT_OBSERVED = "agent_observed"
    REPORTED_BY_AGENT = "reported_by_agent"
    EXTERNAL_DOCUMENT = "external_document"
    INFERRED = "inferred"


class Sensitivity(str, Enum):
    NORMAL = "normal"
    SENSITIVE = "sensitive"
    SECRET = "secret"


class CandidateStatus(str, Enum):
    """``memory_candidates.status`` — the ONE authoritative candidate state."""
    PENDING = "pending"
    AUTO_APPROVED = "auto_approved"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


# ``inferred`` memory can never be pinned nor auto-promoted to a Skill
# (spec §14). Enforced in MemoryService.
PIN_FORBIDDEN_AUTHORITIES: frozenset[str] = frozenset({Authority.INFERRED.value})


# ──────────────────────────────────────────────────────────────────────
# subject_scope controlled taxonomy: user | project:<name> | agent:<name>
# | system. Free-form scopes are REJECTED (raise) — never silently
# normalized — so retrieval filters stay coherent.
# ──────────────────────────────────────────────────────────────────────

_SCOPE_BARE = frozenset({"user", "system"})
# project:foo / agent:Bar — name may contain internal spaces (agent names
# like "Riley [SA]") but must not be empty or have leading/trailing space.
_SCOPE_PREFIXED_RE = re.compile(r"^(project|agent):(\S(?:.*\S)?)$")


def validate_subject_scope(scope: str) -> str:
    """Return ``scope`` unchanged if valid; raise ``ValueError`` otherwise."""
    if scope in _SCOPE_BARE:
        return scope
    if _SCOPE_PREFIXED_RE.match(scope):
        return scope
    raise ValueError(
        f"invalid subject_scope {scope!r}: must be one of 'user', 'system', "
        f"'project:<name>', or 'agent:<name>'"
    )


def is_valid_subject_scope(scope: str) -> bool:
    try:
        validate_subject_scope(scope)
        return True
    except ValueError:
        return False
