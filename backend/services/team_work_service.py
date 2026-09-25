"""Durable conversation-to-team bindings and revisioned user directives.

The database records a directive before it is queued. Delivery is retried only
on an explicit request or backend startup, never by a polling loop. Replays
inspect the session inbox under a file lock so a crash after append cannot
queue the same revision twice.
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from core.database import engine
from services.activity_stream import activity_stream_manager

logger = logging.getLogger(__name__)


class TeamWorkError(ValueError):
    """A rejected team control operation with an HTTP-compatible status."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _connect() -> sqlite3.Connection:
    path = engine.url.database
    if not path:
        raise TeamWorkError("Team work database is not configured", 503)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


@contextmanager
def _database() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _as_dict(row: sqlite3.Row) -> dict:
    result = dict(row)
    if "source_refs_json" in result:
        result["source_refs"] = json.loads(result.pop("source_refs_json"))
    return result


def bind_team(session_id: str, conversation_id: str) -> dict:
    """Bind a spawned team to the chat/voice turn that created it."""
    if not session_id or not conversation_id:
        raise TeamWorkError("session_id and conversation_id are required")
    from fast_agent.spawn.team_spawner import get_team_session

    session = get_team_session(session_id)
    if session is None:
        raise TeamWorkError(f"Team session {session_id!r} not found", 404)
    with _database() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT conversation_id FROM team_work_bindings WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if existing and existing["conversation_id"] != conversation_id:
            raise TeamWorkError("Team is already bound to another conversation", 409)
        if not existing:
            conn.execute(
                "INSERT INTO team_work_bindings "
                "(session_id, conversation_id, team_name, project_brief, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, conversation_id, session.team_name,
                 session.project_brief, time.time()),
            )
    return {"session_id": session_id, "conversation_id": conversation_id,
            "team_name": session.team_name}


def list_teams(conversation_id: str) -> list[dict]:
    """Return a compact, conversation-scoped team list for Jarvis and UI."""
    if not conversation_id:
        raise TeamWorkError("conversation_id is required")
    with _database() as conn:
        rows = conn.execute(
            "SELECT b.session_id, b.team_name, b.project_brief, b.created_at, "
            "COALESCE(MAX(r.revision), 0) AS revision "
            "FROM team_work_bindings b LEFT JOIN team_requirement_revisions r "
            "ON r.session_id=b.session_id WHERE b.conversation_id=? "
            "GROUP BY b.session_id ORDER BY b.created_at DESC",
            (conversation_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def find_teams(query: str = "", limit: int = 20) -> list[dict]:
    """Find team sessions across the single user's conversations on demand."""
    if limit < 1 or limit > 50:
        raise TeamWorkError("limit must be between 1 and 50")
    pattern = f"%{query.strip().replace('%', '')[:100]}%"
    with _database() as conn:
        rows = conn.execute(
            "SELECT b.session_id, b.conversation_id, b.team_name, "
            "substr(b.project_brief, 1, 240) AS project_brief, b.created_at, "
            "COALESCE(MAX(r.revision), 0) AS revision "
            "FROM team_work_bindings b LEFT JOIN team_requirement_revisions r "
            "ON r.session_id=b.session_id "
            "WHERE b.team_name LIKE ? OR b.project_brief LIKE ? "
            "GROUP BY b.session_id ORDER BY b.created_at DESC LIMIT ?",
            (pattern, pattern, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def get_binding(session_id: str) -> dict:
    """Resolve the authoritative conversation for a registered team."""
    with _database() as conn:
        row = conn.execute(
            "SELECT * FROM team_work_bindings WHERE session_id=?", (session_id,)
        ).fetchone()
    if row is None:
        raise TeamWorkError("Team is not bound to a conversation", 404)
    return dict(row)


def get_team_revisions(
    session_id: str, conversation_id: str, *, limit: int = 20,
    before_revision: int | None = None,
) -> list[dict]:
    """Read a bounded page of revisions, excluding other conversations."""
    _require_binding(session_id, conversation_id)
    if limit < 1 or limit > 100:
        raise TeamWorkError("limit must be between 1 and 100")
    if before_revision is not None and before_revision < 1:
        raise TeamWorkError("before_revision must be positive")
    with _database() as conn:
        rows = conn.execute(
            "SELECT * FROM team_requirement_revisions WHERE session_id=? "
            "AND (? IS NULL OR revision < ?) "
            "ORDER BY revision DESC LIMIT ?",
            (session_id, before_revision, before_revision, limit),
        ).fetchall()
    return [_as_dict(row) for row in reversed(rows)]


def _require_binding(session_id: str, conversation_id: str) -> None:
    with _database() as conn:
        row = conn.execute(
            "SELECT conversation_id FROM team_work_bindings WHERE session_id=?",
            (session_id,),
        ).fetchone()
    if row is None:
        raise TeamWorkError("Team is not bound to a conversation", 404)
    if row["conversation_id"] != conversation_id:
        raise TeamWorkError("Team belongs to another conversation", 403)


def create_revision(
    session_id: str,
    conversation_id: str,
    change_text: str,
    *,
    idempotency_key: str | None = None,
    expected_revision: int | None = None,
    source_refs: list[str] | None = None,
) -> dict:
    """Atomically allocate the next revision and persist a pending directive."""
    change_text = change_text.strip()
    if not change_text or len(change_text) > 12_000:
        raise TeamWorkError("Change must contain 1–12000 characters")
    if idempotency_key and len(idempotency_key) > 100:
        raise TeamWorkError("Idempotency key is too long")
    refs = source_refs or []
    if len(refs) > 20 or any(not isinstance(ref, str) or len(ref) > 500 for ref in refs):
        raise TeamWorkError("At most 20 source references of 500 characters are allowed")

    with _database() as conn:
        conn.execute("BEGIN IMMEDIATE")
        binding = conn.execute(
            "SELECT conversation_id FROM team_work_bindings WHERE session_id=?",
            (session_id,),
        ).fetchone()
        if binding is None:
            raise TeamWorkError("Team is not bound to a conversation", 404)
        if binding["conversation_id"] != conversation_id:
            raise TeamWorkError("Team belongs to another conversation", 403)
        if idempotency_key:
            existing = conn.execute(
                "SELECT * FROM team_requirement_revisions "
                "WHERE session_id=? AND idempotency_key=?",
                (session_id, idempotency_key),
            ).fetchone()
            if existing:
                if existing["change_text"] != change_text or json.loads(
                    existing["source_refs_json"]
                ) != refs:
                    raise TeamWorkError("Idempotency key was used for a different change", 409)
                return _as_dict(existing)

        current = conn.execute(
            "SELECT COALESCE(MAX(revision), 0) FROM team_requirement_revisions "
            "WHERE session_id=?", (session_id,),
        ).fetchone()[0]
        if expected_revision is not None and expected_revision != current:
            raise TeamWorkError(
                f"Expected revision {expected_revision}; current revision is {current}", 409
            )
        change_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO team_requirement_revisions "
            "(id, session_id, revision, idempotency_key, change_text, "
            "source_refs_json, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
            (change_id, session_id, current + 1, idempotency_key,
             change_text, json.dumps(refs), time.time()),
        )
        row = conn.execute(
            "SELECT * FROM team_requirement_revisions WHERE id=?", (change_id,)
        ).fetchone()
        return _as_dict(row)


def _queue_once(change: dict) -> tuple[str, str]:
    """Append once to the PM's session inbox; safe to replay after a crash."""
    from fast_agent.spawn.message_bus import MessageBus
    from fast_agent.spawn.team_spawner import get_team_session
    import services.shared_state as state

    session = get_team_session(change["session_id"])
    if session is None:
        raise TeamWorkError("Team session was removed before delivery", 409)
    orchestrator_role = session.template.get("orchestrator")
    pm = next(
        ((name, info) for name, info in session.agents.items()
         if info.get("role") == orchestrator_role), None,
    )
    if pm is None:
        raise TeamWorkError("Team has no orchestrator", 409)
    agent_name, info = pm
    run_id = info.get("run_id")
    record = state.registry_db.get_record(run_id) if state.registry_db and run_id else None
    env_vars = (record or {}).get("original_config") or {}
    messages_dir = (env_vars.get("env_vars") or {}).get("TEAM_MESSAGES_DIR")
    if not messages_dir:
        raise TeamWorkError("Orchestrator inbox is unavailable", 503)

    bus = MessageBus(messages_dir)
    lock_path = Path(messages_dir) / ".team_work.lock"
    content = (
        f"[USER REQUIREMENT CHANGE]\nTeam revision: {change['revision']}\n"
        f"Change ID: {change['id']}\n{change['change_text']}"
    )
    if change["source_refs"]:
        content += "\nReferences: " + ", ".join(change["source_refs"])
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            previous = next(
                (m for m in bus.read_inbox(agent_name)
                 if m.context.get("change_id") == change["id"]), None,
            )
            if previous:
                return previous.message_id, agent_name
            msg = bus.send(
                from_name="Jarvis", to_name=agent_name, content=content,
                message_type="directive", priority="high",
                context={"session_id": change["session_id"],
                         "change_id": change["id"], "revision": change["revision"]},
            )
            return msg.message_id, agent_name
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


async def deliver_revision(change_id: str) -> dict:
    """Queue a pending change, wake PM, and publish a small activity event."""
    with _database() as conn:
        row = conn.execute(
            "SELECT * FROM team_requirement_revisions WHERE id=?", (change_id,)
        ).fetchone()
    if row is None:
        raise TeamWorkError("Revision not found", 404)
    change = _as_dict(row)
    if change["status"] == "delivered":
        return change

    message_id, agent_name = await asyncio.to_thread(_queue_once, change)
    with _database() as conn:
        conn.execute(
            "UPDATE team_requirement_revisions SET status='delivered', "
            "message_id=?, delivered_at=? WHERE id=?",
            (message_id, time.time(), change_id),
        )
    try:
        from fast_agent.spawn.servers._team_helpers import auto_wake_if_idle
        await asyncio.to_thread(auto_wake_if_idle, agent_name)
    except Exception:
        logger.warning("[TEAM-WORK] Could not wake orchestrator %s", agent_name,
                       exc_info=True)

    activity_stream_manager.broadcast({
        "event_type": "team_requirement_change",
        "agent_name": agent_name,
        "session_id": change["session_id"],
        "message": f"User requirement revision {change['revision']} queued",
        "timestamp": time.time(),
        "data": {"change_id": change_id, "revision": change["revision"]},
    })
    change.update(status="delivered", message_id=message_id)
    return change


async def retry_pending(session_id: str, conversation_id: str) -> list[dict]:
    """Explicitly replay pending outbox rows; no background polling."""
    _require_binding(session_id, conversation_id)
    with _database() as conn:
        ids = [row[0] for row in conn.execute(
            "SELECT id FROM team_requirement_revisions "
            "WHERE session_id=? AND status='pending' ORDER BY revision",
            (session_id,),
        )]
    results = []
    for change_id in ids:
        results.append(await deliver_revision(change_id))
    return results


async def replay_pending_on_startup() -> None:
    """Replay durable outbox rows once after the agent runtime is ready."""
    with _database() as conn:
        ids = [row[0] for row in conn.execute(
            "SELECT id FROM team_requirement_revisions WHERE status='pending' "
            "ORDER BY created_at, revision"
        )]
    for change_id in ids:
        try:
            await deliver_revision(change_id)
        except Exception:
            logger.warning("[TEAM-WORK] Startup replay pending for %s", change_id,
                           exc_info=True)
