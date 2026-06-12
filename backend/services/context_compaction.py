"""Context compaction with versioned visibility.

Implements docs/context-compaction-versioned-visibility-spec.md (see the
"Implementation addendum" section there for deviations and rationale).

Flow (all inside ``before_llm_call``, never touching the pending delta):

    threshold check (real provider tokens when available)
    -> save raw snapshot (append-only audit, trigger="pre_compaction")
    -> LLM compactor (dedicated compactor agent) produces a plan
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
import json
import logging
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Bump when the compactor's plan semantics change in a way that alters
# what a stored plan means (used to interpret old event rows).
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
    "File references:",
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
    # Model for the DEDICATED compactor agent (spec §1–2: compaction is
    # performed by a separate compactor component/agent). A model with a
    # larger context window than the origin agent's lets one compactor
    # call feed more history at once. Empty = use the origin agent's own
    # LLM (no separate agent configured yet).
    "compactor_model": "",
    # Fraction of the COMPACTOR's context window a single compaction call
    # may consume as input — the rest is headroom for the instruction
    # scaffold, the JSON output, and provider overhead. Larger = fewer
    # chunks (faster, cheaper) but riskier of overflowing the compactor.
    "compactor_input_ratio": 0.7,
}

# Conservative window used ONLY to size LLM-compactor chunks when the
# real window is unknown (overflow recovery must still work). Never used
# as a compaction threshold — an unknown window fails loud instead.
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
    compactor_model: str
    compactor_input_ratio: float


# Single source for the valid numeric ranges — used by the typed PATCH
# validation AND re-checked at read time, because the generic
# ``PUT /api/settings/{category}/{key}`` route can write any string into
# the same rows, bypassing the typed endpoint.
_RANGES: dict[str, tuple[float, float]] = {
    "compact_at_ratio": (0.3, 0.95),
    "keep_recent_messages": (2, 100),
    "max_tool_result_tokens_in_context": (100, 100000),
    "min_savings_ratio": (0.0, 0.9),
    "snapshot_versions_visible": (1, 50),
    "compactor_input_ratio": (0.1, 0.9),
}
# max_context_tokens is special-cased: 0 (= auto) or within this range.
_MAX_CONTEXT_TOKENS_RANGE = (10000, 2000000)


def _coerce(key: str, value: Any, default: Any) -> Any:
    """Parse a config-DB string back into the default's type. Garbage
    falls back to the default LOUDLY — a typo'd value written through
    the generic settings route must not silently change behaviour."""
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
        logger.warning(
            "[COMPACT] Unparseable config %s.%s=%r — using default %r",
            CONFIG_CATEGORY, key, value, default,
        )
        return default
    return value


def _in_range(key: str, value: Any) -> bool:
    if key == "max_context_tokens":
        lo, hi = _MAX_CONTEXT_TOKENS_RANGE
        return value == 0 or lo <= value <= hi
    if key in _RANGES:
        lo, hi = _RANGES[key]
        return lo <= value <= hi
    return True


def get_compaction_config() -> CompactionConfig:
    """Read config from the config DB (category ``context_compaction``),
    falling back to DEFAULTS per key. Values are plain (non-secret), so
    this is subprocess-safe — no master key needed.

    Fails CLOSED: this feature rewrites agent history, so if the config
    DB cannot be read at all we disable compaction for this call rather
    than running on guessed defaults. Out-of-range values (possible via
    the generic settings PUT, which bypasses the typed PATCH validation)
    revert to the default for that key with a warning.
    """
    values = dict(DEFAULTS)
    try:
        from services.config_service import config_service

        for key, default in DEFAULTS.items():
            raw = config_service.get(CONFIG_CATEGORY, key, default=None)
            if raw is None:
                continue
            value = _coerce(key, raw, default)
            if not _in_range(key, value):
                logger.warning(
                    "[COMPACT] Out-of-range config %s.%s=%r — using default %r",
                    CONFIG_CATEGORY, key, value, default,
                )
                value = default
            values[key] = value
    except Exception as exc:  # config DB unavailable/unreadable
        logger.warning(
            "[COMPACT] Config read failed — compaction disabled for this call: %s", exc,
        )
        values = dict(DEFAULTS)
        values["enabled"] = False
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

    for key, (lo, hi) in _RANGES.items():
        if key in updates:
            v = _num(key)
            if v is not None and not (lo <= v <= hi):
                errors.append(f"{key} must be between {lo} and {hi}")
    if "max_context_tokens" in updates:
        v = _num("max_context_tokens")
        lo, hi = _MAX_CONTEXT_TOKENS_RANGE
        if v is not None and v != 0 and not (lo <= v <= hi):
            errors.append(f"max_context_tokens must be 0 (auto) or between {lo} and {hi}")
    if "compactor_model" in updates:
        v = updates["compactor_model"]
        if not isinstance(v, str) or len(v) > 200:
            errors.append("compactor_model must be a string of at most 200 characters")
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


def _context_token_limit(agent: Any, cfg: CompactionConfig, agent_name: str = "") -> int | None:
    """Authoritative context limit, or None when it is genuinely unknown.

    NO invented fallback here: thresholding against a made-up number
    either compacts prematurely (small guess) or never (big guess). The
    caller fails loud on None so the operator can fix the config.
    """
    if cfg.max_context_tokens > 0:
        return cfg.max_context_tokens
    window = None
    try:
        acc = getattr(agent, "usage_accumulator", None)
        if acc is not None:
            window = getattr(acc, "context_window_size", None)
    except Exception:
        window = None
    if window:
        # Gateway combos rotate between real models with different
        # windows; threshold against the smallest window seen this
        # session so a mid-conversation switch to a smaller model
        # cannot overflow before the next check.
        window = int(window)
        seen = _min_window_seen.get(agent_name)
        if seen is not None:
            window = min(seen, window)
        if agent_name:
            _min_window_seen[agent_name] = window
        return window
    return _min_window_seen.get(agent_name)


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


def _plan_zones(messages: list, cfg: CompactionConfig) -> tuple[int, int, list[int], list[int]] | None:
    """(head_end, tail_start, keep, middle) — the structural safety zones
    the LLM compactor builds on (templates kept, pair-safe tail, latest
    user verbatim). None when the middle zone is too small to be worth
    compacting (also the feasibility pre-check, kept LLM-free so an
    infeasible history is a cheap silent no-op).
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
    return head_end, tail_start, keep, middle


def _tail_truncations(messages: list, tail_start: int, cfg: CompactionConfig) -> list[dict]:
    """Oversized tool results inside the kept tail get truncated (full
    text stays in the raw snapshot) — except the final message, whose
    tool result may be the one the next LLM call reasons over.
    Per-BLOCK check, matching exactly what _truncate_tool_result will
    cut — a joined-text check would flag messages nothing gets removed
    from (PR #85 review F7).
    """
    max_chars = cfg.max_tool_result_tokens_in_context * 4
    summarize: list[dict] = []
    for idx in range(tail_start, len(messages) - 1):
        for call_id, result in (getattr(messages[idx], "tool_results", None) or {}).items():
            blocks = getattr(result, "content", None) or []
            if any(len(getattr(b, "text", "") or "") > max_chars for b in blocks):
                summarize.append(
                    {
                        "index": idx,
                        "call_id": call_id,
                        "action": "truncate_tool_result",
                        "max_chars": max_chars,
                    }
                )
    return summarize


def _plan_skeleton(
    keep: list[int],
    middle: list[int],
    summarize: list[dict],
    raw_snapshot_id: int | None,
) -> dict:
    return {
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
    }


# ---------------------------------------------------------------------------
# LLM compactor — THE only compactor. A dedicated compactor agent
# (``compactor_model``, falling back to the origin agent's LLM) reads the
# dropped turns and writes the summary, so semantics survive. There is no
# rule-based fallback: a compaction that cannot run fails loud.
# ---------------------------------------------------------------------------

# Floor so a tiny/unknown window still yields workable chunks.
_COMPACTOR_MIN_CHUNK_TOKENS = 8000
_COMPACTOR_CALL_TIMEOUT_S = 180

_CHUNK_PROMPT = """You are a faithful compression layer for an AI agent's conversation history. Your ONLY job is to preserve, without loss of meaning, the information a continuation would need. You do NOT interpret, conclude, or steer.

Below is part {part} of {total} of the history. Extract what it actually contains.

Rules:
- Record only what the excerpt explicitly establishes. Never infer, embellish, or invent.
- Do NOT judge whether the task is complete or unfinished, and do NOT decide what should happen next. Whether to continue, stop, or ask the user is the agent's own decision — it makes that decision from your summary plus the verbatim recent messages it still holds, so prejudging it corrupts the outcome.
- "state" = the factual situation exactly as the excerpt last leaves it (what was done, what is underway), stated neutrally — not a verdict on progress.
- "next_actions" = only steps the user or the agent EXPLICITLY stated as pending in this excerpt; if none are stated, return an empty list rather than guessing one.
- Preserve user instructions and intent verbatim in meaning; never paraphrase them into a conclusion.

Return STRICT JSON (no markdown fences, no commentary) with exactly these keys, each a list of short strings (empty list when nothing applies):
{{"goal": [], "constraints": [], "architecture_facts": [], "decisions": [], "tool_findings": [], "errors": [], "file_references": [], "state": [], "next_actions": []}}

"file_references" must list every file path the agent read, edited, or discussed — these let the agent re-open its working set.

Conversation excerpt:
{body}"""

# Dedicated compactor LLM instances, one per configured model name.
_compactor_llm_cache: dict[str, Any] = {}


def _get_compactor_llm(agent: Any, cfg: CompactionConfig) -> tuple[Any, int | None, str]:
    """(llm, context_window, label) for the compactor.

    Spec §1–2: compaction is performed by a SEPARATE compactor
    component/agent — when ``compactor_model`` is configured we build a
    dedicated LLM (reusing the origin agent's context for provider
    config/keys), so a larger-window model can feed more history per
    call. Unset → the origin agent's own LLM as a degraded default.
    """
    model = (cfg.compactor_model or "").strip()
    if model:
        llm = _compactor_llm_cache.get(model)
        if llm is None:
            from fast_agent.agents.agent_types import AgentConfig
            from fast_agent.agents.llm_agent import LlmAgent
            from fast_agent.llm.model_factory import ModelFactory

            ctx = getattr(agent, "_context", None) or getattr(agent, "context", None)
            shell = LlmAgent(AgentConfig(name="context-compactor"), context=ctx)
            llm = ModelFactory.create_factory(model)(shell)
            _compactor_llm_cache[model] = llm
        from fast_agent.llm.model_database import ModelDatabase

        return llm, ModelDatabase.get_context_window(model), model
    llm = getattr(agent, "_llm", None)
    if llm is None:
        raise RuntimeError("agent has no attached LLM and no compactor_model configured")
    return llm, None, "origin-agent-model"


# Path-shaped tokens inside tool-call arguments (file_path, paths in
# queries, …). Arguments are the reliable source — result texts are huge
# and full of incidental paths.
_PATH_RE = re.compile(r"(?:[\w.~-]+)?(?:/[\w.-]+){2,}")


def _file_references(messages: list, idxs: list[int], cap: int = 15) -> list[str]:
    out: list[str] = []
    for i in idxs:
        for _call_id, call in (getattr(messages[i], "tool_calls", None) or {}).items():
            args = getattr(getattr(call, "params", None), "arguments", None) or {}
            for value in args.values():
                if not isinstance(value, str):
                    continue
                for match in _PATH_RE.findall(value)[:3]:
                    if match not in out:
                        out.append(match)
    return out[:cap]


def _pair_safe_chunks(messages: list, middle: list[int], chunk_tokens: int) -> list[list[int]]:
    """Split the middle zone into contiguous chunks of ~chunk_tokens each,
    cutting only where every tool call so far has its result — a chunk
    must be independently summarizable without dangling pairs.
    """
    chunks: list[list[int]] = []
    cur: list[int] = []
    cur_tokens = 0
    open_calls: set[str] = set()
    for idx in middle:
        msg = messages[idx]
        cur.append(idx)
        cur_tokens += estimate_tokens([msg])
        for call_id in getattr(msg, "tool_calls", None) or {}:
            open_calls.add(call_id)
        for call_id in getattr(msg, "tool_results", None) or {}:
            open_calls.discard(call_id)
        if cur_tokens >= chunk_tokens and not open_calls:
            chunks.append(cur)
            cur, cur_tokens = [], 0
    if cur:
        chunks.append(cur)
    return chunks


def _render_for_summary(messages: list, idxs: list[int]) -> str:
    lines: list[str] = []
    for i in idxs:
        msg = messages[i]
        role = str(getattr(msg, "role", ""))
        text = _clip(_msg_text(msg), 2000)
        if text:
            lines.append(f"[{i}] {role}: {text}")
        for _call_id, call in (getattr(msg, "tool_calls", None) or {}).items():
            params = getattr(call, "params", None)
            name = getattr(params, "name", "unknown") if params else "unknown"
            args = getattr(params, "arguments", None) or {}
            lines.append(f"[{i}] {role} called {name}({_clip(json.dumps(args, default=str), 300)})")
        for _call_id, rtext, is_err in _tool_result_texts(msg):
            if rtext.strip():
                tag = "tool_error" if is_err else "tool_result"
                lines.append(f"[{i}] {tag}: {_clip(rtext, 1200)}")
    return "\n".join(lines)


def _parse_chunk_json(text: str) -> dict:
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t.strip())
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("compactor output contains no JSON object")
    data = json.loads(t[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("compactor output is not a JSON object")
    return data


@contextmanager
def _detached_stream_listeners(llm: Any):
    """Swap the LLM's stream-listener sets for empty ones so compactor
    tokens never leak into the live chat stream. MUST wrap the whole
    batch of concurrent compactor calls (a per-call save/restore would
    race: the last call to finish could restore another call's empty
    set and silently kill the chat stream).
    """
    saved_stream = getattr(llm, "_stream_listeners", None)
    saved_tool_stream = getattr(llm, "_tool_stream_listeners", None)
    if saved_stream is not None:
        llm._stream_listeners = set()
    if saved_tool_stream is not None:
        llm._tool_stream_listeners = set()
    try:
        yield
    finally:
        if saved_stream is not None:
            llm._stream_listeners = saved_stream
        if saved_tool_stream is not None:
            llm._tool_stream_listeners = saved_tool_stream


async def _llm_one_shot(llm: Any, prompt_text: str) -> str:
    """One isolated LLM call on the agent's own model.

    Uses ``llm.generate`` directly (history is caller-managed there, so
    the agent's message_history is untouched). The caller detaches
    stream listeners around the whole batch via
    ``_detached_stream_listeners``. Usage IS recorded on the agent's
    accumulator — the cost is real.
    """
    from fast_agent.core.prompt import Prompt

    response = await asyncio.wait_for(
        llm.generate([Prompt.user(prompt_text)], request_params=None, tools=None),
        timeout=_COMPACTOR_CALL_TIMEOUT_S,
    )
    return _msg_text(response)


def _merge_chunk_sections(
    parts: list[dict],
    messages: list,
    middle: list[int],
    raw_snapshot_id: int | None,
) -> str:
    """Deterministic merge of per-chunk JSON into ONE summary message with
    the exact SUMMARY_SECTIONS structure (a second LLM merge pass would
    add cost and a new failure mode for no structural gain). File
    references union the LLM-extracted paths with the deterministic
    tool-argument scan, so a path the model overlooked still survives.
    """

    def gather(key: str, cap: int) -> list[str]:
        out: list[str] = []
        for p in parts:
            for item in p.get(key) or []:
                s = str(item).strip()
                if s and s not in out:
                    out.append(s)
        return out[-cap:]

    def block(items: list[str], fallback: str = "(none)") -> str:
        return "\n".join(f"- {x}" for x in items) or fallback

    latest_idx = _latest_user_text_index(messages)
    latest_user = _msg_text(messages[latest_idx]) if latest_idx is not None else ""
    goal = _clip(latest_user, 600) or "; ".join(gather("goal", 3)) or "(none captured)"
    state_items = gather("state", 4)
    recent_state = state_items[-1] if state_items else "(see recent messages below)"
    next_actions = block(
        gather("next_actions", 6),
        "Continue the current task with the recent messages below.",
    )
    ref_range = f"messages {middle[0]}–{middle[-1]}" if middle else "(none)"
    file_refs = gather("file_references", 20)
    for path in _file_references(messages, middle):
        if path not in file_refs:
            file_refs.append(path)
    file_refs = file_refs[:20]

    # Neutral, non-steering header: states only WHAT this block is, so the
    # agent reads the sections as compressed history (not a new instruction)
    # and still decides for itself whether to continue, stop, or ask —
    # exactly as it would have with the full history.
    return (
        f"{SUMMARY_MARKER}\n\n"
        f"The following is a faithful, compressed summary of earlier turns in this "
        f"same conversation; the most recent messages remain verbatim below it.\n\n"
        f"Current goal:\n{goal}\n\n"
        f"User constraints:\n{block(gather('constraints', 8), '(none captured)')}\n\n"
        f"Architecture facts:\n{block(gather('architecture_facts', 10), '(none captured)')}\n\n"
        f"Important decisions:\n{block(gather('decisions', 10), '(none captured)')}\n\n"
        f"Tool findings:\n{block(gather('tool_findings', 12), '(none captured)')}\n\n"
        f"Errors / unresolved issues:\n{block(gather('errors', 8))}\n\n"
        f"File references:\n{block(file_refs, '(none captured)')}\n\n"
        f"Recent state:\n{_clip(recent_state, 400)}\n\n"
        f"Next actions:\n{next_actions}\n\n"
        f"Raw references:\nraw snapshot #{raw_snapshot_id}, {ref_range}"
    )


async def plan_compaction_llm(
    agent: Any,
    messages: list,
    cfg: CompactionConfig,
    raw_snapshot_id: int | None,
    *,
    agent_name: str = "",
) -> dict | None:
    """The compaction plan, summary written by the DEDICATED compactor
    agent (``compactor_model`` setting; falls back to the origin agent's
    own LLM when unset). Builds on the structural zones in ``_plan_zones``.

    The middle zone is summarized by ONE call when it fits the configured
    fraction (``compactor_input_ratio``) of the COMPACTOR's window,
    otherwise split into pair-safe chunks summarized CONCURRENTLY and
    merged deterministically (the map-reduce path for contexts larger
    than any single compactor call). Raises on any LLM/parsing failure —
    the caller fails loud (no rule-based fallback).
    """
    zones = _plan_zones(messages, cfg)
    if zones is None:
        return None
    _head_end, tail_start, keep, middle = zones
    if not middle:
        return None

    llm, compactor_window, compactor_label = _get_compactor_llm(agent, cfg)

    # Chunk sizing uses the COMPACTOR's window (a bigger compactor model
    # feeds more history per call). Unknown window only degrades sizing,
    # never blocks recovery — use the conservative constant.
    window = (
        compactor_window
        or _context_token_limit(agent, cfg, agent_name)
        or _FALLBACK_CONTEXT_TOKENS
    )
    chunk_tokens = max(_COMPACTOR_MIN_CHUNK_TOKENS, int(window * cfg.compactor_input_ratio))
    chunks = _pair_safe_chunks(messages, middle, chunk_tokens)

    async def summarize(part_no: int, idxs: list[int]) -> dict:
        prompt = _CHUNK_PROMPT.format(
            part=part_no, total=len(chunks), body=_render_for_summary(messages, idxs)
        )
        raw = await _llm_one_shot(llm, prompt)
        return _parse_chunk_json(raw)

    # Concurrent calls on one LLM instance are safe here: each generate()
    # is an independent HTTP request; shared instance state is limited to
    # the usage accumulator (append-only) and stream listeners (detached
    # once around the whole batch).
    with _detached_stream_listeners(llm):
        parts = await asyncio.gather(*(summarize(n + 1, c) for n, c in enumerate(chunks)))

    plan = _plan_skeleton(keep, middle, _tail_truncations(messages, tail_start, cfg), raw_snapshot_id)
    plan.update(
        {
            "summary_message": _merge_chunk_sections(list(parts), messages, middle, raw_snapshot_id),
            "compactor": "llm",
            "compactor_model": compactor_label,
            "compactor_chunks": len(chunks),
            "risks": [
                "LLM summary may omit details — the raw snapshot preserves everything",
            ],
            "confidence": 0.8,
        }
    )
    return plan


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
# Smallest context window observed per agent this session (gateway combos
# rotate real models) and agents already loudly alerted about an unknown
# window (alert once per session, not once per LLM call).
_min_window_seen: dict[str, int] = {}
_window_unknown_alerted: set[str] = set()


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
    _min_window_seen.clear()
    _window_unknown_alerted.clear()
    _compactor_llm_cache.clear()


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


def _alert_window_unknown(
    agent: Any,
    history: list,
    cfg: CompactionConfig,
    *,
    agent_name: str,
    run_id: str,
    session_id: str | None,
    team_name: str | None,
) -> None:
    """Surface "auto-compaction cannot run: window unknown" everywhere the
    operator looks: ERROR log, a failed event row (shows inline on the
    agent's Context Versions tab), and the SSE failure toast.
    """
    if agent_name in _window_unknown_alerted:
        return
    _window_unknown_alerted.add(agent_name)
    model = None
    try:
        model = getattr(getattr(agent, "usage_accumulator", None), "model", None)
    except Exception:
        pass
    error = (
        f"context window unknown for model '{model or 'unknown'}' — auto-compaction is OFF "
        "for this agent. Fix: set max_context_tokens in Settings → Context Compaction, "
        "or use a model known to ModelDatabase. (The window is auto-detected from the "
        "first provider response when the gateway reports a known serving model.)"
    )
    logger.error("[COMPACT] %s (%s)", error, agent_name)
    try:
        from services.context_persistence import save_compaction_event

        save_compaction_event(
            agent_name=agent_name, run_id=run_id, session_id=session_id,
            team_name=team_name, status="failed", error_message=error,
            trigger="auto_threshold",
            message_count_before=len(history),
            estimated_tokens_before=estimate_tokens(history),
            policy_version=POLICY_VERSION,
        )
    except Exception as exc:
        logger.debug("[COMPACT] window-unknown event not recorded: %s", exc)
    _emit_status(cfg, "context_compaction_failed", agent_name, run_id, {"error": error})


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

        if reason not in ("manual", "context_overflow"):
            limit = _context_token_limit(agent, cfg, agent_name)
            current, source = _current_context_tokens(agent, history)
            if limit is None:
                # Window not configured and not yet detected. Before the
                # FIRST provider response the serving-model window is
                # simply unknown *yet* (it is read from response.model
                # after a call) — not a misconfiguration, so stay silent
                # and recheck next call. Only once a response has arrived
                # (source == "provider") and the window is STILL unknown
                # do we fail LOUD — a guessed limit either compacts
                # prematurely or never, so we surface the misconfig
                # (log + failed event + SSE toast) once per agent/session
                # rather than silently guessing.
                if source == "provider":
                    _alert_window_unknown(
                        agent, history, cfg,
                        agent_name=agent_name, run_id=run_id,
                        session_id=session_id, team_name=team_name,
                    )
                return None
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

    # 0. Feasibility BEFORE any event or snapshot (PR #85 review F1): a
    #    too-small middle zone must be a silent no-op — emitting
    #    ``started`` here with no terminal event would leave the UI
    #    banner stuck, and snapshotting would write an orphan raw row
    #    on every LLM call. ``_plan_zones`` is deterministic and
    #    LLM-free, so this stays a cheap pre-check (no compactor call
    #    is spent on a history that has nothing to compact).
    if _plan_zones(history, cfg) is None:
        logger.info("[COMPACT] Nothing to compact for %s (middle zone too small)", agent_name)
        return None

    tokens_before = estimate_tokens(history)
    raw_snapshot_id: int | None = None
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

    # From here on, ``started`` has been emitted — every exit path below
    # MUST be a completed event or a ``_fail`` (terminal failed event),
    # or the UI banner sticks. The blanket except enforces that for
    # anything unforeseen.
    try:
        # 1. Raw snapshot FIRST — without the audit copy we refuse to
        #    touch the history (non-negotiable: raw is never lost).
        raw_snapshot_id = await save_agent_context(
            agent, run_id or f"compact-{int(time.time())}", trigger="pre_compaction",
            agent_name=agent_name, session_id=session_id, team_name=team_name,
        )
        if raw_snapshot_id is None:
            return _fail("raw snapshot could not be saved — compaction aborted")

        # 2. Plan — the LLM compactor (dedicated compactor agent, or the
        #    origin agent's own LLM when ``compactor_model`` is unset) is
        #    THE only compactor: it reads the dropped turns, so the
        #    summary actually carries the task state. There is NO
        #    rule-based fallback — a compaction that cannot run fails LOUD
        #    (failed event + SSE toast); the agent continues on the raw
        #    context (and, for overflow, the original error then
        #    propagates). Silently substituting a semantically-blind
        #    summary would hide the outage and is exactly what the
        #    product decision rejects.
        try:
            plan = await plan_compaction_llm(
                agent, history, cfg, raw_snapshot_id, agent_name=agent_name
            )
        except Exception as exc:
            logger.warning("[COMPACT] LLM compactor failed for %s: %s", agent_name, exc)
            return _fail(f"LLM compactor failed: {exc}")
        if plan is None:
            return _fail("LLM compactor produced no plan after a feasible pre-check")

        # 3. Build working context from copies.
        working = build_working_context(history, plan)

        # 4. Validate — the live history is mutated only after this passes.
        violations = validate_working_context(history, working, cfg, reason=reason)
        if violations:
            return _fail(
                "plan rejected: " + "; ".join(violations),
                plan=plan, validation=violations,
            )

        tokens_after = estimate_tokens(working)

        # 5. Apply atomically (load_message_history deep-copies the input).
        agent.load_message_history(working)
    except Exception as exc:
        logger.error("[COMPACT] Pipeline error for %s: %s", agent_name, exc, exc_info=True)
        return _fail(f"unexpected compaction error: {exc}")

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

    ``on_context_overflow`` hook: emergency compaction + single retry
    when a call overflowed the serving model's window anyway (gateway
    combos can route to a smaller model mid-conversation).
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

    async def on_context_overflow(runner, error) -> bool:
        """The LLM call overflowed the serving model's window (e.g. the
        gateway routed to a smaller model mid-conversation). Compact NOW
        — threshold and grow-memo bypassed — and let the runner reissue
        the call on the rebuilt payload. False propagates the error.
        """
        try:
            from services.sse_progress import current_run_id, normalize_agent_name

            agent = runner._agent
            raw_name = getattr(agent, "name", "Agent")
            logger.warning(
                "[COMPACT] Context overflow for %s (%s) — attempting emergency compaction",
                raw_name, error,
            )
            stats = await maybe_compact_agent(
                agent,
                agent_name=normalize_agent_name(raw_name),
                run_id=current_run_id.get() or "",
                reason="context_overflow",
            )
            return stats is not None
        except Exception as exc:
            logger.error("[COMPACT] on_context_overflow hook error: %s", exc, exc_info=True)
            return False

    return ToolRunnerHooks(
        before_llm_call=on_before_llm_call,
        on_context_overflow=on_context_overflow,
    )


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
