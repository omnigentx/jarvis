"""Per-agent memory ownership — ONE mechanism. The owner of every memory op is
the per-call ``caller_agent`` fast-agent stamps from the calling agent's own name
(mcp_aggregator._execute_on_server). It works identically for in-process agents
(sharing the pooled memory_server subprocess) and spawned agents (dedicated
subprocess) because every agent — including spawned ones — is named with its real
identity. No env/static fallback: a missing identity FAILS rather than mis-scoping.
"""
from types import SimpleNamespace

from tools import memory_server as ms


def _ctx(meta):
    return SimpleNamespace(request_context=SimpleNamespace(meta=meta))


# ── owner resolution: caller_agent is the single source ──────────────────────

def test_caller_agent_resolves_owner():
    # Same path for in-process and spawned: the agent's own name arrives as the
    # trusted per-call caller_agent and scopes the op to that agent's silo.
    assert ms._owner(_ctx({"caller_agent": "IoTAgent"})) == "IoTAgent"
    assert ms._owner(_ctx({"caller_agent": "MemTestCarol"})) == "MemTestCarol"


def test_caller_from_pydantic_style_meta():
    # transport may hand meta as a pydantic model carrying extras (not a dict)
    assert ms._caller_from_ctx(_ctx(SimpleNamespace(caller_agent="MusicAgent"))) == "MusicAgent"
    assert ms._caller_from_ctx(_ctx(SimpleNamespace(model_extra={"caller_agent": "FinanceAgent"}))) == "FinanceAgent"


def test_no_identity_returns_empty_no_fallback():
    # No ctx / no caller / blank → "" so the tool FAILS the op. There is NO
    # fallback to any env or static name (which would mis-scope the write).
    assert ms._owner(None) == ""
    assert ms._owner(_ctx(None)) == ""
    assert ms._owner(_ctx({})) == ""
    assert ms._owner(_ctx({"caller_agent": "  "})) == ""


# ── E2E: caller_agent _meta actually flows through FastMCP to the RPC owner ───

async def test_caller_agent_meta_flows_through_fastmcp(monkeypatch):
    """The real path: a tool call carrying _meta.caller_agent → FastMCP
    request_context.meta → _owner() → the agent_name sent to the backend RPC."""
    from mcp.shared.memory import create_connected_server_and_client_session as connect

    captured = {}

    def fake_rpc(method, params):
        captured["method"] = method
        captured["agent_name"] = params.get("agent_name")
        return {"status": "auto_approved", "candidate_id": "x"}

    monkeypatch.setattr(ms, "rpc_call", fake_rpc)
    async with connect(ms.mcp._mcp_server) as client:
        await client.call_tool("memory_remember", {"content": "the vacuum is a Roborock"},
                               meta={"caller_agent": "IoTAgent"})
    assert captured["method"] == "memory.remember"
    assert captured["agent_name"] == "IoTAgent"


# ── fast-agent stamps caller_agent (the producer side) ───────────────────────

def _mock_aggregator(name, captured):
    from fast_agent.mcp.mcp_aggregator import MCPAggregator

    agg = MCPAggregator.__new__(MCPAggregator)   # bypass heavy __init__
    agg.agent_name = name
    agg.connection_persistence = True

    class _Sess:
        async def call_tool(self, **kw):
            captured.update(kw)
            return "ok"

    class _Conn:
        session = _Sess()

    class _Mgr:
        async def get_server(self, n, client_session_factory=None):
            return _Conn()

    async def _noop_record(*a, **k):
        return None

    agg._require_connection_manager = lambda: _Mgr()
    agg._create_session_factory = lambda n: None
    agg._maybe_mark_rejected_session_cookie_from_tool_result = lambda **kw: None
    agg._record_server_call = _noop_record
    return agg


async def test_aggregator_stamps_caller_agent_on_tool_calls():
    """fast-agent stamps caller_agent=<its agent name> onto every tool call's
    _meta. Same code path for an in-process agent and a spawned-process agent —
    a spawned agent is now named with its real identity, so this stamps it too."""
    captured = {}
    agg = _mock_aggregator("IoTAgent", captured)
    await agg._execute_on_server("memory_server", "tool", "memory_remember", "call_tool",
                                 {"name": "memory_remember", "arguments": {"content": "x"}})
    assert captured["meta"]["caller_agent"] == "IoTAgent"


async def test_stamp_then_read_full_loop():
    """End-to-end: aggregator stamp (producer) feeds memory_server _owner
    (consumer) — the exact stamped meta resolves back to the same owner."""
    captured = {}
    agg = _mock_aggregator("MemTestCarol", captured)   # a spawned-style real name
    await agg._execute_on_server("memory_server", "tool", "memory_search", "call_tool",
                                 {"name": "memory_search", "arguments": {"query": "q"}})
    ctx = SimpleNamespace(request_context=SimpleNamespace(meta=captured["meta"]))
    assert ms._owner(ctx) == "MemTestCarol"


# ── Cascade: deleting an agent purges ONLY its own memory (no DB garbage) ─────

def test_purge_agent_memory_deletes_only_that_agent():
    """When an agent is deleted its silo is purged; other agents are untouched."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from core.database import Base, MemoryCandidate, MemoryRecord
    from services.memory.memory_service import purge_agent_memory

    engine = create_engine("sqlite:///:memory:",
                           connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    for owner in ("IoTAgent", "Jarvis"):
        db.add(MemoryRecord(id=f"m-{owner}", owner_agent_name=owner, memory_type="semantic",
                            subject_scope="user", content="x", normalized_content="x",
                            status="active", authority="user_confirmed", confidence=0.9,
                            current_version=1, created_at=1.0, updated_at=1.0))
        db.add(MemoryCandidate(id=f"c-{owner}", owner_agent_name=owner, candidate_type="extracted",
                               payload_json="{}", source_refs_json="[]", status="pending",
                               confidence=0.5, created_at=1.0))
    db.commit()

    counts = purge_agent_memory(db, "IoTAgent")

    assert counts["records"] == 1 and counts["candidates"] == 1
    for model in (MemoryRecord, MemoryCandidate):
        assert db.query(model).filter(model.owner_agent_name == "IoTAgent").count() == 0
        assert db.query(model).filter(model.owner_agent_name == "Jarvis").count() == 1  # untouched
    db.close()
