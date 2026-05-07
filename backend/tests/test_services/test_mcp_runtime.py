"""Unit tests for services.mcp_runtime (audit context manager + env resolver)."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from core.database import Base, McpEventLogModel, SessionLocal, engine
from services import mcp_runtime
from services.activity_stream import activity_stream_manager


@pytest.fixture(autouse=True)
def _ensure_tables():
    Base.metadata.create_all(bind=engine)
    yield
    with SessionLocal() as db:
        db.query(McpEventLogModel).delete()
        db.commit()


# ── resolve_env ───────────────────────────────────────────────────────


def test_resolve_env_substitutes_placeholders(monkeypatch):
    monkeypatch.setenv("FOO", "bar")
    monkeypatch.setenv("BAZ", "qux")
    out = mcp_runtime.resolve_env({"A": "${FOO}", "B": "prefix-${BAZ}-suffix"})
    assert out == {"A": "bar", "B": "prefix-qux-suffix"}


def test_resolve_env_missing_var_yields_empty_string(monkeypatch):
    monkeypatch.delenv("MISSING_KEY_FOR_TEST", raising=False)
    assert mcp_runtime.resolve_env({"A": "${MISSING_KEY_FOR_TEST}"}) == {"A": ""}


def test_resolve_env_handles_none_values():
    assert mcp_runtime.resolve_env({"A": None}) == {"A": ""}


# ── audit ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_persists_ok_outcome():
    captured: list[dict] = []
    with patch.object(activity_stream_manager, "broadcast", side_effect=lambda e: captured.append(e)):
        async with mcp_runtime.audit("create", server="srv-x", actor="user") as a:
            a.set(extra="value")

    with SessionLocal() as db:
        rows = db.query(McpEventLogModel).filter_by(server_name="srv-x").all()
    assert len(rows) == 1
    assert rows[0].action == "create"
    assert rows[0].outcome == "ok"
    assert rows[0].duration_ms is not None
    detail = json.loads(rows[0].detail_json)
    assert detail == {"extra": "value"}

    assert len(captured) == 1
    ev = captured[0]
    assert ev["type"] == "mcp"
    assert ev["action"] == "create"
    assert ev["outcome"] == "ok"


@pytest.mark.asyncio
async def test_audit_records_failure_and_re_raises():
    with patch.object(activity_stream_manager, "broadcast"):
        with pytest.raises(ValueError, match="boom"):
            async with mcp_runtime.audit("update", server="srv-y") as a:
                a.set(step=1)
                raise ValueError("boom")

    with SessionLocal() as db:
        rows = db.query(McpEventLogModel).filter_by(server_name="srv-y").all()
    assert len(rows) == 1
    assert rows[0].outcome == "fail"
    detail = json.loads(rows[0].detail_json)
    assert detail["step"] == 1
    assert "boom" in detail["error"]
    assert "ValueError" in detail["error"]


@pytest.mark.asyncio
async def test_audit_broadcast_failure_does_not_propagate():
    """If activity_stream broadcast itself raises, audit must still complete."""
    with patch.object(activity_stream_manager, "broadcast", side_effect=RuntimeError("stream dead")):
        async with mcp_runtime.audit("attach", server="srv-z", agent="A1"):
            pass
    # No exception, and DB row still written.
    with SessionLocal() as db:
        assert db.query(McpEventLogModel).filter_by(server_name="srv-z").count() == 1
