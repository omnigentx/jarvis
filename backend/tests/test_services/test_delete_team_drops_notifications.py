"""Regression test for ``DELETE /api/teams/{team_name}`` cascading
into the ``notifications`` table.

The 2026-05-14 toolset-self-audit incident: a user deleted the
previous day's run via the API, started a fresh team with the same
``team_name``, and never got a completion notification for the new
team. Why: the delete route cleaned spawn_registry, team_sessions,
workspace, messages, and activities — but NOT notifications. The
stale notification carried ``team_name: "toolset-self-audit"`` in
its metadata, and the team-completion bridge's dedupe (pre-fix:
keyed on team_name) matched it and skipped emitting a new one.

This test pins the cleanup contract on the delete route directly so
the dedupe fix and the cleanup fix can both stay healthy
independently.
"""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def test_delete_team_cleans_notifications_for_that_team_name(tmp_path, monkeypatch):
    """DELETE /api/teams/{name} must drop any ``team_completion``
    notification whose metadata carries that ``team_name`` — without
    this, the next run that reuses the team name has its notification
    silently swallowed by the dedupe."""
    # Build an isolated SQLite engine bound to a temp file. We do NOT
    # reload core.database — that would corrupt the model registry for
    # every test that runs afterwards in the same pytest session.
    # Instead we monkeypatch get_db_session to hand the route a session
    # against our temp engine; the model classes stay intact.
    import core.database as db_mod
    db_file = tmp_path / "jarvis.db"
    engine = create_engine(f"sqlite:///{db_file}", future=True)
    db_mod.Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)
    monkeypatch.setattr(db_mod, "get_db_session", lambda: SessionLocal())

    # Seed two team_completion notifications: one for the team we're
    # about to delete, one for an unrelated team that must survive.
    sess = SessionLocal()
    try:
        stale = db_mod.NotificationModel(
            type="agent_result",
            title="Team toolset-self-audit completed",
            preview="...",
            content="No detailed result from orchestrator.",
            content_type="markdown",
            is_read=0,
            created_at=time.time(),
            metadata_json=json.dumps({
                "agent": "Morgan [PM]",
                "team_name": "toolset-self-audit",
                "session_id": "oldsess",
                "source": "team_completion",
            }),
        )
        keep = db_mod.NotificationModel(
            type="agent_result",
            title="Team unrelated-team completed",
            preview="...",
            content="ok",
            content_type="markdown",
            is_read=0,
            created_at=time.time(),
            metadata_json=json.dumps({
                "agent": "Other [PM]",
                "team_name": "unrelated-team",
                "session_id": "othersess",
                "source": "team_completion",
            }),
        )
        sess.add_all([stale, keep])
        sess.commit()
        sess.refresh(stale)
        sess.refresh(keep)
        stale_id, keep_id = stale.id, keep.id
    finally:
        sess.close()

    # Stub registry / team_session lookups the route makes.
    fake_registry = MagicMock()
    fake_registry.get_all.return_value = {
        "run-1": {
            "team_name": "toolset-self-audit",
            "agent_name": "Morgan [PM]",
            "session_id": "oldsess",
        }
    }
    fake_registry.delete_by_team.return_value = (1, None)
    fake_registry.delete_by_name.return_value = 0

    import routes.agents as agents_route
    app = FastAPI()
    app.include_router(agents_route.router)
    app.dependency_overrides[agents_route.verify_api_key] = lambda: True

    with patch.object(agents_route, "_trigger_reload", lambda: None), \
            patch("services.shared_state.registry_db", fake_registry, create=True), \
            patch.object(agents_route, "activity_stream_manager", MagicMock()), \
            patch("fast_agent.spawn.team_spawner.list_team_sessions",
                  return_value=[]), \
            patch("fast_agent.spawn.team_spawner.delete_team_session",
                  return_value=True), \
            patch("fast_agent.spawn.team_spawner.delete_team_sessions_by_team_name",
                  return_value=0):
        client = TestClient(app)
        r = client.delete("/api/agents/teams/toolset-self-audit")

    assert r.status_code == 200, r.text
    payload = r.json()
    assert "1 notification(s)" in payload["cleaned"], (
        f"Notifications were not cleaned. cleanup_log={payload['cleaned']!r}"
    )

    # Confirm in the DB: stale gone, unrelated survives.
    sess = SessionLocal()
    try:
        remaining = {n.id for n in sess.query(db_mod.NotificationModel).all()}
    finally:
        sess.close()
    assert stale_id not in remaining, "stale notification not deleted"
    assert keep_id in remaining, "unrelated team's notification was wrongly dropped"
