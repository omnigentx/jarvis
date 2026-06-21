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

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger("memory.fast_extractor")


def _ctx_with_override(ctx, provider: str, base_url: str | None, api_key: str | None):
    """A context copy whose ``config.<provider>`` carries the extractor model's
    own base_url/api_key, so an extractor on a separate endpoint never mutates
    the main agent's provider config."""
    import copy as _copy
    ctx2 = _copy.copy(ctx)
    ctx2.config = ctx.config.model_copy(deep=True)
    prov_cfg = getattr(ctx2.config, provider, None)
    if prov_cfg is not None:
        if base_url:
            prov_cfg.base_url = base_url
        if api_key:
            prov_cfg.api_key = api_key
    return ctx2

# kind (LLM vocabulary) → our memory_type taxonomy. Fast-lane kinds (preference/
# fact/instruction) + slow-lane synthesis kinds (workflow/procedural/episodic/
# decision).
_TYPE_MAP = {"preference": "semantic", "fact": "semantic", "instruction": "pinned",
             "workflow": "procedural", "procedural": "procedural",
             "episodic": "episodic", "decision": "semantic"}

EXTRACTION_PROMPT = """You extract DURABLE long-term memories about the USER from a chat snippet, for a personal assistant.

Return ONLY a JSON array (no prose, no code fence). Each item:
{"kind": "preference|fact|instruction", "content": "<clear third-person statement>", "entities": [{"name": "<entity>", "etype": "person|org|place|topic"}], "evidence_excerpt": "<exact words COPIED VERBATIM from the snippet that state this>", "reasoning_type": "direct|synthesis|inference"}

evidence_excerpt MUST be copied verbatim from the snippet (do NOT paraphrase it) — it is the proof. reasoning_type: "direct" = the snippet states it almost word-for-word; "synthesis" = you combined it across several turns; "inference" = reasonably inferred, not explicitly said. Do NOT output a confidence number; the system computes confidence from the verified evidence.

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
    # Evidence the BACKEND verifies (not a confidence number the LLM guesses):
    # a verbatim quote from the snippet + how the memory relates to it. Confidence
    # is DERIVED from these by services.memory.confidence — never trusted raw.
    evidence_excerpt: str = ""
    reasoning_type: str = "direct"

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
        excerpt = str(it.get("evidence_excerpt") or "").strip()
        rtype = str(it.get("reasoning_type") or "direct").lower().strip()
        out.append(ExtractedMemory(kind=str(it.get("kind") or "fact").lower().strip(),
                                   content=content, entities=ents,
                                   evidence_excerpt=excerpt, reasoning_type=rtype))
    return out


def _known_facts(owner: str, limit: int = 60) -> list[str]:
    """Facts we already hold OR have already proposed, fed to the extractor so it
    doesn't re-propose them. Two sources, both required:
    - active ``memory_records`` (already persisted), and
    - open ``memory_candidates`` (PENDING/AUTO_APPROVED/APPROVED) — a fact the
      agent's `remember` tool just proposed, or a previous extractor run in this
      same debounce window, is NOT yet an active record, so omitting it let the
      lanes re-propose the same fact (the duplicate-cards bug).
    Using the LLM's own "is this already known" judgement avoids a brittle
    embedding-similarity threshold that could miss paraphrases or drop
    genuinely-distinct facts."""
    import json as _json

    from core.database import MemoryCandidate, MemoryRecord, get_db_session
    from services.memory.models import CandidateStatus
    db = get_db_session()
    try:
        rows = (db.query(MemoryRecord.content)
                .filter(MemoryRecord.owner_agent_name == owner,
                        MemoryRecord.status == "active")
                .order_by(MemoryRecord.created_at.desc()).limit(limit).all())
        facts = [r[0][:120] for r in rows if r[0]]
        open_cands = (db.query(MemoryCandidate.payload_json)
                      .filter(MemoryCandidate.owner_agent_name == owner,
                              MemoryCandidate.status.in_([
                                  CandidateStatus.PENDING.value,
                                  CandidateStatus.AUTO_APPROVED.value,
                                  CandidateStatus.APPROVED.value]))
                      .order_by(MemoryCandidate.created_at.desc()).limit(limit).all())
        for (pj,) in open_cands:
            try:
                c = (_json.loads(pj) or {}).get("content", "")
            except (ValueError, TypeError):
                c = ""
            if c:
                facts.append(c[:120])
        return facts
    except Exception as exc:  # noqa: BLE001 — best-effort; never block extraction
        logger.warning("[MEMORY] known-facts lookup failed: %s", exc)
        return []
    finally:
        db.close()


def _extraction_prompt(owner: str) -> str:
    known = _known_facts(owner)
    if not known:
        return EXTRACTION_PROMPT
    return (EXTRACTION_PROMPT
            + "\n\nALREADY KNOWN about this user — do NOT re-extract these, nor "
              "any trivial restatement / paraphrase of them:\n"
            + "\n".join(f"- {k}" for k in known))


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
        raw = await generate_fn(_extraction_prompt(owner) + "\n\nConversation snippet:\n" + snippet)
    except Exception as exc:  # noqa: BLE001 — extractor is best-effort
        logger.warning("[MEMORY] fast extractor LLM failed: %s", exc)
        return []
    return _persist_candidates(owner, parse_extraction(raw), cfg, "extracted", snippet=snippet)


def _snippet_ref(snippet: str) -> str:
    """A stable-ish id for the extraction batch this evidence came from. The
    snippet is ephemeral (no durable message store), so this is a best-effort
    grouping key — the DURABLE proof is the verbatim excerpt in memory_sources."""
    return "snippet:" + hashlib.sha1((snippet or "").encode("utf-8")).hexdigest()[:12]


def _persist_candidates(owner, mems, cfg, candidate_type: str, snippet: str = "") -> list[str]:
    """Propose extracted memories as candidates (ADD-only path). Shared by the
    fast and slow lanes.

    Confidence is NOT taken from the LLM — it is DERIVED (services.memory.confidence)
    from evidence the backend verifies: the LLM's ``evidence_excerpt`` must appear
    verbatim in ``snippet`` (else the claim is unverified → no auto-save, route to
    approval). The verified excerpt is stored as provenance in memory_sources."""
    if not mems:
        return []
    from core.database import get_db_session
    from services.memory import candidate_service as cnd
    from services.memory import confidence as conf
    ids: list[str] = []
    now = time.time()
    db = get_db_session()
    try:
        for m in mems:
            try:
                excerpt = (m.evidence_excerpt or "").strip()
                # None = no evidence to check (shouldn't happen for the extractor,
                # but stays robust); True/False = verified / fabricated.
                excerpt_ok = conf.evidence_supports(excerpt, snippet) if excerpt else None
                verdict = conf.assess_confidence(
                    reasoning_type=m.reasoning_type, excerpt_ok=excerpt_ok,
                    authority="agent_observed")
                # Store the VERIFIED quote as durable provenance (was empty before).
                sources = ([{"source_type": "conversation_snippet",
                             "source_id": _snippet_ref(snippet),
                             "source_excerpt": excerpt,
                             "source_hash": hashlib.sha256(excerpt.encode("utf-8")).hexdigest()[:16],
                             "source_timestamp": now, "authority": "agent_observed"}]
                           if excerpt_ok else [])
                # subject_scope="user" is intentional and asymmetric vs the
                # compactor lane (which uses "agent:<owner>"): the fast lane
                # extracts facts/preferences ABOUT THE USER from their messages,
                # while the slow/compactor lane captures the AGENT's own
                # observations. Different subjects → exact-dedup (keyed on
                # subject_scope) correctly does NOT collapse them.
                cand = cnd.create_candidate(
                    db, owner_agent_name=owner, candidate_type=candidate_type,
                    payload={"memory_type": m.memory_type, "content": m.content,
                             "subject_scope": "user", "authority": "agent_observed",
                             "confidence": verdict.confidence, "entities": m.entities,
                             "reasoning_type": m.reasoning_type, "excerpt_ok": excerpt_ok,
                             "confidence_method": verdict.method},
                    confidence=verdict.confidence, sources=sources,
                    # Fabricated/unverified evidence never auto-saves — a human vets it.
                    requires_approval=(not verdict.auto_save_ok
                                       or cfg.approval_policy != "auto_low_risk"),
                    pinned_token_budget=getattr(cfg, "pinned_token_budget", 1500))
                ids.append(cand.id)
            except Exception as exc:  # noqa: BLE001 — one bad item never drops the rest
                logger.warning("[MEMORY] candidate from extraction failed: %s", exc)
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

        # ALWAYS build a SEPARATE extractor LLM instance (its own
        # usage_accumulator). Even on "inherit" we resolve the agent's own model
        # and spin up a fresh LlmAgent — never reuse agent._llm — because the
        # extractor's fire-and-forget turn would otherwise become the agent's
        # turns[-1], which compaction reads for its threshold (a small extractor
        # turn deflates the context estimate → compaction skipped) and would
        # double-count the agent's tokens.
        if provider and model:
            spec = f"{provider}.{model}"
            api_key = get_curator_api_key()
            use_ctx = (_ctx_with_override(ctx, provider, cfg.curator_base_url, api_key)
                       if (cfg.curator_base_url or "").strip() or api_key else ctx)
        else:                                   # inherit (or provider w/o model)
            spec = model or getattr(
                getattr(main_llm, "default_request_params", None), "model", None)
            use_ctx = ctx

        llm = None
        if spec and use_ctx is not None:
            try:
                from fast_agent.agents.agent_types import AgentConfig
                from fast_agent.agents.llm_agent import LlmAgent
                from fast_agent.llm.model_factory import ModelFactory
                shell = LlmAgent(AgentConfig(name="memory-extractor"), context=use_ctx)
                llm = ModelFactory.create_factory(spec)(shell)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[MEMORY] dedicated extractor LLM failed (%s); using agent LLM", exc)
        if llm is None:                         # last resort only (model unresolvable)
            llm = main_llm
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
                    _persist_and_broadcast_token_usage(
                        "memory-extractor", "", toks, category=category)
            except Exception as exc:  # noqa: BLE001 — never break extraction over telemetry
                logger.debug("[MEMORY] extractor token telemetry skipped: %s", exc)
            parts = [getattr(b, "text", "") for b in (getattr(resp, "content", None) or [])]
            return "\n".join(p for p in parts if p)
        return generate_fn
    except Exception as exc:  # noqa: BLE001
        logger.debug("[MEMORY] extractor unavailable: %s", exc)
        return None


# ── Slow lane — synthesis over a whole conversation, piggybacks compaction ──

SLOW_EXTRACTION_PROMPT = """You extract DURABLE long-term memories that require SYNTHESIS across a whole conversation, for a personal assistant.

Return ONLY a JSON array (no prose, no code fence). Each item:
{"kind": "workflow|procedural|episodic|decision", "content": "<clear third-person statement>", "entities": [{"name": "<entity>", "etype": "person|org|place|topic"}], "evidence_excerpt": "<exact words COPIED VERBATIM from the conversation that support this>", "reasoning_type": "direct|synthesis|inference"}

evidence_excerpt MUST be copied verbatim from the conversation (the proof). These are synthesized memories, so reasoning_type is usually "synthesis" (combined across turns) — use "direct" only if stated almost word-for-word. Do NOT output a confidence number; the system computes confidence from the verified evidence.

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
        logger.warning("[MEMORY] slow extractor LLM failed: %s", exc)
        return []
    return _persist_candidates(owner, parse_extraction(raw), cfg, "extracted_slow", snippet=snippet)


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
                logger.warning("[MEMORY] slow extraction failed: %s", exc)
        asyncio.create_task(_bg())
    except Exception as exc:  # noqa: BLE001
        logger.debug("[MEMORY] slow extraction trigger skipped: %s", exc)
