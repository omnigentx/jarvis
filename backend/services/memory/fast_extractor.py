"""Fast-lane memory extractor — cheap LLM, low context, self-contained facts.

Replaces the brittle embedding-prototype auto-capture (which captured raw user
messages → near-duplicate spam and stored questions as facts). The LLM is the
precise decider: it reads a short recent snippet and extracts DURABLE user
facts / preferences / standing instructions, plus the entities they mention
(for the graph). A frequency gate (debounce, in the caller) controls cost, not a
content classifier. The prompt explicitly rejects trivia, which is what kills
the spam at the source.

The LLM call is injectable (``generate_fn``) so tests drive it with a scripted
PassthroughLLM (true playback — real generate → real parse → real candidate
write), no network.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("memory.fast_extractor")

# kind (LLM vocabulary) → our memory_type taxonomy. Fast-lane kinds (preference/
# fact/instruction) + slow-lane synthesis kinds (workflow/procedural/episodic/
# decision).
_TYPE_MAP = {"preference": "semantic", "fact": "semantic", "instruction": "pinned",
             "workflow": "procedural", "procedural": "procedural",
             "episodic": "episodic", "decision": "semantic"}

EXTRACTION_PROMPT = """You extract DURABLE long-term memories about the USER from a chat snippet, for a personal assistant.

Return ONLY a JSON array (no prose, no code fence). Each item:
{"kind": "preference|fact|instruction", "content": "<clear third-person statement>", "entities": [{"name": "<entity>", "etype": "person|org|place|topic"}], "confidence": 0.0-1.0}

Extract ONLY things worth remembering for months:
- stable personal facts (job, employer, location, family, name)
- durable preferences / habits (likes, dislikes, routines)
- standing instructions for the assistant ("from now on always …")

REJECT — return nothing for: greetings, small talk, one-off task requests, transient state, questions, and anything trivial / obvious / easily re-derived. When in doubt, OMIT. Prefer missing over noise.

If nothing durable, return exactly: []
"""
# NOTE: the prompt embeds literal JSON braces, so build the final prompt by
# CONCATENATION — never str.format()/% (they'd parse `{"kind"...}` as a field).


@dataclass
class ExtractedMemory:
    kind: str
    content: str
    entities: list[dict] = field(default_factory=list)
    confidence: float = 0.6

    @property
    def memory_type(self) -> str:
        return _TYPE_MAP.get(self.kind, "semantic")


def parse_extraction(raw: str) -> list[ExtractedMemory]:
    """Tolerant parse of the LLM's JSON array. A malformed reply yields [] (the
    extractor is best-effort; a bad batch is dropped, not crashed)."""
    if not raw:
        return []
    text = raw.strip()
    if text.startswith("```"):                     # strip code fences if present
        text = text.strip("`")
        text = text[text.find("["):] if "[" in text else text
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return []
    try:
        items = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return []
    out: list[ExtractedMemory] = []
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        content = str(it.get("content") or "").strip()
        if not content:
            continue
        ents = [e for e in (it.get("entities") or []) if isinstance(e, dict) and e.get("name")]
        try:
            conf = float(it.get("confidence", 0.6))
        except (ValueError, TypeError):
            conf = 0.6
        out.append(ExtractedMemory(kind=str(it.get("kind") or "fact").lower().strip(),
                                   content=content, entities=ents, confidence=conf))
    return out


async def run_fast_extraction(owner: str, snippet: str, cfg, *, generate_fn=None) -> list[str]:
    """Extract durable memories from ``snippet`` and propose them as candidates.

    ``generate_fn`` is an async ``str -> str`` LLM call; defaults to the
    configured extractor model. Returns the created candidate ids (empty when
    nothing durable / no LLM available)."""
    if not owner or not (snippet or "").strip():
        return []
    generate_fn = generate_fn or build_extractor_generate_fn()
    if generate_fn is None:
        return []
    try:
        raw = await generate_fn(EXTRACTION_PROMPT + "\n\nConversation snippet:\n" + snippet)
    except Exception as exc:  # noqa: BLE001 — extractor is best-effort
        logger.debug("[MEMORY] fast extractor LLM failed: %s", exc)
        return []
    return _persist_candidates(owner, parse_extraction(raw), cfg, "extracted")


def _persist_candidates(owner, mems, cfg, candidate_type: str) -> list[str]:
    """Propose extracted memories as candidates (ADD-only path). Shared by the
    fast and slow lanes."""
    if not mems:
        return []
    from core.database import get_db_session
    from services.memory import candidate_service as cnd
    ids: list[str] = []
    db = get_db_session()
    try:
        for m in mems:
            try:
                cand = cnd.create_candidate(
                    db, owner_agent_name=owner, candidate_type=candidate_type,
                    payload={"memory_type": m.memory_type, "content": m.content,
                             "subject_scope": "user", "authority": "agent_observed",
                             "confidence": m.confidence, "entities": m.entities},
                    requires_approval=(cfg.approval_policy != "auto_low_risk"),
                    pinned_token_budget=getattr(cfg, "pinned_token_budget", 1500))
                ids.append(cand.id)
            except Exception as exc:  # noqa: BLE001 — one bad item never drops the rest
                logger.debug("[MEMORY] candidate from extraction failed: %s", exc)
    finally:
        db.close()
    return ids


def build_extractor_generate_fn(category: str = "memory:fast"):
    """Async ``str -> str`` LLM call on the configured extractor model. Mirrors
    ``conflict.build_curator``'s resolution (inherit main LLM, or an explicit
    cheap model via the curator settings). Returns None at boot/in tests with no
    agent context — callers then no-op."""
    try:
        import services.shared_state as state
        from services.memory.settings import get_curator_api_key, get_memory_settings
        agents = getattr(state.agent_app, "_agents", {}) or {}
        agent = next(iter(agents.values()), None)
        if agent is None:
            return None
        cfg = get_memory_settings()
        provider = (cfg.curator_provider or "").strip()
        model = (cfg.curator_model or "").strip()
        ctx = getattr(agent, "_context", None) or getattr(agent, "context", None)
        main_llm = getattr(agent, "_llm", None)

        if (not provider and not model) or (provider and not model):
            llm = main_llm
        else:
            from fast_agent.agents.agent_types import AgentConfig
            from fast_agent.agents.llm_agent import LlmAgent
            from fast_agent.llm.model_factory import ModelFactory
            from services.memory.conflict import _ctx_with_override
            if provider:
                spec = f"{provider}.{model}"
                api_key = get_curator_api_key()
                use_ctx = (_ctx_with_override(ctx, provider, cfg.curator_base_url, api_key)
                           if (cfg.curator_base_url or "").strip() or api_key else ctx)
            else:
                spec, use_ctx = model, ctx
            shell = LlmAgent(AgentConfig(name="memory-extractor"), context=use_ctx)
            llm = ModelFactory.create_factory(spec)(shell)
        if llm is None:
            return None

        from fast_agent.core.prompt import Prompt

        async def generate_fn(prompt: str) -> str:
            resp = await llm.generate([Prompt.user(prompt)], request_params=None, tools=None)
            # Token attribution: record this "silent" extractor call under a
            # MEMORY agent name so the Token usage view can show/filter memory
            # spend separately from normal agents (user requirement).
            try:
                from services.sse_progress import (_get_token_info,
                                                   _persist_and_broadcast_token_usage)
                toks = _get_token_info(llm)
                if toks:
                    _persist_and_broadcast_token_usage(category, "", toks)
            except Exception:  # noqa: BLE001 — never break extraction over telemetry
                pass
            parts = [getattr(b, "text", "") for b in (getattr(resp, "content", None) or [])]
            return "\n".join(p for p in parts if p)
        return generate_fn
    except Exception as exc:  # noqa: BLE001
        logger.debug("[MEMORY] extractor unavailable: %s", exc)
        return None


# ── Slow lane — synthesis over a whole conversation, piggybacks compaction ──

SLOW_EXTRACTION_PROMPT = """You extract DURABLE long-term memories that require SYNTHESIS across a whole conversation, for a personal assistant.

Return ONLY a JSON array (no prose, no code fence). Each item:
{"kind": "workflow|procedural|episodic|decision", "content": "<clear third-person statement>", "entities": [{"name": "<entity>", "etype": "person|org|place|topic"}], "confidence": 0.0-1.0}

Extract ONLY synthesized, multi-turn memories:
- reusable workflows / procedures the user established
- decisions reached after discussion
- episodic summaries of what was accomplished

DO NOT extract simple personal facts or preferences (a separate fast pass handles those). REJECT trivia, one-offs, transient state. If nothing durable, return: []
"""


async def run_slow_extraction(owner: str, snippet: str, cfg, *, generate_fn=None) -> list[str]:
    """Slow lane: synthesize workflow/procedural/episodic memories from a whole
    conversation segment. Capable model, more context, runs rarely (compaction
    cadence). Returns created candidate ids."""
    if not owner or not (snippet or "").strip():
        return []
    generate_fn = generate_fn or build_extractor_generate_fn("memory:slow")
    if generate_fn is None:
        return []
    try:
        raw = await generate_fn(SLOW_EXTRACTION_PROMPT + "\n\nConversation:\n" + snippet)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[MEMORY] slow extractor LLM failed: %s", exc)
        return []
    return _persist_candidates(owner, parse_extraction(raw), cfg, "extracted_slow")


def _history_to_text(history, max_msgs: int = 40) -> str:
    lines = []
    try:
        from services.memory.retrieval_hook import is_injected_memory
    except Exception:  # noqa: BLE001
        is_injected_memory = lambda m: False  # noqa: E731
    for m in (history or [])[-max_msgs:]:
        if is_injected_memory(m):
            continue
        parts = [getattr(b, "text", "") for b in (getattr(m, "content", None) or [])]
        txt = " ".join(p for p in parts if p).strip()
        if txt:
            lines.append(f"{getattr(m, 'role', 'user')}: {txt}")
    return "\n".join(lines)


def fire_slow_extraction_from_history(agent_name: str, history) -> None:
    """Fire-and-forget slow extraction from a compaction segment. Called by the
    compaction hook at the threshold (the composite trigger: >50% context + many
    turns) so synthesis runs over the segment about to be summarized away. Gated
    by memory.enabled + auto_capture; best-effort, never raises into compaction."""
    try:
        import asyncio

        from helpers.agent_identity import normalize_agent_name
        from services.memory.settings import get_memory_settings
        cfg = get_memory_settings()
        if not (cfg.enabled and cfg.auto_capture_preferences):
            return
        owner = normalize_agent_name(agent_name or "")
        snippet = _history_to_text(history)
        if not owner or not snippet.strip():
            return

        async def _bg():
            try:
                await run_slow_extraction(owner, snippet, cfg)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[MEMORY] slow extraction failed: %s", exc)
        asyncio.create_task(_bg())
    except Exception as exc:  # noqa: BLE001
        logger.debug("[MEMORY] slow extraction trigger skipped: %s", exc)
