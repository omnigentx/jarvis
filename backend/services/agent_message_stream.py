"""Stream agent.message_history deltas as `message_turn` SSE events.

Source of truth for the Team Monitor v2 UI. Each turn an LLM/tool call
appends to ``agent.message_history`` (maintained by fast-agent at
``llm_decorator._persist_history``), this module forwards the new
``PromptMessageExtended`` (one event per turn) to the activity stream.

Each agent object carries a small integer cursor so we know what's new.
Large text blocks (assistant content, tool_results) are truncated for
the live event; the UI fetches the full payload on demand from
``GET /api/agents/{name}/turns/{turn_idx}/full`` (see ``routes/agents``).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Per-agent cursor lives on the agent object so it survives across
# request-scoped hook factory recreations.
HISTORY_CURSOR_ATTR = "_message_stream_cursor"

# Cap a single text block at 16 KiB so a 140 KiB serpapi blob doesn't
# blow up SSE chunk size or the browser DOM. UI fetches full content
# via the per-turn endpoint when the user clicks "Show full".
MAX_BLOCK_TEXT_BYTES = 16 * 1024

# Per-agent ring buffer of broadcast turns. We must keep our own copy
# because some agents (e.g. ``FinanceAgent`` invoked via Jarvis's
# ``agent__FinanceAgent`` tool) run on a CLONE that is discarded after
# the call — see ``agents_as_tools_agent._invoke_child_agent``. The
# persistent ``agent_app._agents[name]`` template never observes those
# turns, so a page-load fetch of ``/messages`` would otherwise return
# empty even though events were broadcast live moments earlier.
#
# Keys are normalized agent names (e.g. ``FinanceAgent``, not
# ``FinanceAgent[1]``). Values are full untruncated PromptMessageExtended
# dumps in turn_idx order (no gaps within one run; later runs reset
# turn_idx to 0 and overwrite earlier entries). Capped so memory stays
# bounded across many runs.
_RECENT_TURNS_PER_AGENT_CAP = 200
_recent_turns: dict[str, list[dict]] = {}


def _truncate_block(block: Any) -> Any:
    """Truncate a content block in place if its text exceeds the cap.

    Pydantic-dumped blocks are plain dicts. Non-text blocks (image, etc.)
    are returned unchanged.
    """
    if not isinstance(block, dict):
        return block
    text = block.get("text")
    if not isinstance(text, str):
        return block
    encoded = text.encode("utf-8", errors="ignore")
    if len(encoded) <= MAX_BLOCK_TEXT_BYTES:
        return block
    block["text"] = encoded[:MAX_BLOCK_TEXT_BYTES].decode("utf-8", errors="ignore")
    block["_truncated"] = True
    block["_full_size"] = len(encoded)
    return block


def trim_message_for_stream(payload: dict) -> dict:
    """Mutate ``payload`` in place to cap large text blocks. Returns it.

    Applies to: top-level ``content`` blocks, plus each
    ``tool_results[*].content`` block. ``tool_calls`` arguments are left
    alone (they're parameters, normally small; redaction is the caller's
    job).
    """
    for block in payload.get("content") or []:
        _truncate_block(block)

    for _tid, result in (payload.get("tool_results") or {}).items():
        if not isinstance(result, dict):
            continue
        for block in result.get("content") or []:
            _truncate_block(block)

    return payload


def _record_recent_turn(agent_name: str, turn_idx: int, full_payload: dict) -> None:
    """Cache the FULL (untruncated) turn dump in the per-agent ring buffer.

    Keeps the latest ``_RECENT_TURNS_PER_AGENT_CAP`` entries. Replaces by
    turn_idx so re-runs (turn_idx resets to 0) overwrite earlier slots.
    """
    bucket = _recent_turns.get(agent_name)
    if bucket is None:
        bucket = []
        _recent_turns[agent_name] = bucket
    # Replace existing slot if present, else insert in sorted order.
    for i, existing in enumerate(bucket):
        if existing.get("turn_idx") == turn_idx:
            bucket[i] = {"turn_idx": turn_idx, "message": full_payload}
            return
    bucket.append({"turn_idx": turn_idx, "message": full_payload})
    # Bound size — drop oldest when above cap.
    if len(bucket) > _RECENT_TURNS_PER_AGENT_CAP:
        del bucket[: len(bucket) - _RECENT_TURNS_PER_AGENT_CAP]


def get_recent_turns(agent_name: str) -> list[dict]:
    """Return cached turns for an agent (read-only snapshot)."""
    return list(_recent_turns.get(agent_name) or [])


def reset_recent_turns(agent_name: str | None = None) -> None:
    """Test helper: clear the cache (one agent or all)."""
    if agent_name is None:
        _recent_turns.clear()
    else:
        _recent_turns.pop(agent_name, None)


def emit_message_history_delta(agent, agent_name: str, run_id: str | None) -> int:
    """Broadcast each new ``PromptMessageExtended`` in ``agent.message_history``.

    Returns the number of turns emitted (useful for tests/observability).
    Resets cursor to 0 if history was cleared (e.g. /clear command) —
    new turns are emitted starting at 0 again.
    """
    try:
        history = list(agent.message_history)
    except Exception as exc:
        logger.debug("[message_stream] history not accessible on %s: %s", agent_name, exc)
        return 0

    cursor = getattr(agent, HISTORY_CURSOR_ATTR, 0)
    if cursor > len(history):
        cursor = 0  # history was cleared

    if cursor >= len(history):
        return 0

    # Lazy import to avoid circular dependency at module load.
    from services.activity_stream import activity_stream_manager

    emitted = 0
    for idx in range(cursor, len(history)):
        msg = history[idx]
        try:
            full = msg.model_dump(mode="json", exclude_none=True)
        except Exception as exc:
            logger.warning(
                "[message_stream] model_dump failed for %s turn %d: %s",
                agent_name, idx, exc,
            )
            continue

        # Cache the FULL (untruncated) payload so the read endpoints
        # ``/messages`` and ``/turns/{idx}/full`` can still serve it
        # after the live agent (often a discarded clone) goes away.
        _record_recent_turn(agent_name, idx, full)

        # The broadcast version is trimmed for SSE size.
        # ``trim_message_for_stream`` mutates a copy so the cache stays full.
        trimmed = trim_message_for_stream(json.loads(json.dumps(full)))

        activity_stream_manager.broadcast({
            "agent_name": agent_name,
            "event_type": "message_turn",
            "run_id": run_id,
            "timestamp": time.time(),
            "data": {
                "turn_idx": idx,
                "role": trimmed.get("role"),
                "message": trimmed,
            },
        })
        emitted += 1

    setattr(agent, HISTORY_CURSOR_ATTR, len(history))
    return emitted


# ── Read-side helpers used by the messages REST endpoints ──


def _resolve_agent_history(agent_name: str) -> list:
    """Return the agent's PromptMessageExtended list, live or from snapshot.

    Resolution order:
      1. Live AgentApp runtime (``state.agent_app``) — in-process agents,
         freshest data including the turn that just completed.
      2. Latest ``agent_context_snapshots`` row — subprocess agents and
         in-process agents that have shut down.
    Returns ``[]`` if neither source has data.
    """
    # 1. Live in-process agent
    try:
        import services.shared_state as state

        agent_app = getattr(state, "agent_app", None)
        if agent_app is not None:
            agents_map = getattr(agent_app, "_agents", None) or {}
            agent = agents_map.get(agent_name)
            if agent is not None and hasattr(agent, "message_history"):
                try:
                    return list(agent.message_history)
                except Exception as exc:
                    logger.debug(
                        "[message_stream] live history read failed for %s: %s",
                        agent_name, exc,
                    )
    except Exception as exc:
        logger.debug("[message_stream] runtime lookup failed: %s", exc)

    # 2. Persisted snapshot (covers subprocess agents)
    try:
        from services.context_persistence import load_latest_context

        snapshot = load_latest_context(agent_name)
        if snapshot:
            return list(snapshot)
    except Exception as exc:
        logger.debug("[message_stream] snapshot read failed for %s: %s", agent_name, exc)

    return []


def list_agent_messages(
    agent_name: str,
    *,
    since: int = 0,
    limit: int = 200,
) -> dict:
    """Return ``{turns: [{turn_idx, message}, ...], total: N}`` with trimming applied.

    Resolution order:
      1. Live ``agent.message_history`` if non-empty (covers persistent agents
         that own their history — Jarvis, etc.)
      2. The ``_recent_turns`` cache populated by ``emit_message_history_delta``
         (covers ephemeral clones — e.g. ``agent__FinanceAgent`` invocations).
      3. Latest ``agent_context_snapshots`` row (covers subprocess agents).

    ``since`` is the first turn_idx to include (delta fetch on reconnect).
    ``limit`` caps the number of turns returned (latest ``limit`` if the
    history is longer than ``since + limit``).
    """
    if since < 0:
        since = 0

    # 1. Live agent.message_history
    history = _resolve_agent_history(agent_name)
    if history:
        total = len(history)
        if since >= total:
            return {"turns": [], "total": total}
        end = total
        start = max(since, end - limit) if limit > 0 else since
        turns: list[dict] = []
        for idx in range(start, end):
            msg = history[idx]
            try:
                payload = msg.model_dump(mode="json", exclude_none=True)
            except Exception as exc:
                logger.warning(
                    "[message_stream] model_dump failed for %s turn %d: %s",
                    agent_name, idx, exc,
                )
                continue
            turns.append({
                "turn_idx": idx,
                "role": payload.get("role"),
                "message": trim_message_for_stream(payload),
            })
        return {"turns": turns, "total": total, "start": start}

    # 2. Recent broadcast cache — used when the live agent is a discarded clone
    cached = get_recent_turns(agent_name)
    if cached:
        # cached entries are sorted by insertion order; sort by turn_idx to
        # be safe against out-of-order replacements.
        cached_sorted = sorted(cached, key=lambda t: t.get("turn_idx", 0))
        total = (cached_sorted[-1].get("turn_idx", -1) + 1) if cached_sorted else 0
        # Filter by since
        filtered = [t for t in cached_sorted if t.get("turn_idx", -1) >= since]
        if limit > 0 and len(filtered) > limit:
            filtered = filtered[-limit:]
        out_turns = []
        for t in filtered:
            payload = json.loads(json.dumps(t["message"]))  # copy before trim
            out_turns.append({
                "turn_idx": t["turn_idx"],
                "role": payload.get("role"),
                "message": trim_message_for_stream(payload),
            })
        start = out_turns[0]["turn_idx"] if out_turns else since
        return {"turns": out_turns, "total": total, "start": start}

    return {"turns": [], "total": 0}


def get_agent_turn_full(agent_name: str, turn_idx: int) -> dict | None:
    """Return the untruncated PromptMessageExtended dump for a single turn.

    Reads cache first (covers clones), then live agent (covers persistent
    agents and subprocess snapshots). Returns ``None`` if the turn doesn't
    exist anywhere.
    """
    # Try cache first — same reasoning as list_agent_messages.
    for entry in get_recent_turns(agent_name):
        if entry.get("turn_idx") == turn_idx:
            return {"turn_idx": turn_idx, "message": entry["message"]}

    history = _resolve_agent_history(agent_name)
    if turn_idx < 0 or turn_idx >= len(history):
        return None
    msg = history[turn_idx]
    try:
        return {
            "turn_idx": turn_idx,
            "message": msg.model_dump(mode="json", exclude_none=True),
        }
    except Exception as exc:
        logger.warning(
            "[message_stream] full dump failed for %s turn %d: %s",
            agent_name, turn_idx, exc,
        )
        return None
