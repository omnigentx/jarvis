"""Auto-inject retrieval hook (spec §6, §7).

A ``before_llm_call`` hook that — when the current turn needs historical
context — retrieves relevant memory and injects a single, self-replacing
evidence block into the agent's message history right before the LLM call.
Runs AFTER the compaction hook (merged on top) so fresh evidence is never
summarized away in the same turn.

Cost model: most turns are Level 0 (no retrieval). The cheap signals
(identifier regex + English lexicon) fire with zero model cost; only when
those are silent does the multilingual embedding gate run (one short-query
embedding). Gated entirely by ``memory.enabled``; best-effort — never breaks
the LLM call.
"""
from __future__ import annotations

import asyncio
import logging
import time

from helpers.agent_identity import normalize_agent_name

logger = logging.getLogger("memory.retrieval_hook")

# Fast-lane capture is a FREQUENCY gate, not a content classifier: run the cheap
# extractor every N user turns over the recent snippet. A frequency gate cannot
# misclassify content (the brittleness we removed); the LLM is the precise
# decider, debounce just bounds cost.
EXTRACT_EVERY_N = 4

# RESERVED sentinel prefixing our injected block. Two jobs: (1) code detects an
# auto-injected memory message by this stable token — NOT by the human-readable
# prose, which is free to reword/i18n; (2) provenance so business logic can
# filter these out of "real user" messages. Never change this literal. The
# ``jarvis:provenance`` channel carries the same flag for surfaces that don't
# read content (persists through save/load history).
MEMORY_MARKER = "⟦memory:recalled⟧"
PROVENANCE_CHANNEL = "jarvis:provenance"
PROVENANCE_RECALL = "memory_recall"
# The change-gate key is stored ON the injected block (this channel) so the gate
# is derived from HISTORY, not a process-lifetime in-memory attribute — it
# therefore survives a restart / agent re-creation (otherwise a reloaded
# conversation would re-inject a duplicate block every restart).
RECALL_KEY_CHANNEL = "jarvis:recall_key"
# Recall is ALWAYS-ON now (no intent gate): retrieve across these types every
# turn and let relevance ranking decide what (if anything) is worth injecting.
RECALL_TYPES = ["episodic", "semantic", "procedural"]


def _msg_text(msg) -> str:
    parts = []
    for block in getattr(msg, "content", None) or []:
        t = getattr(block, "text", None)
        if t:
            parts.append(t)
    return "\n".join(parts)


def is_injected_memory(msg) -> bool:
    """True if ``msg`` is an auto-injected memory-recall block (not real user
    input). Detects via the provenance channel first, then the reserved sentinel
    — never the human-readable prose. Use this anywhere business logic must
    exclude injected memory (turn counting, export, analytics)."""
    channels = getattr(msg, "channels", None) or {}
    if PROVENANCE_CHANNEL in channels:
        return True
    return MEMORY_MARKER in _msg_text(msg)


def _latest_user_text(delta_messages) -> str:
    for msg in reversed(list(delta_messages or [])):
        if str(getattr(msg, "role", "")) == "user" and not is_injected_memory(msg):
            txt = _msg_text(msg).strip()
            if txt:
                return txt
    return ""


def _render_block(evidence) -> str:
    lines = [f"{MEMORY_MARKER} [System memory recall — not user input] Stored "
             f"memories that may be relevant to the user's message "
             f"(reference only; do not repeat verbatim):"]
    for e in evidence:
        lines.append(f"- [{e.memory_type}] {e.excerpt}")
    return "\n".join(lines)


def _evidence_key(evidence) -> str:
    """Stable identity of the rendered memory set — change-gate compares this to
    the previously injected set. Keyed on what is actually shown so an identical
    set produces an identical key (→ skip re-injection, keep the prefix cache)."""
    return "␟".join(
        f"{getattr(e, 'memory_type', '')}\x1f{getattr(e, 'excerpt', '')}" for e in evidence)


def _block_recall_key(msg) -> str | None:
    for c in (getattr(msg, "channels", None) or {}).get(RECALL_KEY_CHANNEL) or []:
        t = getattr(c, "text", None)
        if t is not None:
            return t
    return None


def _injected_keys(history) -> set[str]:
    """Keys of every memory block already in this conversation — the change-gate
    skips re-injecting any set that is already present (no duplicate, no churn)."""
    return {k for m in history if is_injected_memory(m)
            and (k := _block_recall_key(m)) is not None}


def _build_block_message(evidence, key: str):
    """A memory-recall message: prose in ``content`` (the LLM reads it); the
    provenance flag + change-gate key in ``channels`` (code reads them, persist
    through save/load, don't reach the LLM as content). Role stays ``user``
    framed — the standard cross-provider RAG pattern — never a plain unmarked
    user message."""
    from fast_agent.mcp.helpers.content_helpers import text_content
    from fast_agent.mcp.prompt_message_extended import PromptMessageExtended
    return PromptMessageExtended(
        role="user",
        content=[text_content(_render_block(evidence))],
        channels={PROVENANCE_CHANNEL: [text_content(PROVENANCE_RECALL)],
                  RECALL_KEY_CHANNEL: [text_content(key)]},
    )


def create_memory_retrieval_hooks():
    from fast_agent.agents.tool_runner import ToolRunnerHooks

    async def on_before_llm_call(runner, delta_messages) -> None:
        try:
            from services.memory.settings import get_memory_settings
            cfg = get_memory_settings()
            if not cfg.enabled:
                return
            agent = runner._agent
            owner = normalize_agent_name(getattr(agent, "name", "") or "")
            # The delta is usually the user's text turn, but after a tool call it
            # can be a tool-result with no text → fall back to the last real user
            # message in history so recall still fires on post-tool turns.
            query = _latest_user_text(delta_messages) or \
                _latest_user_text(getattr(agent, "message_history", []) or [])
            if not owner or not query:
                return

            # Capture (write side) — fast-lane LLM extractor, debounced every N
            # turns, fire-and-forget so it never adds latency to this LLM call.
            if cfg.auto_capture_preferences:
                cnt = getattr(agent, "_jarvis_extract_turns", 0) + 1
                if cnt >= EXTRACT_EVERY_N:
                    agent._jarvis_extract_turns = 0
                    asyncio.create_task(_run_extraction(owner, _recent_snippet(agent, query), cfg))
                else:
                    agent._jarvis_extract_turns = cnt

            # Recall — ALWAYS retrieve (no intent gate); inject only when this
            # exact set is NOT already in the conversation (change-gate, derived
            # from history → survives restart). We never strip/move earlier
            # blocks: mid-history mutation is what breaks the KV prefix cache. A
            # stable profile yields the same set → already present → no new block
            # → prefix stays warm. Lingering older blocks are bounded (one per
            # distinct relevant-set) and the DB stays correct (ADD-only); pruning
            # stale blocks is handled when slow-lane/compaction integration lands
            # (they carry the reserved sentinel + channel for that).
            evidence = await _retrieve(owner, query, RECALL_TYPES, cfg)
            if not evidence:
                return
            key = _evidence_key(evidence)
            history = list(getattr(agent, "message_history", []) or [])
            if key in _injected_keys(history):
                return                      # this set already in context
            history.append(_build_block_message(evidence, key))   # append-only at tail
            agent.load_message_history(history)
        except Exception as exc:  # noqa: BLE001 — never break the LLM call
            logger.error("[MEMORY] retrieval hook error: %s", exc, exc_info=True)

    return ToolRunnerHooks(before_llm_call=on_before_llm_call)


def _recent_snippet(agent, current_query: str, n_msgs: int = 8) -> str:
    """Recent conversation text fed to the fast-lane extractor: the last few real
    messages (injected memory blocks excluded) plus the current user turn."""
    lines = []
    for m in (getattr(agent, "message_history", []) or [])[-n_msgs:]:
        if is_injected_memory(m):
            continue
        txt = _msg_text(m).strip()
        if txt:
            lines.append(f"{getattr(m, 'role', 'user')}: {txt}")
    lines.append(f"user: {current_query}")
    return "\n".join(lines)


async def _run_extraction(owner: str, snippet: str, cfg) -> None:
    """Fire-and-forget fast-lane extraction (best-effort; never raises)."""
    try:
        from services.memory.fast_extractor import run_fast_extraction
        await run_fast_extraction(owner, snippet, cfg)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[MEMORY] fast extraction failed: %s", exc)


async def _retrieve(owner: str, query: str, targets: set[str], cfg):
    from core.database import get_db_session
    from services.retrieval.contracts import RetrievalRequest
    from services.retrieval.orchestrator import RetrievalOrchestrator
    db = get_db_session()
    try:
        orch = RetrievalOrchestrator(db, cfg)
        req = RetrievalRequest(owner_agent_name=owner, query=query,
                               types=list(targets), mode=cfg.mode)
        # The hook already decided this turn needs recall → force the fast round.
        result = await orch.retrieve(req, now=time.time(), agent_requested=True)
        return result.evidence
    finally:
        db.close()


def attach_memory_hooks_to_all(agent_app) -> int:
    """Idempotently attach the retrieval hook to every in-process agent, merged
    ON TOP of the compaction hook (so compaction runs first). Mirror of
    attach_compaction_hooks_to_all."""
    from services.sse_progress import merge_hooks

    hook = create_memory_retrieval_hooks()
    newly = 0
    agents = getattr(agent_app, "_agents", {}) or {}
    for name, ag in agents.items():
        if getattr(ag, "_jarvis_memory_hook", False):
            continue
        existing = getattr(ag, "tool_runner_hooks", None)
        ag.tool_runner_hooks = merge_hooks(existing, hook) if existing else hook
        ag._jarvis_memory_hook = True
        newly += 1
    return newly
