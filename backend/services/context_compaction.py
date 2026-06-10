"""Context compaction with versioned visibility.

Implements docs/context-compaction-versioned-visibility-spec.md (see the
"Implementation addendum" section there for deviations and rationale).

Flow (all inside ``before_llm_call``, never touching the pending delta):

    threshold check (real provider tokens when available)
    -> save raw snapshot (append-only audit, trigger="pre_compaction")
    -> rule-based compactor produces a plan (same contract a future
       LLM compactor must honour)
    -> backend-side validation (independent of the compactor)
    -> working context built from deep copies
    -> agent.load_message_history(working)   # the ONLY live mutation
    -> completed event row persisted (working json + plan + stats)
    -> SSE status events on the activity stream

Safety model — why the live agent can never be corrupted:
  - The working context is built entirely from deep copies; the live
    ``agent._message_history`` is replaced in one atomic
    ``load_message_history()`` call only AFTER validation passes.
  - Mid tool-loop, ``LlmDecorator._persist_history`` has already pushed
    assistant(tool_calls) turns into message_history while their
    tool_results still ride in ``runner.delta_messages``. The tail of
    the history is therefore never summarized: the keep-recent window is
    pair-extended and the final message is always preserved verbatim.
  - The delta (current user message / pending tool results) is never
    read or written here — compaction operates on message_history only,
    and the provider payload is rebuilt from message_history + delta on
    every call (LlmDecorator._prepare_llm_call), so the imminent call
    picks up the compacted history automatically.

Token numbers: the THRESHOLD uses the provider-reported context tokens
from ``usage_accumulator`` when available (ground truth); the
before/after savings use one shared chars/4 estimator so the two numbers
are comparable (mixing a real "before" with an estimated "after" would
fabricate savings).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Bump when the rule-based planner's behaviour changes in a way that
# alters what a stored plan means (used to interpret old event rows).
POLICY_VERSION = 1

CONFIG_CATEGORY = "context_compaction"

SUMMARY_MARKER = "[COMPACTED_CONTEXT_SUMMARY]"

# Required section headers — backend validation rejects a summary that
# omits any of them, so a future LLM compactor cannot silently drop a
# section the resumed agent relies on.
SUMMARY_SECTIONS = (
    "Current goal:",
    "User constraints:",
    "Architecture facts:",
    "Important decisions:",
    "Tool findings:",
    "Errors / unresolved issues:",
    "Recent state:",
    "Next actions:",
    "Raw references:",
)

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # 0 = auto-detect from the model's context window (ModelDatabase via
    # usage_accumulator); a positive value overrides. Spec suggested a
    # static 120000 — auto is model-aware and survives model swaps.
    "max_context_tokens": 0,
    "compact_at_ratio": 0.7,
    "keep_recent_messages": 10,
    "max_tool_result_tokens_in_context": 1500,
    # Reject plans that save less than this fraction (unless manual) —
    # a no-op compaction would still pay the summary-quality risk.
    "min_savings_ratio": 0.05,
    "snapshot_versions_visible": 3,
    "emit_live_status": True,
}

# Fallback context window when neither config nor the model database
# knows the limit. Matches the spec's suggested default.
_FALLBACK_CONTEXT_TOKENS = 120000

# Compaction is pointless (and the validator would reject it) when the
# middle zone is tiny — require a few messages beyond the kept tail.
_MIN_MIDDLE_MESSAGES = 3


@dataclass(frozen=True)
class CompactionConfig:
    enabled: bool
    max_context_tokens: int
    compact_at_ratio: float
    keep_recent_messages: int
    max_tool_result_tokens_in_context: int
    min_savings_ratio: float
    snapshot_versions_visible: int
    emit_live_status: bool


def _coerce(value: Any, default: Any) -> Any:
    """Parse a config-DB string back into the default's type."""
    if value is None:
        return default
    if isinstance(default, bool):
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    try:
        if isinstance(default, int):
            return int(float(value))
        if isinstance(default, float):
            return float(value)
    except (TypeError, ValueError):
        return default
    return value


def get_compaction_config() -> CompactionConfig:
    """Read config from the config DB (category ``context_compaction``),
    falling back to DEFAULTS per key. Values are plain (non-secret), so
    this is subprocess-safe — no master key needed.
    """
    values = dict(DEFAULTS)
    try:
        from services.config_service import config_service

        for key, default in DEFAULTS.items():
            try:
                raw = config_service.get(CONFIG_CATEGORY, key, default=None)
            except Exception:
                raw = None
            if raw is not None:
                values[key] = _coerce(raw, default)
    except Exception as exc:  # config DB unavailable (e.g. bare subprocess)
        logger.debug("[COMPACT] Config read failed, using defaults: %s", exc)
    return CompactionConfig(**values)


def update_compaction_config(updates: dict[str, Any], *, user: str = "user") -> CompactionConfig:
    """Validate + persist settings through config_service (audit history
    and export/import come for free). Raises ValueError on bad input.
    """
    errors = validate_config_updates(updates)
    if errors:
        raise ValueError("; ".join(errors))

    from services.config_service import config_service

    config_service.set_many(
        [(CONFIG_CATEGORY, key, str(updates[key]), False) for key in updates],
        user=user,
    )
    return get_compaction_config()


def validate_config_updates(updates: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key in updates:
        if key not in DEFAULTS:
            errors.append(f"unknown setting: {key}")
    if not updates:
        errors.append("no settings provided")

    def _num(key):
        try:
            return float(updates[key])
        except (TypeError, ValueError):
            errors.append(f"{key} must be a number")
            return None

    if "compact_at_ratio" in updates:
        v = _num("compact_at_ratio")
        if v is not None and not (0.3 <= v <= 0.95):
            errors.append("compact_at_ratio must be between 0.3 and 0.95")
    if "keep_recent_messages" in updates:
        v = _num("keep_recent_messages")
        if v is not None and not (2 <= v <= 100):
            errors.append("keep_recent_messages must be between 2 and 100")
    if "max_context_tokens" in updates:
        v = _num("max_context_tokens")
        if v is not None and v != 0 and not (10000 <= v <= 2000000):
            errors.append("max_context_tokens must be 0 (auto) or between 10000 and 2000000")
    if "max_tool_result_tokens_in_context" in updates:
        v = _num("max_tool_result_tokens_in_context")
        if v is not None and not (100 <= v <= 100000):
            errors.append("max_tool_result_tokens_in_context must be between 100 and 100000")
    if "min_savings_ratio" in updates:
        v = _num("min_savings_ratio")
        if v is not None and not (0 <= v <= 0.9):
            errors.append("min_savings_ratio must be between 0 and 0.9")
    if "snapshot_versions_visible" in updates:
        v = _num("snapshot_versions_visible")
        if v is not None and not (1 <= v <= 50):
            errors.append("snapshot_versions_visible must be between 1 and 50")
    return errors


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


def estimate_tokens(messages: list) -> int:
    """Deterministic chars/4 estimate over the serialized messages.

    Intentionally crude: it exists so before/after numbers are computed
    the SAME way and the savings ratio is meaningful. The compaction
    TRIGGER prefers the provider-reported token count (see
    ``_current_context_tokens``); this estimator is the fallback and the
    before/after yardstick.
    """
    total = 0
    for msg in messages or []:
        try:
            total += len(msg.model_dump_json()) // 4
        except Exception:
            total += len(str(msg)) // 4
    return total


def _current_context_tokens(agent: Any, history: list) -> tuple[int, str]:
    """Best-available context size: provider truth first, estimate second."""
    try:
        acc = getattr(agent, "usage_accumulator", None)
        if acc is not None:
            real = getattr(acc, "current_context_tokens", 0) or 0
            if real > 0:
                return int(real), "provider"
    except Exception:
        pass
    return estimate_tokens(history), "estimate"


def _context_token_limit(agent: Any, cfg: CompactionConfig) -> int:
    if cfg.max_context_tokens > 0:
        return cfg.max_context_tokens
    try:
        acc = getattr(agent, "usage_accumulator", None)
        if acc is not None:
            window = getattr(acc, "context_window_size", None)
            if window:
                return int(window)
    except Exception:
        pass
    return _FALLBACK_CONTEXT_TOKENS


# ---------------------------------------------------------------------------
# Message helpers
# ---------------------------------------------------------------------------


def _msg_text(msg: Any) -> str:
    parts = []
    for block in getattr(msg, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


def _tool_result_texts(msg: Any) -> list[tuple[str, str, bool]]:
    """[(call_id, text, is_error)] for a message's tool_results."""
    out = []
    for call_id, result in (getattr(msg, "tool_results", None) or {}).items():
        parts = []
        for block in getattr(result, "content", None) or []:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        out.append((call_id, "\n".join(parts), bool(getattr(result, "isError", False))))
    return out


def _tool_call_names(msg: Any) -> list[str]:
    names = []
    for _id, call in (getattr(msg, "tool_calls", None) or {}).items():
        params = getattr(call, "params", None)
        names.append(getattr(params, "name", "unknown") if params else "unknown")
    return names


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " …"


def _template_prefix_len(messages: list) -> int:
    n = 0
    for msg in messages:
        if getattr(msg, "is_template", False):
            n += 1
        else:
            break
    return n


def _latest_user_text_index(messages: list) -> int | None:
    """Index of the latest user message that carries actual text (the
    "latest user request"). Tool-result-only user messages don't count.
    """
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if str(getattr(msg, "role", "")) != "user":
            continue
        if getattr(msg, "tool_results", None) and not _msg_text(msg).strip():
            continue
        if _msg_text(msg).strip():
            return i
    return None


def _pair_safe_tail_start(messages: list, head_end: int, keep_recent: int) -> int:
    """Largest index <= len-keep_recent such that no tool_results inside
    the tail reference a tool_calls message outside it. Splitting an
    assistant(tool_calls) from its user(tool_results) produces a payload
    most providers reject — the cut must move back to keep pairs whole.
    """
    start = max(head_end, len(messages) - keep_recent)
    while start > head_end:
        # Call ids OWNED inside the tail.
        owned: set[str] = set()
        needed: set[str] = set()
        for msg in messages[start:]:
            for call_id in (getattr(msg, "tool_calls", None) or {}):
                owned.add(call_id)
            for call_id in (getattr(msg, "tool_results", None) or {}):
                needed.add(call_id)
        if needed - owned:
            start -= 1
            continue
        return start
    return head_end


# ---------------------------------------------------------------------------
# Rule-based compactor (same output contract as a future LLM compactor)
# ---------------------------------------------------------------------------


def plan_compaction(messages: list, cfg: CompactionConfig, raw_snapshot_id: int | None) -> dict | None:
    """Produce a compaction plan, or None when there is nothing worth
    compacting (middle zone too small).

    Contract (spec §8): summary_message, keep_verbatim, summarize,
    delete_from_working_context, promote_to_memory, raw_references,
    risks, confidence. Indices refer to positions in the CURRENT
    message_history (== the raw snapshot saved just before planning).
    """
    head_end = _template_prefix_len(messages)
    tail_start = _pair_safe_tail_start(messages, head_end, cfg.keep_recent_messages)
    if tail_start - head_end < _MIN_MIDDLE_MESSAGES:
        return None

    keep = list(range(head_end)) + list(range(tail_start, len(messages)))
    middle = list(range(head_end, tail_start))

    # Non-negotiable: the latest user request survives verbatim even when
    # a long tool loop pushed it out of the keep-recent window.
    forced_user_idx = _latest_user_text_index(messages)
    if forced_user_idx is not None and forced_user_idx in middle:
        middle.remove(forced_user_idx)
        keep.append(forced_user_idx)
        keep.sort()

    summary = _build_summary(messages, middle, raw_snapshot_id)

    # Oversized tool results inside the kept tail get truncated (full
    # text stays in the raw snapshot) — except the final message, whose
    # tool result may be the one the next LLM call reasons over.
    max_chars = cfg.max_tool_result_tokens_in_context * 4
    summarize: list[dict] = []
    for idx in range(tail_start, len(messages) - 1):
        for call_id, text, _err in _tool_result_texts(messages[idx]):
            if len(text) > max_chars:
                summarize.append(
                    {
                        "index": idx,
                        "call_id": call_id,
                        "action": "truncate_tool_result",
                        "max_chars": max_chars,
                    }
                )

    return {
        "summary_message": summary,
        "keep_verbatim": keep,
        "summarize": summarize,
        "delete_from_working_context": middle,
        "promote_to_memory": [],  # reserved — no memory subsystem yet
        "raw_references": [
            {
                "raw_snapshot_id": raw_snapshot_id,
                "message_indexes": [middle[0], middle[-1]] if middle else [],
            }
        ],
        "risks": [
            "rule-based summary may miss implicit decisions buried in dropped turns",
        ],
        # Fixed mid confidence: the rule-based planner is structurally
        # safe but semantically blind; an LLM compactor should set this
        # from its own judgement.
        "confidence": 0.6,
    }


def _build_summary(messages: list, middle: list[int], raw_snapshot_id: int | None) -> str:
    first_user = next(
        (
            _msg_text(messages[i])
            for i in range(len(messages))
            if str(getattr(messages[i], "role", "")) == "user" and _msg_text(messages[i]).strip()
        ),
        "",
    )
    latest_idx = _latest_user_text_index(messages)
    latest_user = _msg_text(messages[latest_idx]) if latest_idx is not None else ""

    tool_counts: dict[str, int] = {}
    findings: list[str] = []
    errors: list[str] = []
    carried: list[str] = []
    decisions: list[str] = []

    for i in middle:
        msg = messages[i]
        text = _msg_text(msg)
        if text.startswith(SUMMARY_MARKER):
            carried.append(_clip(text, 1500))
            continue
        if str(getattr(msg, "role", "")) == "assistant" and text.strip():
            decisions.append(_clip(text, 400))
        for name in _tool_call_names(msg):
            tool_counts[name] = tool_counts.get(name, 0) + 1
        for _call_id, rtext, is_err in _tool_result_texts(msg):
            if not rtext.strip():
                continue
            if is_err:
                errors.append(_clip(rtext, 300))
            else:
                findings.append(_clip(rtext, 200))

    tools_line = (
        ", ".join(f"{n}×{c}" for n, c in sorted(tool_counts.items())) or "(none)"
    )
    findings_block = "\n".join(f"- {f}" for f in findings[-5:]) or "(none captured)"
    errors_block = "\n".join(f"- {e}" for e in errors[-5:]) or "(none)"
    decisions_block = "\n".join(f"- {d}" for d in decisions[-3:]) or "(none captured)"
    recent_state = decisions[-1] if decisions else "(see recent messages below)"
    carried_block = ("\n\nCarried from previous compaction:\n" + "\n---\n".join(carried)) if carried else ""
    ref_range = f"messages {middle[0]}–{middle[-1]}" if middle else "(none)"

    return (
        f"{SUMMARY_MARKER}\n\n"
        f"Current goal:\n{_clip(latest_user or first_user, 600) or '(none captured)'}\n\n"
        f"User constraints:\n(not captured by rule-based compactor)\n\n"
        f"Architecture facts:\n(not captured by rule-based compactor)\n\n"
        f"Important decisions:\n{decisions_block}\n\n"
        f"Tool findings:\nTools used: {tools_line}\n{findings_block}\n\n"
        f"Errors / unresolved issues:\n{errors_block}\n\n"
        f"Recent state:\n{_clip(recent_state, 400)}\n\n"
        f"Next actions:\nContinue the current task with the recent messages below.\n\n"
        f"Raw references:\nraw snapshot #{raw_snapshot_id}, {ref_range}"
        f"{carried_block}"
    )


def build_working_context(messages: list, plan: dict) -> list:
    """Materialize the plan into a new message list (all deep copies —
    the live history must stay untouched until validation passes).
    """
    from fast_agent.types import PromptMessageExtended
    from mcp.types import TextContent

    keep = set(plan["keep_verbatim"])
    head_end = _template_prefix_len(messages)
    truncations: dict[int, list[dict]] = {}
    for entry in plan.get("summarize", []):
        truncations.setdefault(entry["index"], []).append(entry)

    working: list = []
    summary_inserted = False
    for idx, msg in enumerate(messages):
        if idx not in keep:
            continue
        if idx >= head_end and not summary_inserted:
            working.append(
                PromptMessageExtended(
                    role="user",
                    content=[TextContent(type="text", text=plan["summary_message"])],
                )
            )
            summary_inserted = True
        copy = msg.model_copy(deep=True)
        for entry in truncations.get(idx, []):
            _truncate_tool_result(copy, entry["call_id"], entry["max_chars"])
        working.append(copy)
    if not summary_inserted:  # degenerate: everything was template
        working.append(
            PromptMessageExtended(
                role="user",
                content=[TextContent(type="text", text=plan["summary_message"])],
            )
        )
    return working


def _truncate_tool_result(msg: Any, call_id: str, max_chars: int) -> None:
    result = (getattr(msg, "tool_results", None) or {}).get(call_id)
    if result is None:
        return
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text and len(text) > max_chars:
            block.text = (
                text[:max_chars]
                + f"\n…[truncated by context compaction — full output preserved in the raw snapshot]"
            )


# ---------------------------------------------------------------------------
# Backend validation (independent of whichever compactor produced the plan)
# ---------------------------------------------------------------------------


def validate_working_context(
    raw_messages: list,
    working_messages: list,
    cfg: CompactionConfig,
    *,
    reason: str = "auto_threshold",
) -> list[str]:
    """Return a list of violations (empty = valid). Mirrors spec §8."""
    errors: list[str] = []
    if not working_messages:
        return ["working context is empty"]

    # Round-trip through the canonical serializer — anything from_json
    # can't reproduce would corrupt the next resume.
    try:
        from fast_agent.mcp.prompt_serialization import from_json, to_json

        round_tripped = from_json(to_json(working_messages))
        if len(round_tripped) != len(working_messages):
            errors.append("working context does not round-trip through prompt serialization")
    except Exception as exc:
        return [f"working context failed serialization round-trip: {exc}"]

    # System/template messages preserved, in order, at the head.
    raw_templates = [m for m in raw_messages if getattr(m, "is_template", False)]
    working_templates = [m for m in working_messages if getattr(m, "is_template", False)]
    if len(raw_templates) != len(working_templates):
        errors.append("system/template messages were removed")
    else:
        for rt, wt in zip(raw_templates, working_templates):
            if _msg_text(rt) != _msg_text(wt):
                errors.append("system/template message content was altered")
                break

    # Latest user request preserved verbatim.
    latest_idx = _latest_user_text_index(raw_messages)
    if latest_idx is not None:
        latest_text = _msg_text(raw_messages[latest_idx])
        if not any(
            _msg_text(m) == latest_text
            for m in working_messages
            if str(getattr(m, "role", "")) == "user"
        ):
            errors.append("latest user request was removed")

    # Final message (possibly an assistant turn whose tool results are
    # still pending in the runner delta) must survive byte-identical.
    if raw_messages:
        try:
            from fast_agent.mcp.prompt_serialization import to_json as _tj

            if _tj([raw_messages[-1]]) != _tj([working_messages[-1]]):
                errors.append("final message was not preserved verbatim")
        except Exception:
            errors.append("final message comparison failed")

    # Tool-call/tool-result pairing must stay resolvable.
    seen_calls: set[str] = set()
    unresolved: set[str] = set()
    for msg in working_messages:
        for call_id in (getattr(msg, "tool_calls", None) or {}):
            seen_calls.add(call_id)
            unresolved.add(call_id)
        for call_id in (getattr(msg, "tool_results", None) or {}):
            if call_id not in seen_calls:
                errors.append(f"tool result {call_id} has no preceding tool call")
            unresolved.discard(call_id)
    final_calls = set(getattr(working_messages[-1], "tool_calls", None) or {})
    if unresolved - final_calls:
        errors.append("tool call(s) lost their tool result in the working context")

    # Exactly one compaction summary with all required sections.
    summaries = [m for m in working_messages if _msg_text(m).startswith(SUMMARY_MARKER)]
    if len(summaries) != 1:
        errors.append(f"expected exactly 1 summary message, found {len(summaries)}")
    else:
        text = _msg_text(summaries[0])
        missing = [s for s in SUMMARY_SECTIONS if s not in text]
        if missing:
            errors.append(f"summary missing sections: {', '.join(missing)}")

    # Minimum savings (skip for manual compactions).
    if reason != "manual":
        before = estimate_tokens(raw_messages)
        after = estimate_tokens(working_messages)
        if before > 0 and after > before * (1 - cfg.min_savings_ratio):
            errors.append(
                f"savings below minimum: {before} -> {after} "
                f"(required ≥ {cfg.min_savings_ratio:.0%})"
            )

    return errors


# ---------------------------------------------------------------------------
# Manager — threshold check + full pipeline
# ---------------------------------------------------------------------------

# One lock per agent so a slow compaction can't stack a second one, and a
# per-length memo so a too-small-to-help history isn't re-planned on every
# subsequent LLM call (it retries only after the history grows).
_locks: dict[str, asyncio.Lock] = {}
_last_attempt_len: dict[str, int] = {}


def _lock_for(agent_name: str) -> asyncio.Lock:
    lock = _locks.get(agent_name)
    if lock is None:
        lock = asyncio.Lock()
        _locks[agent_name] = lock
    return lock


def reset_compaction_guards() -> None:
    """Test hook: clear per-agent memo/locks."""
    _locks.clear()
    _last_attempt_len.clear()


def _emit_status(cfg: CompactionConfig, event_type: str, agent_name: str, run_id: str, data: dict) -> None:
    if not cfg.emit_live_status:
        return
    payload = {
        "agent_name": agent_name,
        "event_type": event_type,
        "run_id": run_id,
        "timestamp": time.time(),
        "data": data,
    }
    try:
        from services.activity_stream import activity_stream_manager

        activity_stream_manager.broadcast(payload)
    except Exception as exc:
        logger.debug("[COMPACT] activity broadcast failed: %s", exc)
    # Mirror into the per-request chat-stream queue (if a chat request is
    # live) so the chat UI can show a non-blocking status line.
    try:
        from services.sse_progress import current_run_id, progress_manager

        rid = current_run_id.get() or ""
        if rid:
            progress_manager.push(rid, event_type, {"agent": agent_name, **data})
    except Exception as exc:
        logger.debug("[COMPACT] progress push failed: %s", exc)


async def maybe_compact_agent(
    agent: Any,
    *,
    agent_name: str,
    run_id: str = "",
    session_id: str | None = None,
    team_name: str | None = None,
    reason: str = "auto_threshold",
) -> dict | None:
    """Compact the agent's history if the context threshold is exceeded.

    Returns a stats dict when a compaction was applied, else None.
    Never raises — a compaction failure must never break the LLM call
    (the agent continues on the raw context; the failure is recorded).
    """
    try:
        cfg = get_compaction_config()
        if not cfg.enabled and reason != "manual":
            return None

        history = list(getattr(agent, "message_history", None) or [])
        if len(history) < cfg.keep_recent_messages + _MIN_MIDDLE_MESSAGES:
            return None

        if reason != "manual":
            limit = _context_token_limit(agent, cfg)
            current, source = _current_context_tokens(agent, history)
            if current < limit * cfg.compact_at_ratio:
                return None
            if _last_attempt_len.get(agent_name) == len(history):
                return None
            logger.info(
                "[COMPACT] Threshold hit for %s: %d/%d tokens (%s, ratio %.2f)",
                agent_name, current, limit, source, cfg.compact_at_ratio,
            )

        lock = _lock_for(agent_name)
        if lock.locked():
            return None
        async with lock:
            # Memo BEFORE attempting: even a failed/skipped attempt should
            # not be retried until the history actually grows.
            _last_attempt_len[agent_name] = len(history)
            return await _run_compaction(
                agent, history, cfg,
                agent_name=agent_name, run_id=run_id,
                session_id=session_id, team_name=team_name, reason=reason,
            )
    except Exception as exc:
        logger.error("[COMPACT] Unexpected error for %s: %s", agent_name, exc, exc_info=True)
        return None


async def _run_compaction(
    agent: Any,
    history: list,
    cfg: CompactionConfig,
    *,
    agent_name: str,
    run_id: str,
    session_id: str | None,
    team_name: str | None,
    reason: str,
) -> dict | None:
    import json as _json

    from services.context_persistence import save_agent_context, save_compaction_event

    tokens_before = estimate_tokens(history)
    _emit_status(
        cfg, "context_compaction_started", agent_name, run_id,
        {"estimated_tokens_before": tokens_before, "message_count": len(history)},
    )

    def _fail(error: str, *, plan: dict | None = None, validation: list[str] | None = None):
        logger.warning("[COMPACT] Failed for %s: %s", agent_name, error)
        save_compaction_event(
            agent_name=agent_name, run_id=run_id, session_id=session_id,
            team_name=team_name, raw_snapshot_id=raw_snapshot_id,
            status="failed", error_message=error, trigger=reason,
            plan_json=_json.dumps(plan) if plan else None,
            validation_json=_json.dumps(validation) if validation else None,
            message_count_before=len(history),
            estimated_tokens_before=tokens_before,
            policy_version=POLICY_VERSION,
        )
        _emit_status(
            cfg, "context_compaction_failed", agent_name, run_id, {"error": error},
        )
        return None

    # 1. Raw snapshot FIRST — without the audit copy we refuse to touch
    #    the history (non-negotiable: raw is never lost).
    raw_snapshot_id = await save_agent_context(
        agent, run_id or f"compact-{int(time.time())}", trigger="pre_compaction",
        agent_name=agent_name, session_id=session_id, team_name=team_name,
    )
    if raw_snapshot_id is None:
        return _fail("raw snapshot could not be saved — compaction aborted")

    # 2. Plan (rule-based compactor).
    plan = plan_compaction(history, cfg, raw_snapshot_id)
    if plan is None:
        logger.info("[COMPACT] Nothing to compact for %s (middle zone too small)", agent_name)
        return None

    # 3. Build working context from copies.
    try:
        working = build_working_context(history, plan)
    except Exception as exc:
        return _fail(f"failed to build working context: {exc}", plan=plan)

    # 4. Validate — the live history is mutated only after this passes.
    violations = validate_working_context(history, working, cfg, reason=reason)
    if violations:
        return _fail("plan rejected: " + "; ".join(violations), plan=plan, validation=violations)

    tokens_after = estimate_tokens(working)

    # 5. Apply atomically (load_message_history deep-copies the input).
    try:
        agent.load_message_history(working)
    except Exception as exc:
        return _fail(f"failed to load working context into agent: {exc}", plan=plan)

    # 6. Persist the completed event (working json + plan + stats).
    from fast_agent.mcp.prompt_serialization import to_json

    event_id = save_compaction_event(
        agent_name=agent_name, run_id=run_id, session_id=session_id,
        team_name=team_name, raw_snapshot_id=raw_snapshot_id,
        working_context_json=to_json(working),
        summary_message=plan["summary_message"],
        plan_json=_json.dumps({k: v for k, v in plan.items() if k != "summary_message"}),
        validation_json=_json.dumps([]),
        message_count_before=len(history), message_count_after=len(working),
        estimated_tokens_before=tokens_before, estimated_tokens_after=tokens_after,
        trigger=reason, confidence=plan.get("confidence", 0.0),
        status="completed", policy_version=POLICY_VERSION,
    )

    saved = tokens_before - tokens_after
    stats = {
        "event_id": event_id,
        "raw_snapshot_id": raw_snapshot_id,
        "estimated_tokens_before": tokens_before,
        "estimated_tokens_after": tokens_after,
        "saved_tokens": saved,
        "reduction_ratio": round(saved / tokens_before, 4) if tokens_before else 0,
        "message_count_before": len(history),
        "message_count_after": len(working),
    }
    logger.info(
        "[COMPACT] Compacted %s: %d→%d msgs, ~%d→~%d tokens (saved ~%d, event #%s)",
        agent_name, len(history), len(working), tokens_before, tokens_after, saved, event_id,
    )
    _emit_status(cfg, "context_compaction_completed", agent_name, run_id, stats)
    return stats


# ---------------------------------------------------------------------------
# Hook wiring (mirrors the always-on token-persistence hook pattern)
# ---------------------------------------------------------------------------


def create_context_compaction_hooks():
    """``before_llm_call`` hook: threshold-check + compact the agent's
    message_history. The pending delta is never touched; the imminent
    LLM call picks the compacted history up automatically because the
    provider payload is rebuilt from message_history on every call.
    """
    from fast_agent.agents.tool_runner import ToolRunnerHooks

    async def on_before_llm_call(runner, _delta_messages) -> None:
        try:
            from services.sse_progress import current_run_id, normalize_agent_name

            agent = runner._agent
            raw_name = getattr(agent, "name", "Agent")
            await maybe_compact_agent(
                agent,
                agent_name=normalize_agent_name(raw_name),
                run_id=current_run_id.get() or "",
            )
        except Exception as exc:
            # Never let compaction break the LLM call.
            logger.error("[COMPACT] before_llm_call hook error: %s", exc, exc_info=True)

    return ToolRunnerHooks(before_llm_call=on_before_llm_call)


def attach_compaction_hooks_to_all(agent_app) -> int:
    """Idempotently attach the compaction hook to every in-process agent
    (sentinel ``_jarvis_compaction_hook``, same pattern as the token
    hook). Returns the number of agents newly hooked.
    """
    from services.sse_progress import merge_hooks

    hook = create_context_compaction_hooks()
    newly = 0
    try:
        agents = getattr(agent_app, "_agents", {}) or {}
    except Exception:
        agents = {}
    for name, ag in agents.items():
        if getattr(ag, "_jarvis_compaction_hook", False):
            continue
        existing = getattr(ag, "tool_runner_hooks", None)
        ag.tool_runner_hooks = merge_hooks(existing, hook) if existing else hook
        ag._jarvis_compaction_hook = True
        newly += 1
        logger.debug("[COMPACT] Attached compaction hook to '%s'", name)
    return newly
