"""Canonical agent-identity helpers.

Agents are identified across Jarvis by their NAME (there is no separate
agent UUID — see the schema note in ``core/database.py``). The durable
memory subsystem keys every record on the *normalized* agent name, so the
normalization rule must have one authoritative home rather than being copied
per call site.

``normalize_agent_name`` historically lived in ``services/sse_progress.py``
(every SSE progress event flows through it). It is moved here so the spawn
subsystem, the memory services, and the progress hooks all share one
definition. ``sse_progress`` re-exports it for backward compatibility.

Identity rule recap:
  - instance suffixes like ``[1]`` / ``[2]`` are display-only and stripped;
  - a team-role suffix like ``[SA]`` is part of the agent's full identity
    and is KEPT (``"Khoi [SA]"`` is one agent, ``"Khoi [SA][1]"`` is the
    same agent shown as instance 1).
"""

import re

# Trailing instance suffix only: ``[<digits>]`` at end of string.
_INSTANCE_SUFFIX_RE = re.compile(r"\[\d+\]$")


def normalize_agent_name(name: str) -> str:
    """Strip a trailing instance suffix like ``[1]`` / ``[2]``.

    e.g. ``'FinanceAgent[1]'`` -> ``'FinanceAgent'``,
         ``'Khoi [SA][1]'``    -> ``'Khoi [SA]'`` (role suffix preserved),
         ``'Khoi [SA]'``       -> ``'Khoi [SA]'`` (unchanged).

    This keeps Activity Stream ``agent_name`` aligned with REST API names
    and is the canonical owner key for durable memory.
    """
    return _INSTANCE_SUFFIX_RE.sub("", name)
