"""SQLite-backed store for dynamic agent definitions.

Single source of truth for agents that are NOT defined in code (agent.py
decorators). Replaces the old file-based agent card layer
(`.fast-agent/agent_cards/*.md`).

Tables
------
agent_definitions       — one row per dynamic agent
agent_definitions_meta  — small key/value bag; currently holds `rev`,
                          a monotonic counter the reload loop polls so
                          subprocess writers (e.g. agent_spawner MCP)
                          can signal the parent to reload from DB.

Why a separate meta table? Reload signalling needs to be cheap to read
on every poll tick. A single-row SELECT on a 2-row meta table is
cheaper than COUNT/MAX over the definitions table and stays correct
under deletes.

Concurrency
-----------
SQLite is in WAL mode (see .db-wal file). Concurrent reads + a single
writer at a time. CRUD helpers open short-lived connections; the
reload-polling loop holds its own connection. No long-lived locks.

DB path resolution follows the project pattern in
context_persistence.py — SPAWN_REGISTRY_DB env var (absolute path) for
subprocess safety; falls back to data/jarvis.db relative to CWD when
called from the parent process.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from typing import Any, Iterable

logger = logging.getLogger(__name__)


def _get_db_path() -> str | None:
    """Resolve absolute DB path; mirrors context_persistence._get_db_path."""
    db_path = os.environ.get("SPAWN_REGISTRY_DB")
    if not db_path:
        fallback = os.path.join("data", "jarvis.db")
        if os.path.exists(fallback):
            return os.path.abspath(fallback)
        return None
    return db_path


def _ensure_tables(conn: sqlite3.Connection) -> None:
    """Create tables idempotently. Safe to call on every connect."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_definitions (
            name TEXT PRIMARY KEY,
            instruction TEXT NOT NULL,
            servers TEXT NOT NULL DEFAULT '[]',
            tools TEXT NOT NULL DEFAULT '{}',
            skills TEXT NOT NULL DEFAULT '[]',
            model TEXT,
            use_history INTEGER NOT NULL DEFAULT 1,
            request_params TEXT NOT NULL DEFAULT '{}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_definitions_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """
    )
    # Seed the rev counter once. INSERT OR IGNORE keeps subsequent
    # opens cheap and avoids resetting the counter on restart.
    conn.execute(
        "INSERT OR IGNORE INTO agent_definitions_meta (key, value) VALUES ('rev', '0')"
    )
    conn.commit()


def _connect() -> sqlite3.Connection | None:
    """Open a connection with row factory + tables ensured. Returns None
    when no DB path is configured (test harness without SPAWN_REGISTRY_DB
    and no local data/jarvis.db)."""
    path = _get_db_path()
    if not path:
        return None
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    _ensure_tables(conn)
    return conn


def _bump_rev(conn: sqlite3.Connection) -> int:
    """Increment rev atomically and return the new value.

    Done inside the caller's transaction so a failed mutation does not
    leak a rev bump that would trigger an empty reload.
    """
    row = conn.execute(
        "SELECT value FROM agent_definitions_meta WHERE key = 'rev'"
    ).fetchone()
    new_rev = int(row["value"] if row else 0) + 1
    conn.execute(
        "INSERT OR REPLACE INTO agent_definitions_meta (key, value) VALUES ('rev', ?)",
        (str(new_rev),),
    )
    return new_rev


def get_rev() -> int:
    """Read the current rev counter. Returns 0 when DB / table missing."""
    conn = _connect()
    if not conn:
        return 0
    try:
        row = conn.execute(
            "SELECT value FROM agent_definitions_meta WHERE key = 'rev'"
        ).fetchone()
        return int(row["value"]) if row else 0
    finally:
        conn.close()


# ── Row <-> dict shape ────────────────────────────────────────────────


# JSON-encoded columns (stored as TEXT, exposed as Python objects).
_JSON_COLS = ("servers", "tools", "skills", "request_params")


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    out: dict[str, Any] = {k: row[k] for k in row.keys()}
    for col in _JSON_COLS:
        out[col] = json.loads(out[col]) if out.get(col) else _json_default(col)
    out["use_history"] = bool(out["use_history"])
    return out


def _json_default(col: str) -> Any:
    return [] if col in ("servers", "skills") else {}


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("agent name must be a non-empty string")


def _encode_json_fields(values: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with JSON columns serialized to TEXT."""
    out = dict(values)
    for col in _JSON_COLS:
        if col in out and not isinstance(out[col], str):
            out[col] = json.dumps(out[col] or _json_default(col), ensure_ascii=False)
    return out


# ── CRUD ──────────────────────────────────────────────────────────────


def create_definition(
    *,
    name: str,
    instruction: str,
    servers: Iterable[str] | None = None,
    tools: dict[str, list[str]] | None = None,
    skills: Iterable[str] | None = None,
    model: str | None = None,
    use_history: bool = True,
    request_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Insert a new agent definition. Raises if `name` already exists."""
    _validate_name(name)
    if not instruction or not instruction.strip():
        raise ValueError("instruction must not be empty")

    conn = _connect()
    if not conn:
        raise RuntimeError("agent_definitions: DB not configured")
    try:
        existing = conn.execute(
            "SELECT 1 FROM agent_definitions WHERE name = ?", (name,)
        ).fetchone()
        if existing:
            raise ValueError(f"agent '{name}' already exists")

        now = time.time()
        encoded = _encode_json_fields(
            {
                "servers": list(servers or []),
                "tools": dict(tools or {}),
                "skills": list(skills or []),
                "request_params": dict(request_params or {}),
            }
        )
        conn.execute(
            """
            INSERT INTO agent_definitions
              (name, instruction, servers, tools, skills, model,
               use_history, request_params, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                instruction,
                encoded["servers"],
                encoded["tools"],
                encoded["skills"],
                model,
                1 if use_history else 0,
                encoded["request_params"],
                now,
                now,
            ),
        )
        _bump_rev(conn)
        conn.commit()
        logger.info("[AGENT_DEFS] created '%s'", name)
        return get_definition(name)  # re-fetch through canonical decoder
    finally:
        conn.close()


def get_definition(name: str) -> dict[str, Any] | None:
    """Return one definition as a dict, or None if missing."""
    _validate_name(name)
    conn = _connect()
    if not conn:
        return None
    try:
        row = conn.execute(
            "SELECT * FROM agent_definitions WHERE name = ?", (name,)
        ).fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


def list_definitions() -> list[dict[str, Any]]:
    """Return all definitions ordered by created_at ascending."""
    conn = _connect()
    if not conn:
        return []
    try:
        rows = conn.execute(
            "SELECT * FROM agent_definitions ORDER BY created_at ASC"
        ).fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        conn.close()


# Updatable columns — everything except primary key and created_at.
_UPDATABLE = {
    "instruction",
    "servers",
    "tools",
    "skills",
    "model",
    "use_history",
    "request_params",
}


def update_definition(name: str, **updates: Any) -> dict[str, Any]:
    """Partially update a definition. Raises if `name` not found or no
    valid fields given. Unknown keys raise rather than silently drop —
    silent drops mask caller bugs."""
    _validate_name(name)
    if not updates:
        raise ValueError("update_definition: no fields given")
    unknown = set(updates) - _UPDATABLE
    if unknown:
        raise ValueError(f"update_definition: unknown fields {sorted(unknown)}")

    conn = _connect()
    if not conn:
        raise RuntimeError("agent_definitions: DB not configured")
    try:
        existing = conn.execute(
            "SELECT 1 FROM agent_definitions WHERE name = ?", (name,)
        ).fetchone()
        if not existing:
            raise ValueError(f"agent '{name}' not found")

        encoded = _encode_json_fields(updates)
        if "use_history" in encoded:
            encoded["use_history"] = 1 if encoded["use_history"] else 0

        encoded["updated_at"] = time.time()
        set_clause = ", ".join(f"{col} = ?" for col in encoded.keys())
        params = list(encoded.values()) + [name]
        conn.execute(
            f"UPDATE agent_definitions SET {set_clause} WHERE name = ?", params
        )
        _bump_rev(conn)
        conn.commit()
        logger.info("[AGENT_DEFS] updated '%s' (%s)", name, sorted(updates.keys()))
        return get_definition(name)
    finally:
        conn.close()


def delete_definition(name: str) -> bool:
    """Delete by name. Returns True if a row was removed, False if no
    such row (idempotent for retries)."""
    _validate_name(name)
    conn = _connect()
    if not conn:
        return False
    try:
        cur = conn.execute("DELETE FROM agent_definitions WHERE name = ?", (name,))
        removed = cur.rowcount > 0
        if removed:
            _bump_rev(conn)
            logger.info("[AGENT_DEFS] deleted '%s'", name)
        conn.commit()
        return removed
    finally:
        conn.close()
