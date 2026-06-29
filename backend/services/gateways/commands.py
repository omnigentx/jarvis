"""Slash commands users can type in a gateway chat (Telegram / Zalo).

A message that starts with ``/`` and names a known command is handled here and
NOT forwarded to the agent. Unknown ``/...`` text is left alone (returns None) so
it falls through to the agent — we don't hijack legitimate slash text.

Why these three (and not ``/stop``): the gateway poll loop processes one message
at a time and blocks on the agent's reply, so it can't *receive* a ``/stop``
mid-run to interrupt — that command is unworkable in this transport and is
intentionally omitted.

To add a command: extend ``_COMMANDS`` (and ``_ALIASES``) — :func:`handle`,
``/help``, and the tests pick it up.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

# Canonical name → one-line help. Aliases map into these.
_COMMANDS = {
    "new": "start a new conversation (clears context)",
    "agent": "switch the answering agent — /agent <name>",
    "whoami": "show your user id",
    "help": "show this list",
}
_ALIASES = {"reset": "new", "clear": "new", "id": "whoami"}


@dataclass(slots=True)
class CommandContext:
    """Everything a command needs to run, supplied by the GatewayManager."""
    current_agent: str
    agent_names: List[str]
    user_id: str
    reset_conversation: Callable[[], None]   # /new
    set_agent: Callable[[str], None]         # /agent


def parse(text: str) -> Optional[Tuple[str, List[str]]]:
    """Return ``(canonical_name, args)`` for a recognized command, else None."""
    if not text or not text.startswith("/"):
        return None
    parts = text.strip().split()
    name = parts[0][1:].lower()
    name = _ALIASES.get(name, name)
    if name not in _COMMANDS:
        return None
    return name, parts[1:]


def help_text() -> str:
    lines = ["Available commands:"]
    for name, desc in _COMMANDS.items():
        lines.append(f"/{name} — {desc}")
    lines.append("(aliases: /reset, /clear = /new)")
    return "\n".join(lines)


def handle(text: str, ctx: CommandContext) -> Optional[str]:
    """Execute a slash command and return the reply, or None if ``text`` is not
    a recognized command (the caller then dispatches to the agent)."""
    parsed = parse(text)
    if parsed is None:
        return None
    name, args = parsed

    if name == "new":
        ctx.reset_conversation()
        return "🆕 Started a new conversation — previous context cleared."

    if name == "agent":
        if not args:
            return (f"Current agent: {ctx.current_agent}\n"
                    f"Usage: /agent <name>\n"
                    f"Available: {', '.join(ctx.agent_names)}")
        target = args[0]
        match = next((a for a in ctx.agent_names if a.lower() == target.lower()), None)
        if match is None:
            return (f"❌ Unknown agent '{target}'.\n"
                    f"Available: {', '.join(ctx.agent_names)}")
        ctx.set_agent(match)
        return f"✅ Now answering with agent: {match}"

    if name == "whoami":
        return f"Your user id: {ctx.user_id}"

    if name == "help":
        return help_text()

    return None  # unreachable (parse already gated), but explicit
