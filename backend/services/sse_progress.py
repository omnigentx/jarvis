"""
SSE Progress Events for Jarvis v3 — Enhanced

Provides rich, real-time progress updates during agent processing via Server-Sent Events.
Uses FastAgent's ToolRunnerHooks API — NO modifications to FastAgent library needed.

Architecture:
  1. ProgressEventManager manages per-request asyncio.Queue
  2. Hook functions push detailed events with tool names, args, results, tokens, durations
  3. /chat-stream SSE endpoint yields events from queue
"""

import asyncio
import json
import time
import logging
import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Optional

from fast_agent.agents.tool_runner import ToolRunnerHooks
from fast_agent.types import PromptMessageExtended

from services.agent_message_stream import emit_message_history_delta

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# Token-persistence is always-on (a default hook attached at app
# startup — see ``server.py`` lifespan). The default hook needs
# a ``run_id`` to tag each ``token_usage`` row so the dashboard
# can correlate rows back to the request/job that produced them.
#
# Callers (chat.py, ws_voice.py, inject.py, cron_scheduler.py)
# set this ContextVar around their ``agent_app.send`` / ``resume_and_send``
# / ``agent.generate`` call. The default hook reads it at call
# time. ContextVar is asyncio-aware: each request task sees only
# its own value even when many requests are inflight in parallel.
#
# Empty string is OK when no caller set it (legacy path): the row
# still gets written, just without run_id correlation.
# ──────────────────────────────────────────────────────────────
current_run_id: ContextVar[str] = ContextVar("current_run_id", default="")

# Lazy import to avoid circular — resolved at first use
_activity_stream = None
def _get_activity_stream():
    global _activity_stream
    if _activity_stream is None:
        from services.activity_stream import activity_stream_manager
        _activity_stream = activity_stream_manager
    return _activity_stream


def _persist_activity(
    agent_name: str, event_type: str, message: str,
    run_id: str | None = None, data: dict | None = None,
    session_id: str | None = None,
) -> None:
    """Persist an in-process agent event to agent_activities table.
    
    This mirrors SpawnProgressBridge._persist_activity() so that
    in-process agents (Jarvis, etc.) also have persistent activity
    history queryable after page reload.
    """
    try:
        from core.database import AgentActivity, get_db_session
        import json as _json

        db = get_db_session()
        try:
            activity = AgentActivity(
                agent_name=agent_name,
                run_id=run_id,
                session_id=session_id,
                event_type=event_type,
                message=message,
                data_json=_json.dumps(data, ensure_ascii=False, default=str) if data else None,
                created_at=time.time(),
            )
            db.add(activity)
            db.commit()
        except Exception as e:
            db.rollback()
            logger.debug("Failed to persist activity: %s", e)
        finally:
            db.close()
    except Exception as e:
        logger.debug("Could not import DB for activity persistence: %s", e)


# --- Agent Name Mapping ---

def normalize_agent_name(name: str) -> str:
    """Strip instance suffix like [1], [2] from agent names.
    e.g. 'FinanceAgent[1]' -> 'FinanceAgent'
    This ensures Activity Stream agent_name matches REST API names.
    """
    return re.sub(r'\[\d+\]$', '', name)


def humanize_agent_name(name: str) -> str:
    """Convert CamelCase agent names to TTS-friendly spaced names.
    e.g. 'FinanceAgent' -> 'Finance Agent', 'IoTAgent' -> 'IoT Agent'
    """
    # Strip instance suffix like [1], [2]
    base = normalize_agent_name(name)
    # Insert space before uppercase letters (but not consecutive like IoT)
    spaced = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', base)
    # Restore instance suffix
    suffix = re.search(r'\[\d+\]$', name)
    if suffix:
        spaced += f" {suffix.group()}"
    return spaced


# --- Event Types ---

def _make_event(event_type: str, data: dict) -> dict:
    """Create SSE event payload."""
    # Humanize agent name if present
    if 'agent' in data:
        data['agent_display'] = humanize_agent_name(data['agent'])
    return {"type": event_type, **data, "timestamp": time.time()}


def _get_token_info(agent) -> Optional[dict]:
    """Extract token usage from agent's UsageAccumulator (latest turn).
    Returns full token breakdown for SSE events."""
    try:
        acc = getattr(agent, 'usage_accumulator', None)
        if acc and acc.turns:
            last = acc.turns[-1]
            cache = getattr(last, 'cache_usage', None)
            cache_hit = getattr(cache, 'cache_hit_tokens', 0) if cache else 0
            cache_read = getattr(cache, 'cache_read_tokens', 0) if cache else 0
            cache_write = getattr(cache, 'cache_write_tokens', 0) if cache else 0
            reasoning = getattr(last, 'reasoning_tokens', 0)
            logger.info(
                f"[TOKEN_DEBUG] model={last.model} "
                f"in={last.input_tokens} out={last.output_tokens} total={last.total_tokens} "
                f"cache_hit={cache_hit} cache_read={cache_read} cache_write={cache_write} "
                f"reasoning={reasoning} cache_obj={cache}"
            )
            return {
                "input": last.input_tokens,
                "output": last.output_tokens,
                "total": last.total_tokens,
                "model": last.model,
                "cache_hit": cache_hit,
                "cache_read": cache_read,
                "cache_write": cache_write,
                "reasoning": reasoning,
            }
    except Exception:
        pass
    return None


def _persist_and_broadcast_token_usage(
    agent_name: str, run_id: str, tokens: Optional[dict]
):
    """Persist token usage to SQLite and broadcast via activity stream.
    Called once per LLM call (on_after_llm_call) to avoid double-counting."""
    if not tokens or tokens.get("total", 0) == 0:
        return
    
    try:
        from services.pricing import estimate_cost
        from core.database import get_db, TokenUsageRecord
        
        model = tokens.get("model", "unknown")
        input_t = tokens.get("input", 0)
        output_t = tokens.get("output", 0)
        total_t = tokens.get("total", 0)
        cache_hit = tokens.get("cache_hit", 0)
        cache_read = tokens.get("cache_read", 0)
        cache_write = tokens.get("cache_write", 0)
        reasoning = tokens.get("reasoning", 0)
        
        est = estimate_cost(
            model=model,
            input_tokens=input_t,
            output_tokens=output_t,
            cache_read_tokens=cache_read + cache_hit,  # Both represent cached reads
        )
        
        # Persist to SQLite
        db = next(get_db())
        try:
            record = TokenUsageRecord(
                agent_name=agent_name,
                run_id=run_id,
                model=model,
                input_tokens=input_t,
                output_tokens=output_t,
                total_tokens=total_t,
                cache_hit_tokens=cache_hit,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
                reasoning_tokens=reasoning,
                est_cost=est,
            )
            db.add(record)
            db.commit()
        finally:
            db.close()
        
        # Broadcast to activity stream for realtime dashboard
        cached = cache_read + cache_hit
        _get_activity_stream().broadcast({
            "agent_name": agent_name,
            "event_type": "token_usage",
            "run_id": run_id,
            "timestamp": time.time(),
            "data": {
                "model": model,
                "input_tokens": input_t,
                "output_tokens": output_t,
                "total_tokens": total_t,
                "cached_tokens": cached,
                "reasoning_tokens": reasoning,
                "est_cost": est,
            },
        })
        logger.debug(
            f"[TOKEN] {agent_name} model={model} "
            f"in={input_t} out={output_t} cache={cache_read+cache_hit} cost=${est:.6f}"
        )
    except Exception as e:
        logger.warning(f"[TOKEN] Failed to persist token usage: {e}", exc_info=True)


def create_token_persistence_hooks() -> ToolRunnerHooks:
    """Always-on hook that persists every LLM call's token usage to SQLite.

    Attached at app startup to every in-process agent (see ``server.py``
    lifespan + ``attach_token_persistence_hooks_to_all``). Reads ``run_id``
    from the ``current_run_id`` ContextVar set by the caller (chat / voice
    / inject / cron). If no caller set it, the row is still written with
    an empty ``run_id`` — better than silently losing the LLM call.

    Why a separate factory (vs reusing ``create_progress_hooks``):
      - ``create_progress_hooks`` requires a per-request ``request_id`` and
        pushes events to the chat-stream SSE queue. That queue only has a
        listener for chat / voice / inject — scheduler-triggered calls have
        no SSE listener.
      - Token persistence must run for EVERY LLM call (chat, voice, inject,
        cron, future-callers). Decoupling it from the chat-stream UI lets
        every in-process LLM call get tracked exactly once, regardless of
        caller.
    """
    async def on_after_llm_call(runner, message: PromptMessageExtended) -> None:
        agent = runner._agent
        raw_name = getattr(agent, 'name', 'Agent')
        agent_name = normalize_agent_name(raw_name)
        tokens = _get_token_info(agent)
        run_id = current_run_id.get() or ""
        _persist_and_broadcast_token_usage(agent_name, run_id, tokens)

    return ToolRunnerHooks(after_llm_call=on_after_llm_call)


def attach_token_persistence_hooks_to_all(agent_app) -> int:
    """Idempotently attach the always-on token-persistence hook to every
    in-process agent in ``agent_app._agents``.

    Idempotency is per-agent: a sentinel attribute ``_jarvis_token_hook``
    is set so re-running this (e.g. after dynamic-agent reload) does not
    stack duplicate hooks. Returns the number of agents NEWLY hooked.
    """
    token_hook = create_token_persistence_hooks()
    newly = 0
    try:
        agents = getattr(agent_app, "_agents", {}) or {}
    except Exception:
        agents = {}
    for name, ag in agents.items():
        if getattr(ag, "_jarvis_token_hook", False):
            continue
        existing = getattr(ag, "tool_runner_hooks", None)
        if existing:
            ag.tool_runner_hooks = merge_hooks(existing, token_hook)
        else:
            ag.tool_runner_hooks = token_hook
        ag._jarvis_token_hook = True
        newly += 1
        logger.debug("[TOKEN] Attached default token-persistence hook to '%s'", name)
    return newly


def _get_model_name(agent) -> str:
    """Get model name from agent."""
    try:
        acc = getattr(agent, 'usage_accumulator', None)
        if acc and acc.model:
            return acc.model
        # Fallback: check llm attribute
        llm = getattr(agent, 'llm', None)
        if llm:
            model = getattr(llm, 'model', None)
            if model:
                return str(model)
    except Exception:
        pass
    return "unknown"


def _extract_tool_info(message: PromptMessageExtended) -> list[dict]:
    """Extract tool names and args from tool_calls."""
    tools = []
    if message.tool_calls:
        for tool_id, call in message.tool_calls.items():
            try:
                name = call.params.name if hasattr(call, 'params') else str(call)
                args = {}
                if hasattr(call, 'params') and hasattr(call.params, 'arguments'):
                    raw_args = call.params.arguments
                    if isinstance(raw_args, dict):
                        args = raw_args
                    elif isinstance(raw_args, str):
                        try:
                            args = json.loads(raw_args)
                        except (json.JSONDecodeError, TypeError):
                            args = {"raw": raw_args[:200]}
                tools.append({"name": name, "args": {k: str(v) for k, v in args.items()}})
            except Exception:
                tools.append({"name": str(tool_id)[:20], "args": {}})
    return tools


def _extract_result_preview(message: PromptMessageExtended) -> Optional[str]:
    """Extract a brief preview of tool results."""
    try:
        if message.tool_results:
            for tool_id, result in message.tool_results.items():
                if result.content:
                    for block in result.content:
                        text = getattr(block, 'text', None)
                        if text:
                            return text.strip()
    except Exception:
        pass
    return None


def _make_message(agent_display: str, event_type: str, details: str = "") -> str:
    """Generate human-readable message for the event."""
    msgs = {
        "thinking": f"{agent_display} thinking...",
        "tool_request": f"{agent_display} calling {details}",
        "tool_running": f"{agent_display} running {details}...",
        "tool_done": f"{agent_display} completed {details}",
        "responding": f"{agent_display} responded",
    }
    return msgs.get(event_type, f"{agent_display} {event_type}")


# --- Per-Request Queue Manager ---

class ProgressEventManager:
    """Manages asyncio.Queue per request_id for SSE streaming."""
    
    def __init__(self):
        self._queues: dict[str, asyncio.Queue] = {}
    
    def create(self, request_id: str) -> asyncio.Queue:
        q = asyncio.Queue()
        self._queues[request_id] = q
        return q
    
    def get(self, request_id: str) -> Optional[asyncio.Queue]:
        return self._queues.get(request_id)
    
    def remove(self, request_id: str):
        self._queues.pop(request_id, None)
    
    def push(self, request_id: str, event_type: str, data: dict):
        q = self._queues.get(request_id)
        if q:
            try:
                q.put_nowait(_make_event(event_type, data))
            except asyncio.QueueFull:
                logger.warning(f"SSE queue full for {request_id}")


# Singleton
progress_manager = ProgressEventManager()


# --- ToolRunnerHooks Factory ---

def create_progress_hooks(request_id: str, session_id: str | None = None) -> ToolRunnerHooks:
    """Create ToolRunnerHooks that push rich progress events to the SSE queue.
    
    Args:
        request_id: Unique ID for this chat request
        session_id: Backend conversation/session ID for linking activities
    """
    
    # Track timing for tool duration calculation
    _tool_start_times: dict[str, float] = {}
    _tool_names: dict[str, list[dict]] = {}
    
    async def on_before_llm_call(runner, messages: list[PromptMessageExtended]) -> None:
        # Broadcast "thinking" to Activity Stream (global dashboard) only.
        # This ensures agents show "running" status on the Agents page.
        # NOT persisted — no reasoning text available, only clutters history.
        agent = runner._agent
        raw_name = getattr(agent, 'name', 'Agent')
        agent_name = normalize_agent_name(raw_name)
        display = humanize_agent_name(raw_name)
        model = _get_model_name(agent)
        thinking_msg = f"💭 {display} thinking... ({model})"
        _get_activity_stream().broadcast({
            "agent_name": agent_name,
            "event_type": "thinking",
            "message": thinking_msg,
            "run_id": request_id,
            "timestamp": time.time(),
        })
        # NOT persisted — thinking has no useful content to store
    
    async def on_after_llm_call(runner, message: PromptMessageExtended) -> None:
        agent = runner._agent
        raw_name = getattr(agent, 'name', 'Agent')
        agent_name = normalize_agent_name(raw_name)
        display = humanize_agent_name(raw_name)
        tokens = _get_token_info(agent)

        # Stream the new turn(s) appended to agent.message_history.
        emit_message_history_delta(agent, agent_name, request_id)

        # NOTE: token-usage persistence is NOT done here anymore. It's
        # handled by the always-on ``create_token_persistence_hooks``
        # attached at app startup (see ``server.py`` lifespan). Doing
        # it here would double-count when both hook chains fire.
        # The ``tokens`` dict above is still passed to progress events
        # below so the chat UI can show realtime token usage.

        # ── chat-stream progress events (powers the chat UI progress widgets) ──
        # Activity-stream tool_call/response broadcasts were removed: the
        # message_turn channel above is the canonical source for monitor UI.
        # ``_persist_activity`` rows are still written for the legacy
        # AgentDetail "Activity" tab and for audit.
        if message.tool_calls:
            tools_info = _extract_tool_info(message)
            tool_names_str = ", ".join(t["name"] for t in tools_info)

            _tool_start_times[agent_name] = time.time()
            _tool_names[agent_name] = tools_info

            progress_manager.push(request_id, "tool_request", {
                "agent": agent_name,
                "tools": tools_info,
                "tokens": tokens,
                "message": _make_message(display, "tool_request", tool_names_str),
            })
            tool_call_msg = f"🔧 {display} calling {tool_names_str}"
            _persist_activity(
                agent_name, "tool_call", tool_call_msg,
                run_id=request_id, session_id=session_id,
                data={"tools": tools_info},
            )
        else:
            preview = message.last_text()
            preview_short = (preview[:300] + "...") if preview and len(preview) > 300 else preview

            progress_manager.push(request_id, "responding", {
                "agent": agent_name,
                "preview": preview_short,
                "tokens": tokens,
                "message": f"{display} responded",
            })
            response_msg = preview_short or f"{display} responded"
            _persist_activity(agent_name, "response", response_msg, run_id=request_id, session_id=session_id)
    
    async def on_before_tool_call(runner, message: PromptMessageExtended) -> None:
        agent = runner._agent
        raw_name = getattr(agent, 'name', 'Agent')
        agent_name = normalize_agent_name(raw_name)
        display = humanize_agent_name(raw_name)
        
        tools_running = _tool_names.get(agent_name, [])
        tool_str = ", ".join(t["name"] for t in tools_running) if tools_running else "tools"
        
        progress_manager.push(request_id, "tool_running", {
            "agent": agent_name,
            "tools": tools_running,
            "message": _make_message(display, "tool_running", tool_str),
        })
    
    async def on_after_tool_call(runner, message: PromptMessageExtended) -> None:
        agent = runner._agent
        raw_name = getattr(agent, 'name', 'Agent')
        agent_name = normalize_agent_name(raw_name)
        display = humanize_agent_name(raw_name)

        # Stream the tool_result turn appended after the tool ran.
        emit_message_history_delta(agent, agent_name, request_id)

        # Calculate duration
        start = _tool_start_times.pop(agent_name, None)
        duration_ms = int((time.time() - start) * 1000) if start else None
        
        # Get result preview
        result_preview = _extract_result_preview(message)
        
        tools_done = _tool_names.pop(agent_name, [])
        tool_str = ", ".join(t["name"] for t in tools_done) if tools_done else "tools"
        
        # chat-stream progress event for the chat UI's per-tool progress widget.
        # Activity-stream tool_result broadcast was removed — message_turn covers
        # the monitor UI. Persistence kept for AgentDetail's audit history tab.
        progress_manager.push(request_id, "tool_done", {
            "agent": agent_name,
            "tools": tools_done,
            "result_preview": result_preview,
            "duration_ms": duration_ms,
            "message": f"{display} completed {tool_str}" + (f" ({duration_ms/1000:.1f}s)" if duration_ms else ""),
        })
        tool_done_msg = f"✅ {display} completed {tool_str}"
        _persist_activity(
            agent_name, "tool_result", tool_done_msg,
            run_id=request_id, session_id=session_id,
            data={
                "duration_ms": duration_ms,
                "tools": tools_done,
                "result_preview": result_preview,
            },
        )
        
        # Summary log for debugging
        _dur_str = f" duration={duration_ms}ms" if duration_ms else ""
        logger.info(f"[AGENT] {agent_name} tool_done: {tool_str}{_dur_str}")
    
    return ToolRunnerHooks(
        before_llm_call=on_before_llm_call,
        after_llm_call=on_after_llm_call,
        before_tool_call=on_before_tool_call,
        after_tool_call=on_after_tool_call,
    )


def merge_hooks(a: ToolRunnerHooks, b: ToolRunnerHooks) -> ToolRunnerHooks:
    """Merge two ToolRunnerHooks — both hooks fire for each event.

    ``on_pause_cancel`` is OR'd: if either hook returns True the runner
    retries the LLM call. Lets multiple subsystems (e.g. PauseController
    and a future cancellation-aware progress tracker) coexist without
    one silently shadowing the other's retry decision.
    """

    async def _chain(fn1, fn2, *args):
        if fn1: await fn1(*args)
        if fn2: await fn2(*args)

    async def _any_true(fn1, fn2, *args):
        # Short-circuit on first True so the second hook doesn't block
        # awaiting resume if the first already decided to retry.
        if fn1 is not None and await fn1(*args):
            return True
        if fn2 is not None and await fn2(*args):
            return True
        return False

    return ToolRunnerHooks(
        before_llm_call=(lambda r, m: _chain(a.before_llm_call, b.before_llm_call, r, m))
            if a.before_llm_call or b.before_llm_call else None,
        after_llm_call=(lambda r, m: _chain(a.after_llm_call, b.after_llm_call, r, m))
            if a.after_llm_call or b.after_llm_call else None,
        before_tool_call=(lambda r, m: _chain(a.before_tool_call, b.before_tool_call, r, m))
            if a.before_tool_call or b.before_tool_call else None,
        after_tool_call=(lambda r, m: _chain(a.after_tool_call, b.after_tool_call, r, m))
            if a.after_tool_call or b.after_tool_call else None,
        after_turn_complete=(lambda r, m: _chain(a.after_turn_complete, b.after_turn_complete, r, m))
            if a.after_turn_complete or b.after_turn_complete else None,
        on_pause_cancel=(lambda r: _any_true(a.on_pause_cancel, b.on_pause_cancel, r))
            if a.on_pause_cancel or b.on_pause_cancel else None,
    )
