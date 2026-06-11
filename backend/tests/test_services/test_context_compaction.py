"""Tests for services/context_compaction.py.

Real PromptMessageExtended objects + real prompt_serialization + real
SQLite (tmp DB via SPAWN_REGISTRY_DB) — the subsystem under test is
never mocked. Only the config reader is pinned for determinism.
"""

import asyncio
import json
import sqlite3
import time

import pytest
from mcp.types import CallToolRequest, CallToolRequestParams, CallToolResult, TextContent

from fast_agent.mcp.prompt_serialization import from_json, to_json
from fast_agent.types import PromptMessageExtended

import services.context_compaction as cc
from services.context_compaction import (
    CompactionConfig,
    DEFAULTS,
    SUMMARY_MARKER,
    build_working_context,
    estimate_tokens,
    maybe_compact_agent,
    reset_compaction_guards,
    validate_config_updates,
    validate_working_context,
)
from services.context_persistence import (
    get_compaction_events_meta,
    load_latest_context_json_any,
    save_compaction_event,
)


# ── Message builders (real types, no stubs) ──


def text_msg(role: str, text: str, *, is_template: bool = False) -> PromptMessageExtended:
    return PromptMessageExtended(
        role=role,
        content=[TextContent(type="text", text=text)],
        is_template=is_template,
    )


def tool_pair(call_id: str, name: str, result_text: str, *, is_error: bool = False, arguments=None):
    """(assistant tool_calls msg, user tool_results msg) with matching id."""
    call = CallToolRequest(
        method="tools/call",
        params=CallToolRequestParams(name=name, arguments=arguments or {"q": "x"}),
    )
    assistant = PromptMessageExtended(role="assistant", tool_calls={call_id: call})
    result = CallToolResult(
        content=[TextContent(type="text", text=result_text)], isError=is_error
    )
    user = PromptMessageExtended(role="user", tool_results={call_id: result})
    return assistant, user


def build_history(n_pairs: int = 8, *, filler_chars: int = 400) -> list[PromptMessageExtended]:
    """template ×2, user request, then tool-loop pairs, then a final answer."""
    msgs = [
        text_msg("user", "SYSTEM TEMPLATE: you are a test agent", is_template=True),
        text_msg("assistant", "Acknowledged template", is_template=True),
        text_msg("user", "Please research topic X thoroughly"),
    ]
    for i in range(n_pairs):
        a, u = tool_pair(f"call_{i}", f"tool_{i % 3}", f"result {i}: " + "x" * filler_chars)
        msgs.extend([a, u])
    msgs.append(text_msg("assistant", "Interim findings: alpha beta gamma"))
    msgs.append(text_msg("user", "Now summarize what you found"))
    msgs.append(text_msg("assistant", "Working on the summary now"))
    return msgs


def cfg_with(**overrides) -> CompactionConfig:
    values = dict(DEFAULTS)
    values.update(overrides)
    return CompactionConfig(**values)


@pytest.fixture
def reg_db(tmp_path, monkeypatch):
    db = tmp_path / "spawn_registry.db"
    monkeypatch.setenv("SPAWN_REGISTRY_DB", str(db))
    reset_compaction_guards()
    yield db
    reset_compaction_guards()


class FakeUsage:
    def __init__(self, current=100000, window=120000):
        self.current_context_tokens = current
        self.context_window_size = window


class FakeAgent:
    def __init__(self, messages, usage=None, name="TestAgent"):
        self.name = name
        self._history = list(messages)
        self.usage_accumulator = usage
        # Compaction is LLM-first for every reason — give every fake
        # agent a working compactor LLM; tests for the no-LLM paths set
        # ``agent._llm = None`` explicitly.
        self._llm = FakeCompactorLLM()

    @property
    def message_history(self):
        return self._history

    def load_message_history(self, messages):
        self._history = [m.model_copy(deep=True) for m in messages]


# ── Planner ──
#
# Compaction is LLM-only. ``make_plan`` drives the real ``plan_compaction_llm``
# with a FakeCompactorLLM (a valid all-sections summary), so the structural
# machinery (zones, build, validate, truncation) is exercised exactly as in
# production. Pure-structure assertions hit ``_plan_zones`` directly.


def make_plan(msgs, cfg, raw_snapshot_id=1):
    agent = FakeAgent(msgs, usage=FakeUsage())
    return asyncio.run(
        cc.plan_compaction_llm(agent, msgs, cfg, raw_snapshot_id, agent_name="planner")
    )


def test_plan_keeps_templates_and_recent_tail():
    msgs = build_history()
    cfg = cfg_with(keep_recent_messages=4)
    plan = make_plan(msgs, cfg)
    assert plan is not None
    keep = set(plan["keep_verbatim"])
    # Templates (0,1) kept; last 4 (pair-safe) kept; middle dropped.
    assert {0, 1} <= keep
    assert set(range(len(msgs) - 4, len(msgs))) <= keep
    assert plan["delete_from_working_context"]
    assert set(plan["delete_from_working_context"]).isdisjoint(keep)
    assert plan["summary_message"].startswith(SUMMARY_MARKER)
    assert plan["compactor"] == "llm"


def test_pair_safe_cut_never_splits_tool_pair():
    msgs = build_history()
    # keep_recent=3 would cut between assistant(tool_calls) and its
    # user(tool_results) for some histories — the zone planner must extend
    # the tail until every tool_result's owner is inside it.
    for keep_recent in range(2, 9):
        zones = cc._plan_zones(msgs, cfg_with(keep_recent_messages=keep_recent))
        if zones is None:
            continue
        _h, _t, keep_list, _middle = zones
        keep = set(keep_list)
        owned = set()
        for idx in sorted(keep):
            for cid in (msgs[idx].tool_calls or {}):
                owned.add(cid)
            for cid in (msgs[idx].tool_results or {}):
                assert cid in owned, f"keep_recent={keep_recent} split pair {cid}"


def test_latest_user_request_forced_into_keep():
    msgs = build_history()
    # Long tool tail AFTER the latest user text message: append pairs so
    # "Now summarize what you found" falls outside the keep window.
    for i in range(10, 16):
        a, u = tool_pair(f"call_{i}", "tool_x", "r" * 200)
        msgs.extend([a, u])
    zones = cc._plan_zones(msgs, cfg_with(keep_recent_messages=4))
    assert zones is not None
    keep = zones[2]
    latest_user_idx = max(
        i for i, m in enumerate(msgs)
        if str(m.role) == "user" and not m.tool_results
    )
    assert latest_user_idx in keep


def test_no_plan_when_middle_too_small():
    msgs = build_history(n_pairs=1)
    assert cc._plan_zones(msgs, cfg_with(keep_recent_messages=10)) is None


# ── Build + validate ──


def test_summary_is_faithful_and_non_steering():
    # The summary must compress, not steer: it carries the actual goal and
    # is labelled as compressed history, with NO injected directive telling
    # the agent to continue/stop/wait. The agent decides for itself from
    # the goal + verbatim recent messages — so the compactor works for any
    # task state (in-progress, done, or blocked) without overfitting.
    msgs = build_history()
    plan = make_plan(msgs, cfg_with(keep_recent_messages=4))
    summary = plan["summary_message"]
    assert summary.startswith(SUMMARY_MARKER)
    # Faithful: the real latest user instruction is the goal.
    assert "Now summarize what you found" in summary
    # Labelled as compressed history (descriptive), not an instruction.
    assert "compressed summary of earlier turns" in summary
    # No hardcoded steering verdict in either direction.
    low = summary.lower()
    for steer in ("do not stop", "keep going", "task is complete", "task is finished",
                  "you are mid-task", "await the user", "task complete"):
        assert steer not in low, f"summary should not steer: {steer!r}"


def test_working_context_roundtrips_and_validates():
    msgs = build_history()
    cfg = cfg_with(keep_recent_messages=4)
    plan = make_plan(msgs, cfg)
    working = build_working_context(msgs, plan)
    assert validate_working_context(msgs, working, cfg) == []
    # Round-trip through the canonical serializer.
    restored = from_json(to_json(working))
    assert len(restored) == len(working)
    # Summary message present exactly once, after templates.
    summaries = [m for m in working if (m.content and getattr(m.content[0], "text", "").startswith(SUMMARY_MARKER))]
    assert len(summaries) == 1
    assert working[0].is_template and working[1].is_template
    # Final message preserved verbatim.
    assert to_json([working[-1]]) == to_json([msgs[-1]])


def test_validation_rejects_dropped_latest_user():
    msgs = build_history()
    cfg = cfg_with(keep_recent_messages=4)
    plan = make_plan(msgs, cfg)
    working = build_working_context(msgs, plan)
    bad = [m for m in working if "Now summarize what you found" not in (
        m.content[0].text if m.content else "")]
    errors = validate_working_context(msgs, bad, cfg)
    assert any("latest user request" in e for e in errors)


def test_validation_rejects_broken_tool_pairing():
    msgs = build_history()
    cfg = cfg_with(keep_recent_messages=4)
    plan = make_plan(msgs, cfg)
    working = build_working_context(msgs, plan)
    # Drop one tool_results message that pairs an earlier tool_calls.
    victim = next(
        i for i, m in enumerate(working[:-1]) if m.tool_results
    )
    bad = working[:victim] + working[victim + 1:]
    errors = validate_working_context(msgs, bad, cfg)
    assert any("tool" in e for e in errors)


def test_validation_rejects_missing_summary_sections():
    msgs = build_history()
    cfg = cfg_with(keep_recent_messages=4)
    plan = make_plan(msgs, cfg)
    plan["summary_message"] = SUMMARY_MARKER + "\n\nCurrent goal:\nonly this"
    working = build_working_context(msgs, plan)
    errors = validate_working_context(msgs, working, cfg)
    assert any("summary missing sections" in e for e in errors)


def test_validation_rejects_template_removal():
    msgs = build_history()
    cfg = cfg_with(keep_recent_messages=4)
    plan = make_plan(msgs, cfg)
    working = build_working_context(msgs, plan)
    bad = [m for m in working if not m.is_template]
    errors = validate_working_context(msgs, bad, cfg)
    assert any("template" in e for e in errors)


def test_validation_rejects_insufficient_savings():
    msgs = build_history()
    cfg = cfg_with(keep_recent_messages=4, min_savings_ratio=0.99)
    plan = make_plan(msgs, cfg)
    working = build_working_context(msgs, plan)
    errors = validate_working_context(msgs, working, cfg)
    assert any("savings below minimum" in e for e in errors)
    # Manual reason bypasses the savings floor.
    assert not any(
        "savings" in e
        for e in validate_working_context(msgs, working, cfg, reason="manual")
    )


def test_oversized_tail_tool_result_truncated_except_final():
    msgs = build_history()
    huge = "y" * 20000
    a, u = tool_pair("call_huge", "big_tool", huge)
    final = text_msg("assistant", "done with big tool")
    msgs.extend([a, u, final])
    cfg = cfg_with(keep_recent_messages=4, max_tool_result_tokens_in_context=500)
    plan = make_plan(msgs, cfg)
    assert any(e["action"] == "truncate_tool_result" for e in plan["summarize"])
    working = build_working_context(msgs, plan)
    truncated = next(m for m in working if m.tool_results and "call_huge" in m.tool_results)
    text = truncated.tool_results["call_huge"].content[0].text
    assert len(text) < len(huge)
    assert "truncated by context compaction" in text
    assert validate_working_context(msgs, working, cfg) == []


# ── Full pipeline against real SQLite ──


def _trigger_cfg(**overrides):
    values = {"keep_recent_messages": 4, "min_savings_ratio": 0.05}
    values.update(overrides)
    return cfg_with(**values)


def run_compact(agent, monkeypatch, cfg=None, **kwargs):
    monkeypatch.setattr(cc, "get_compaction_config", lambda: cfg or _trigger_cfg())
    return asyncio.run(
        maybe_compact_agent(agent, agent_name=agent.name, run_id="run-1", **kwargs)
    )


def test_full_compaction_flow(reg_db, monkeypatch):
    msgs = build_history()
    agent = FakeAgent(msgs, usage=FakeUsage(current=110000, window=120000))
    before_json = to_json(msgs)

    stats = run_compact(agent, monkeypatch)
    assert stats is not None
    assert stats["saved_tokens"] > 0
    assert stats["message_count_after"] < stats["message_count_before"]

    # Live history replaced and contains the summary.
    texts = [
        (m.content[0].text if m.content else "") for m in agent.message_history
    ]
    assert any(t.startswith(SUMMARY_MARKER) for t in texts)

    conn = sqlite3.connect(reg_db)
    # Raw snapshot is append-only and byte-identical to pre-compaction history.
    raw = conn.execute(
        "SELECT context_json, trigger FROM agent_context_snapshots WHERE id = ?",
        (stats["raw_snapshot_id"],),
    ).fetchone()
    assert raw[1] == "pre_compaction"
    assert raw[0] == before_json
    # Completed event row with working json + stats.
    ev = conn.execute(
        "SELECT status, working_context_json, estimated_tokens_before, estimated_tokens_after "
        "FROM context_compaction_events WHERE id = ?",
        (stats["event_id"],),
    ).fetchone()
    conn.close()
    assert ev[0] == "completed"
    assert len(from_json(ev[1])) == stats["message_count_after"]
    assert ev[2] > ev[3]


def test_below_threshold_no_compaction(reg_db, monkeypatch):
    agent = FakeAgent(build_history(), usage=FakeUsage(current=1000, window=120000))
    assert run_compact(agent, monkeypatch) is None
    assert get_compaction_events_meta(agent.name, limit=10) == []


def test_disabled_no_compaction(reg_db, monkeypatch):
    agent = FakeAgent(build_history(), usage=FakeUsage(current=110000))
    assert run_compact(agent, monkeypatch, cfg=_trigger_cfg(enabled=False)) is None


def test_failed_validation_leaves_history_untouched(reg_db, monkeypatch):
    msgs = build_history()
    agent = FakeAgent(msgs, usage=FakeUsage(current=110000))
    before_json = to_json(agent.message_history)
    # min_savings_ratio=0.99 → validator rejects the plan.
    stats = run_compact(agent, monkeypatch, cfg=_trigger_cfg(min_savings_ratio=0.99))
    assert stats is None
    assert to_json(agent.message_history) == before_json
    events = get_compaction_events_meta(agent.name, limit=10)
    assert len(events) == 1
    assert events[0]["status"] == "failed"
    assert "savings" in events[0]["error_message"]


def test_memo_guard_blocks_retry_at_same_length(reg_db, monkeypatch):
    msgs = build_history()
    agent = FakeAgent(msgs, usage=FakeUsage(current=110000))
    # First attempt fails validation (savings floor) and sets the memo.
    run_compact(agent, monkeypatch, cfg=_trigger_cfg(min_savings_ratio=0.99))
    run_compact(agent, monkeypatch, cfg=_trigger_cfg(min_savings_ratio=0.99))
    # Only ONE failed event — the second call was memo-skipped.
    assert len(get_compaction_events_meta(agent.name, limit=10)) == 1


@pytest.fixture
def sse_spy(monkeypatch):
    """Record every activity-stream broadcast from _emit_status."""
    import services.activity_stream as act

    events = []
    monkeypatch.setattr(act.activity_stream_manager, "broadcast", events.append)
    return events


def test_infeasible_plan_is_silent_noop(reg_db, monkeypatch, sse_spy):
    # PR #85 review F1: threshold exceeded but the middle zone is too
    # small to compact (keep_recent ≈ history length). Must be a TOTAL
    # no-op: no started event (stuck banner), no orphan raw snapshot,
    # no event row.
    msgs = build_history(n_pairs=2)  # 10 messages
    agent = FakeAgent(msgs, usage=FakeUsage(current=110000))
    stats = run_compact(agent, monkeypatch, cfg=_trigger_cfg(keep_recent_messages=7))
    assert stats is None
    assert sse_spy == []
    conn = sqlite3.connect(reg_db)
    # The table itself may not even exist — save_agent_context was never
    # reached, which is exactly the point.
    try:
        n_raw = conn.execute("SELECT COUNT(*) FROM agent_context_snapshots").fetchone()[0]
    except sqlite3.OperationalError:
        n_raw = 0
    conn.close()
    assert n_raw == 0
    assert get_compaction_events_meta(agent.name, limit=10) == []


def test_unexpected_pipeline_error_emits_terminal_failed(reg_db, monkeypatch, sse_spy):
    # PR #85 review F1: once ``started`` is emitted, EVERY exit path must
    # produce a terminal event — an unexpected exception included.
    msgs = build_history()
    agent = FakeAgent(msgs, usage=FakeUsage(current=110000))
    before_json = to_json(agent.message_history)
    monkeypatch.setattr(
        cc, "build_working_context",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    stats = run_compact(agent, monkeypatch)
    assert stats is None
    types = [e["event_type"] for e in sse_spy]
    assert types == ["context_compaction_started", "context_compaction_failed"]
    assert to_json(agent.message_history) == before_json
    events = get_compaction_events_meta(agent.name, limit=10)
    assert len(events) == 1 and events[0]["status"] == "failed"


def test_config_fails_closed_when_db_unreadable(monkeypatch):
    # PR #85 review F5: a history-rewriting feature must not run on
    # guessed defaults when the user's config cannot be read.
    import services.config_service as cs

    def _boom(*a, **k):
        raise RuntimeError("db gone")

    monkeypatch.setattr(cs.config_service, "get", _boom)
    cfg = cc.get_compaction_config()
    assert cfg.enabled is False


def test_out_of_range_config_value_reverts_to_default(monkeypatch):
    # PR #85 review F4: the generic settings PUT bypasses the typed PATCH
    # validation — out-of-range rows must revert to the default loudly.
    import services.config_service as cs

    def _get(category, key, default=None):
        return "5.0" if key == "compact_at_ratio" else None

    monkeypatch.setattr(cs.config_service, "get", _get)
    cfg = cc.get_compaction_config()
    assert cfg.compact_at_ratio == DEFAULTS["compact_at_ratio"]


def test_truncation_flag_is_per_block_not_joined(reg_db):
    # PR #85 review F7: many small blocks whose JOINED text exceeds the
    # cap must NOT be flagged — _truncate_tool_result only cuts blocks
    # that are individually oversized, and the diff UI must agree.
    msgs = build_history()
    call = CallToolRequest(
        method="tools/call", params=CallToolRequestParams(name="multi", arguments={})
    )
    result = CallToolResult(
        content=[TextContent(type="text", text="z" * 1500) for _ in range(5)],
        isError=False,
    )
    msgs.append(PromptMessageExtended(role="assistant", tool_calls={"call_multi": call}))
    msgs.append(PromptMessageExtended(role="user", tool_results={"call_multi": result}))
    msgs.append(text_msg("assistant", "done"))
    cfg = cfg_with(keep_recent_messages=4, max_tool_result_tokens_in_context=500)
    plan = make_plan(msgs, cfg)
    flagged = [e for e in plan["summarize"] if e["call_id"] == "call_multi"]
    assert flagged == []  # joined 7500 chars > 2000, but each block is 1500 < 2000


def test_attach_compaction_hooks_idempotent_and_merges():
    # PR #85 review F2 plumbing: the helper both server.py AND the
    # dynamic-agents reload loop call. Sentinel must prevent stacking;
    # existing hooks must be merged, not shadowed.
    class App:
        pass

    class Ag:
        pass

    app = App()
    a = Ag()
    from fast_agent.agents.tool_runner import ToolRunnerHooks

    async def noop(*_a):
        return None

    a.tool_runner_hooks = ToolRunnerHooks(after_llm_call=noop)
    app._agents = {"x": a}
    assert cc.attach_compaction_hooks_to_all(app) == 1
    assert a._jarvis_compaction_hook is True
    assert a.tool_runner_hooks.before_llm_call is not None  # ours
    assert a.tool_runner_hooks.after_llm_call is not None   # original preserved
    assert cc.attach_compaction_hooks_to_all(app) == 0      # idempotent


def test_hook_never_raises(monkeypatch):
    class BrokenRunner:
        @property
        def _agent(self):
            raise RuntimeError("boom")

    hooks = cc.create_context_compaction_hooks()
    asyncio.run(hooks.before_llm_call(BrokenRunner(), []))  # must not raise


# ── Resume source selection (newest-wins) ──


def test_resume_picks_newest_across_raw_and_working(reg_db):
    conn = sqlite3.connect(reg_db)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_context_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            agent_name TEXT NOT NULL, session_id TEXT, team_name TEXT,
            context_json TEXT NOT NULL, message_count INTEGER DEFAULT 0,
            total_input_tokens INTEGER DEFAULT 0, total_output_tokens INTEGER DEFAULT 0,
            trigger TEXT DEFAULT 'manual', created_at REAL NOT NULL)
    """)
    conn.execute(
        "INSERT INTO agent_context_snapshots (run_id, agent_name, context_json, created_at) "
        "VALUES ('r', 'A', '{\"messages\": [\"raw-old\"]}', ?)",
        (time.time() - 100,),
    )
    conn.commit()
    conn.close()

    # Working snapshot newer than raw → working wins.
    save_compaction_event(
        agent_name="A", working_context_json='{"messages": ["working-new"]}',
        status="completed",
    )
    assert "working-new" in load_latest_context_json_any("A")

    # A newer raw snapshot (post-compaction turns) must beat the working one
    # — the spec's "prefer working, fallback raw" would lose these turns.
    conn = sqlite3.connect(reg_db)
    conn.execute(
        "INSERT INTO agent_context_snapshots (run_id, agent_name, context_json, created_at) "
        "VALUES ('r', 'A', '{\"messages\": [\"raw-newest\"]}', ?)",
        (time.time() + 100,),
    )
    conn.commit()
    conn.close()
    assert "raw-newest" in load_latest_context_json_any("A")


def test_failed_working_snapshots_ignored_on_resume(reg_db):
    save_compaction_event(
        agent_name="B", working_context_json='{"messages": ["bad"]}', status="failed",
    )
    assert load_latest_context_json_any("B") is None


# ── Config validation ──


def test_validate_config_updates_ranges():
    assert validate_config_updates({"compact_at_ratio": 0.7}) == []
    assert validate_config_updates({"compact_at_ratio": 0.1})
    assert validate_config_updates({"keep_recent_messages": 1})
    assert validate_config_updates({"max_context_tokens": 0}) == []
    assert validate_config_updates({"max_context_tokens": 5000})
    assert validate_config_updates({"unknown_key": 1})
    assert validate_config_updates({})


def test_estimate_tokens_monotonic():
    small = [text_msg("user", "hi")]
    big = [text_msg("user", "hi" * 1000)]
    assert estimate_tokens(big) > estimate_tokens(small) > 0
    assert estimate_tokens([]) == 0


# ── Serving-model window detection + min-window tracking (Tier 1) ──


def test_resolve_serving_model_window_handles_gateway_dot_names():
    from fast_agent.llm.provider.openai.llm_openai import _resolve_serving_model_window

    # 9router reports dotted versions; database keys use dashes.
    assert _resolve_serving_model_window("claude-sonnet-4.5") == 200000
    assert _resolve_serving_model_window("gpt-5.5") == 272000
    # Unknown alias (the requested combo name) stays unresolved.
    assert _resolve_serving_model_window("coding-agent") is None
    assert _resolve_serving_model_window(None) is None


def test_context_token_limit_tracks_min_window_across_rotation():
    reset_compaction_guards()
    cfg = cfg_with()
    agent = FakeAgent(build_history(), usage=FakeUsage(current=0, window=272000))
    assert cc._context_token_limit(agent, cfg, "RotA") == 272000
    # Gateway routes to a smaller model → threshold follows immediately.
    agent.usage_accumulator.context_window_size = 200000
    assert cc._context_token_limit(agent, cfg, "RotA") == 200000
    # Rotation back to the big model must NOT raise the threshold again —
    # the next call could still land on the small model.
    agent.usage_accumulator.context_window_size = 272000
    assert cc._context_token_limit(agent, cfg, "RotA") == 200000
    # Window temporarily unknown → last known minimum, not the fallback.
    agent.usage_accumulator.context_window_size = None
    assert cc._context_token_limit(agent, cfg, "RotA") == 200000
    reset_compaction_guards()


def test_context_token_limit_unknown_is_none_and_explicit_override():
    reset_compaction_guards()
    agent = FakeAgent(build_history(), usage=FakeUsage(current=0, window=None))
    # No invented fallback: unknown window must be None (caller fails loud).
    assert cc._context_token_limit(agent, cfg_with(), "FreshAgent") is None
    # Explicit config beats detection entirely.
    assert (
        cc._context_token_limit(agent, cfg_with(max_context_tokens=50000), "FreshAgent")
        == 50000
    )
    reset_compaction_guards()


def test_window_unknown_before_first_response_is_silent(reg_db, monkeypatch, sse_spy):
    # current=0 → no provider response yet → the serving-model window is
    # simply not detected *yet*, not a misconfig. Must be a silent no-op:
    # no failed event, no scary toast on the user's first message.
    msgs = build_history()
    agent = FakeAgent(msgs, usage=FakeUsage(current=0, window=None))
    assert run_compact(agent, monkeypatch) is None
    assert get_compaction_events_meta(agent.name, limit=10) == []
    assert not any(e["event_type"] == "context_compaction_failed" for e in sse_spy)


def test_window_unknown_fails_loud_once_and_skips_compaction(reg_db, monkeypatch, sse_spy):
    # current>0 → a provider response HAS arrived but the window is still
    # unknown → genuine misconfig → fail loud.
    msgs = build_history()
    agent = FakeAgent(msgs, usage=FakeUsage(current=110000, window=None))
    before_json = to_json(agent.message_history)

    assert run_compact(agent, monkeypatch) is None
    # History untouched — no compaction against a guessed limit.
    assert to_json(agent.message_history) == before_json
    # LOUD: one failed event row visible on the dashboard…
    events = get_compaction_events_meta(agent.name, limit=10)
    assert len(events) == 1
    assert events[0]["status"] == "failed"
    assert "context window unknown" in events[0]["error_message"]
    # …and the SSE failure toast fired.
    assert any(e["event_type"] == "context_compaction_failed" for e in sse_spy)

    # Alerted once per session, not once per LLM call.
    assert run_compact(agent, monkeypatch) is None
    assert len(get_compaction_events_meta(agent.name, limit=10)) == 1


# ── context_overflow reason: bypass + LLM compactor + fallback ──


class FakeCompactorLLM:
    """Stands in for agent._llm: returns per-call JSON chunk summaries."""

    def __init__(self, garbage=False):
        self._stream_listeners = {"chat-ui-listener"}
        self._tool_stream_listeners = {"tool-listener"}
        self.garbage = garbage
        self.prompts: list[str] = []
        self.listeners_during_call: list[set] = []

    async def generate(self, messages, request_params=None, tools=None):
        self.listeners_during_call.append(set(self._stream_listeners))
        self.prompts.append(messages[0].content[0].text)
        n = len(self.prompts)
        if self.garbage:
            text = "I am not JSON at all, sorry."
        else:
            text = json.dumps(
                {
                    "goal": [f"research topic X (part {n})"],
                    "constraints": [],
                    "architecture_facts": [],
                    "decisions": [f"decision-from-part-{n}"],
                    "tool_findings": [f"finding-from-part-{n}"],
                    "errors": [],
                    "file_references": [f"/llm/seen/part-{n}.py"],
                    "state": [f"state-{n}"],
                    "next_actions": ["continue"],
                }
            )
        return text_msg("assistant", text)


def test_overflow_reason_bypasses_threshold_and_memo(reg_db, monkeypatch):
    msgs = build_history()
    # current=1000 is far below any threshold — auto would be a no-op.
    agent = FakeAgent(msgs, usage=FakeUsage(current=1000, window=120000))
    assert run_compact(agent, monkeypatch) is None  # proves below threshold

    stats = run_compact(agent, monkeypatch, reason="context_overflow")
    assert stats is not None
    conn = sqlite3.connect(reg_db)
    trigger = conn.execute(
        "SELECT trigger FROM context_compaction_events WHERE id = ?",
        (stats["event_id"],),
    ).fetchone()[0]
    conn.close()
    assert trigger == "context_overflow"


def test_overflow_respects_disabled_setting(reg_db, monkeypatch):
    agent = FakeAgent(build_history(), usage=FakeUsage(current=1000))
    cfg = _trigger_cfg(enabled=False)
    assert run_compact(agent, monkeypatch, cfg=cfg, reason="context_overflow") is None


def test_overflow_uses_llm_compactor_and_restores_listeners(reg_db, monkeypatch):
    msgs = build_history()
    agent = FakeAgent(msgs, usage=FakeUsage(current=1000, window=120000))
    llm = FakeCompactorLLM()
    agent._llm = llm

    stats = run_compact(agent, monkeypatch, reason="context_overflow")
    assert stats is not None
    assert len(llm.prompts) >= 1

    # Stream listeners were detached during the side-channel call and
    # restored afterwards (compactor tokens must not leak into chat).
    assert all(seen == set() for seen in llm.listeners_during_call)
    assert llm._stream_listeners == {"chat-ui-listener"}
    assert llm._tool_stream_listeners == {"tool-listener"}

    conn = sqlite3.connect(reg_db)
    plan_json, summary = conn.execute(
        "SELECT plan_json, summary_message FROM context_compaction_events WHERE id = ?",
        (stats["event_id"],),
    ).fetchone()
    conn.close()
    plan = json.loads(plan_json)
    assert plan["compactor"] == "llm"
    assert "decision-from-part-1" in summary
    # The applied working context carries the LLM summary.
    texts = [(m.content[0].text if m.content else "") for m in agent.message_history]
    assert any(t.startswith(SUMMARY_MARKER) and "decision-from-part-1" in t for t in texts)


def test_overflow_llm_garbage_fails_loud_no_rule_fallback(reg_db, monkeypatch):
    # Compaction is LLM-only: unparseable compactor output is a visible
    # failed event, NOT a silent downgrade to a rule-based summary. The
    # overflowing call then fails (the hook returns False → error
    # propagates), which is the honest outcome.
    msgs = build_history()
    agent = FakeAgent(msgs, usage=FakeUsage(current=1000, window=120000))
    agent._llm = FakeCompactorLLM(garbage=True)
    before_json = to_json(agent.message_history)

    stats = run_compact(agent, monkeypatch, reason="context_overflow")
    assert stats is None
    assert to_json(agent.message_history) == before_json  # untouched
    events = get_compaction_events_meta(agent.name, limit=10)
    assert len(events) == 1
    assert events[0]["status"] == "failed"
    assert "LLM compactor failed" in events[0]["error_message"]


def test_overflow_without_llm_fails_loud(reg_db, monkeypatch):
    # No LLM and no compactor_model → no compactor → fail loud (no
    # rule-based rescue exists anymore).
    agent = FakeAgent(build_history(), usage=FakeUsage(current=1000, window=120000))
    agent._llm = None
    stats = run_compact(agent, monkeypatch, reason="context_overflow")
    assert stats is None
    events = get_compaction_events_meta(agent.name, limit=10)
    assert events and events[0]["status"] == "failed"


def test_threshold_llm_failure_fails_loud_no_rule_fallback(reg_db, monkeypatch):
    """Auto-threshold compaction is LLM-only: a compactor failure must be
    a visible failed event, NOT a silent quality downgrade to rule-based."""
    msgs = build_history()
    agent = FakeAgent(msgs, usage=FakeUsage(current=110000, window=120000))
    agent._llm = FakeCompactorLLM(garbage=True)
    before_json = to_json(agent.message_history)

    assert run_compact(agent, monkeypatch) is None
    assert to_json(agent.message_history) == before_json  # untouched
    events = get_compaction_events_meta(agent.name, limit=10)
    assert len(events) == 1
    assert events[0]["status"] == "failed"
    assert "LLM compactor failed" in events[0]["error_message"]


def test_compactor_model_uses_dedicated_llm_not_origin(reg_db, monkeypatch):
    """compactor_model set → the dedicated compactor agent does the work;
    the origin agent's own LLM is never called (spec: separate compactor
    component/agent)."""
    msgs = build_history()
    agent = FakeAgent(msgs, usage=FakeUsage(current=110000, window=120000))
    origin_llm = FakeCompactorLLM(garbage=True)  # would fail if used
    agent._llm = origin_llm
    dedicated = FakeCompactorLLM()
    monkeypatch.setitem(cc._compactor_llm_cache, "big-window-model", dedicated)

    cfg = _trigger_cfg(compactor_model="big-window-model")
    stats = run_compact(agent, monkeypatch, cfg=cfg)
    assert stats is not None
    assert len(dedicated.prompts) >= 1
    assert origin_llm.prompts == []

    conn = sqlite3.connect(reg_db)
    plan_json = conn.execute(
        "SELECT plan_json FROM context_compaction_events WHERE id = ?",
        (stats["event_id"],),
    ).fetchone()[0]
    conn.close()
    plan = json.loads(plan_json)
    assert plan["compactor"] == "llm"
    assert plan["compactor_model"] == "big-window-model"


def test_summary_keeps_file_references_from_both_sources(reg_db, monkeypatch):
    """File references survive compaction: paths the LLM lists AND paths
    deterministically scanned from tool-call arguments."""
    msgs = [
        text_msg("user", "SYSTEM TEMPLATE", is_template=True),
        text_msg("user", "Refactor the voice module"),
    ]
    a, u = tool_pair(
        "call_read", "Read", "file contents here " + "x" * 400,
        arguments={"file_path": "/repo/backend/services/webrtc_voice.py"},
    )
    msgs.extend([a, u])
    for i in range(6):
        a, u = tool_pair(f"call_{i}", "Bash", f"result {i}: " + "x" * 400)
        msgs.extend([a, u])
    msgs.append(text_msg("user", "Now summarize"))
    msgs.append(text_msg("assistant", "Working on it"))

    agent = FakeAgent(msgs, usage=FakeUsage(current=110000, window=120000))
    stats = run_compact(agent, monkeypatch)
    assert stats is not None
    summary = next(
        m.content[0].text for m in agent.message_history
        if m.content and m.content[0].text.startswith(SUMMARY_MARKER)
    )
    files_section = summary.split("File references:")[1].split("Recent state:")[0]
    assert "/repo/backend/services/webrtc_voice.py" in files_section  # deterministic scan
    assert "/llm/seen/part-1.py" in files_section  # LLM-extracted


# ── Pair-safe chunking + map-reduce merge ──


def _chunk_pairing_ok(messages, chunk):
    calls, results = set(), set()
    for i in chunk:
        calls |= set(getattr(messages[i], "tool_calls", None) or {})
        results |= set(getattr(messages[i], "tool_results", None) or {})
    return calls == results


def test_pair_safe_chunks_never_split_tool_pairs():
    msgs = build_history(n_pairs=12, filler_chars=400)
    cfg = cfg_with(keep_recent_messages=4)
    zones = cc._plan_zones(msgs, cfg)
    assert zones is not None
    _h, _t, _keep, middle = zones

    chunks = cc._pair_safe_chunks(msgs, middle, chunk_tokens=120)
    assert len(chunks) >= 2
    assert [i for c in chunks for i in c] == middle  # order + completeness
    for chunk in chunks:
        assert _chunk_pairing_ok(msgs, chunk)


def test_chunked_map_reduce_merges_all_parts(reg_db, monkeypatch):
    msgs = build_history(n_pairs=12, filler_chars=400)
    # Small window so the configured input ratio yields tiny chunks and
    # forces the map-reduce path.
    agent = FakeAgent(msgs, usage=FakeUsage(current=1000, window=1500))
    llm = FakeCompactorLLM()
    agent._llm = llm
    monkeypatch.setattr(cc, "_COMPACTOR_MIN_CHUNK_TOKENS", 120)

    cfg = _trigger_cfg(compactor_input_ratio=0.1)
    plan = asyncio.run(
        cc.plan_compaction_llm(agent, msgs, cfg, raw_snapshot_id=7, agent_name=agent.name)
    )
    assert plan is not None
    assert plan["compactor"] == "llm"
    assert plan["compactor_chunks"] == len(llm.prompts) >= 2

    # Items from the FIRST and LAST chunks both survive the merge.
    summary = plan["summary_message"]
    assert "decision-from-part-1" in summary
    assert f"decision-from-part-{len(llm.prompts)}" in summary

    # The merged plan passes the same backend validation.
    working = build_working_context(msgs, plan)
    assert validate_working_context(msgs, working, cfg) == []


# ── on_context_overflow hook wiring ──


def test_on_context_overflow_hook_compacts_and_reports_true(reg_db, monkeypatch):
    from types import SimpleNamespace

    msgs = build_history()
    agent = FakeAgent(msgs, usage=FakeUsage(current=1000, window=120000))
    monkeypatch.setattr(cc, "get_compaction_config", lambda: _trigger_cfg())
    hooks = cc.create_context_compaction_hooks()
    assert hooks.on_context_overflow is not None

    handled = asyncio.run(
        hooks.on_context_overflow(
            SimpleNamespace(_agent=agent),
            Exception("This model's maximum context length is 200000 tokens"),
        )
    )
    assert handled is True
    texts = [(m.content[0].text if m.content else "") for m in agent.message_history]
    assert any(t.startswith(SUMMARY_MARKER) for t in texts)


def test_on_context_overflow_hook_returns_false_when_nothing_to_compact(reg_db, monkeypatch):
    from types import SimpleNamespace

    # History too small to compact → hook must say False so the error
    # propagates instead of a futile retry.
    msgs = build_history(n_pairs=0)
    agent = FakeAgent(msgs, usage=FakeUsage(current=1000, window=120000))
    monkeypatch.setattr(cc, "get_compaction_config", lambda: _trigger_cfg())
    hooks = cc.create_context_compaction_hooks()

    handled = asyncio.run(
        hooks.on_context_overflow(
            SimpleNamespace(_agent=agent), Exception("prompt is too long")
        )
    )
    assert handled is False
