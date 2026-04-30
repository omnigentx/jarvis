"""Save/load agent context windows to/from SQLite.

Single source of truth for agent context — no file fallback.
If DB has no snapshot, agent starts fresh (fail visibly).

Uses SPAWN_REGISTRY_DB env var (absolute path) to connect,
following the same pattern as email_server.py, registry_backends.py,
and spawn_progress_bridge.py — safe for subprocess use even after
os.chdir() to workspace.

Edge cases handled:
- Empty context: skipped (no save)
- Serialization error: logged, returns None
- DB write failure: logged, returns None (agent continues running)
- Large context: warning if > 5MB, still saves
- Subprocess CWD changed: SPAWN_REGISTRY_DB is absolute, always works
"""

import json
import logging
import os
import sqlite3
import time
from typing import Any

logger = logging.getLogger(__name__)

# Context size warning threshold (5MB)
_CONTEXT_SIZE_WARN_BYTES = 5 * 1024 * 1024


def _get_db_path() -> str | None:
    """Get absolute DB path from env, following the established pattern."""
    db_path = os.environ.get("SPAWN_REGISTRY_DB")
    if not db_path:
        # Fallback for parent process (routes, etc.)
        fallback = os.path.join("data", "jarvis.db")
        if os.path.exists(fallback):
            return os.path.abspath(fallback)
        return None
    return db_path


def _ensure_table(conn: sqlite3.Connection) -> None:
    """Create agent_context_snapshots table if missing."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_context_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            session_id TEXT,
            team_name TEXT,
            context_json TEXT NOT NULL,
            message_count INTEGER DEFAULT 0,
            total_input_tokens INTEGER DEFAULT 0,
            total_output_tokens INTEGER DEFAULT 0,
            trigger TEXT DEFAULT 'manual',
            created_at REAL NOT NULL
        )
    """)
    conn.commit()


async def save_agent_context(
    agent: Any,  # AgentProtocol — not type-hinted to avoid import in subprocess
    run_id: str,
    trigger: str,
    *,
    agent_name: str | None = None,
    session_id: str | None = None,
    team_name: str | None = None,
) -> int | None:
    """Serialize agent.message_history → SQLite via raw sqlite3.

    Uses SPAWN_REGISTRY_DB env var for the absolute path — safe from any CWD.
    Returns the snapshot ID on success, None on failure.
    Never raises — all errors are caught and logged.

    Args:
        agent_name: Explicit display name (e.g. "Khoi [SA]"). If not provided,
                    falls back to agent.name which may be a generic internal name
                    like "child" — this is WRONG for team agents.
    """
    db_path = _get_db_path()
    if not db_path:
        logger.debug("[CONTEXT] No DB path available — skip save")
        return None

    try:
        from fast_agent.mcp.prompt_serialization import to_json
    except ImportError as exc:
        logger.debug("[CONTEXT] prompt_serialization unavailable: %s", exc)
        return None

    # Use explicit name, fall back to agent's internal name
    resolved_name = agent_name or getattr(agent, "name", "unknown")
    messages = getattr(agent, "message_history", None)

    if not messages:
        logger.debug("[CONTEXT] Skip empty context: %s (trigger=%s)", resolved_name, trigger)
        return None

    # Serialize via fast-agent's native lossless format
    try:
        context_json = to_json(messages)
    except Exception as exc:
        logger.error("[CONTEXT] Serialization failed: %s — %s", resolved_name, exc, exc_info=True)
        return None

    # Warn on oversized context
    context_size = len(context_json.encode("utf-8"))
    if context_size > _CONTEXT_SIZE_WARN_BYTES:
        logger.warning(
            "[CONTEXT] Large context for %s: %.1f MB (%d messages)",
            resolved_name, context_size / (1024 * 1024), len(messages),
        )

    # Extract token stats from usage accumulator if available
    total_input = 0
    total_output = 0
    try:
        llm = getattr(agent, "llm", None)
        if llm:
            acc = getattr(llm, "usage_accumulator", None)
            if acc:
                total_input = getattr(acc, "total_input_tokens", 0) or 0
                total_output = getattr(acc, "total_output_tokens", 0) or 0
    except Exception:
        pass  # Token stats are nice-to-have, not critical

    try:
        conn = sqlite3.connect(db_path, timeout=10)
        _ensure_table(conn)
        cursor = conn.execute(
            """INSERT INTO agent_context_snapshots
               (run_id, agent_name, session_id, team_name, context_json,
                message_count, total_input_tokens, total_output_tokens,
                trigger, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id, resolved_name, session_id, team_name, context_json,
                len(messages), total_input, total_output,
                trigger, time.time(),
            ),
        )
        conn.commit()
        snapshot_id = cursor.lastrowid
        conn.close()
        logger.info(
            "[CONTEXT] Saved %s: %d messages, trigger=%s, id=%d, size=%.0fKB",
            resolved_name, len(messages), trigger, snapshot_id, context_size / 1024,
        )
        return snapshot_id
    except Exception as exc:
        logger.error("[CONTEXT] DB write failed: %s — %s", resolved_name, exc, exc_info=True)
        return None


def load_latest_context(
    agent_name: str,
    *,
    run_id: str | None = None,
    session_id: str | None = None,
) -> list | None:
    """Load the most recent context snapshot from SQLite.

    Returns list[PromptMessageExtended] or None.
    NO file fallback — if DB has no snapshot, returns None (agent starts fresh).
    """
    db_path = _get_db_path()
    if not db_path:
        return None

    try:
        from fast_agent.mcp.prompt_serialization import from_json
    except ImportError:
        return None

    try:
        conn = sqlite3.connect(db_path, timeout=10)
        _ensure_table(conn)

        query = "SELECT id, context_json, trigger FROM agent_context_snapshots WHERE agent_name = ?"
        params: list[Any] = [agent_name]
        if run_id:
            query += " AND run_id = ?"
            params.append(run_id)
        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)
        query += " ORDER BY created_at DESC LIMIT 1"

        row = conn.execute(query, params).fetchone()
        conn.close()

        if not row:
            logger.info("[CONTEXT] No snapshot for %s — agent starts fresh", agent_name)
            return None

        snapshot_id, context_json, trigger = row
        messages = from_json(context_json)
        logger.info(
            "[CONTEXT] Loaded %s: %d messages from snapshot #%d (trigger=%s)",
            agent_name, len(messages), snapshot_id, trigger,
        )
        return messages
    except Exception as exc:
        logger.error("[CONTEXT] Load failed: %s — %s", agent_name, exc, exc_info=True)
        return None


def load_latest_context_json(
    agent_name: str,
    *,
    run_id: str | None = None,
    session_id: str | None = None,
) -> str | None:
    """Load latest context as raw JSON string — for temp history files.

    Unlike load_latest_context() which deserializes to PromptMessageExtended,
    this returns the raw JSON string directly from DB — efficient for writing
    to temp history files without deserialization/re-serialization overhead.
    """
    db_path = _get_db_path()
    if not db_path:
        return None

    try:
        conn = sqlite3.connect(db_path, timeout=10)
        _ensure_table(conn)

        query = "SELECT id, context_json, trigger FROM agent_context_snapshots WHERE agent_name = ?"
        params: list[Any] = [agent_name]
        if run_id:
            query += " AND run_id = ?"
            params.append(run_id)
        if session_id:
            query += " AND session_id = ?"
            params.append(session_id)
        query += " ORDER BY created_at DESC LIMIT 1"

        row = conn.execute(query, params).fetchone()
        conn.close()

        if not row:
            logger.info("[CONTEXT] No snapshot (json) for %s — agent starts fresh", agent_name)
            return None

        snapshot_id, context_json, trigger = row
        logger.info(
            "[CONTEXT] Loaded raw JSON for %s: snapshot #%d (trigger=%s, size=%.0fKB)",
            agent_name, snapshot_id, trigger, len(context_json) / 1024,
        )
        return context_json
    except Exception as exc:
        logger.error("[CONTEXT] Load JSON failed: %s — %s", agent_name, exc, exc_info=True)
        return None


def get_context_snapshot_meta(
    agent_name: str,
    *,
    run_id: str | None = None,
    limit: int = 5,
) -> list[dict]:
    """Get metadata of recent context snapshots (for API/dashboard).

    Returns list of dicts WITHOUT context_json (too large for API response).
    """
    db_path = _get_db_path()
    if not db_path:
        return []

    try:
        conn = sqlite3.connect(db_path, timeout=10)
        _ensure_table(conn)

        query = """SELECT id, run_id, agent_name, session_id, team_name,
                          message_count, total_input_tokens, total_output_tokens,
                          trigger, created_at
                   FROM agent_context_snapshots WHERE agent_name = ?"""
        params: list[Any] = [agent_name]
        if run_id:
            query += " AND run_id = ?"
            params.append(run_id)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(query, params).fetchall()
        conn.close()

        return [
            {
                "id": r[0],
                "run_id": r[1],
                "agent_name": r[2],
                "session_id": r[3],
                "team_name": r[4],
                "message_count": r[5],
                "total_input_tokens": r[6],
                "total_output_tokens": r[7],
                "trigger": r[8],
                "created_at": r[9],
            }
            for r in rows
        ]
    except Exception:
        return []


def get_context_messages(snapshot_id: int) -> list[dict] | None:
    """Load and parse context messages by snapshot ID.

    Uses fast-agent's from_json() to deserialize PromptMessageExtended objects,
    then extracts text content, tool_calls, and tool_results for UI rendering.
    """
    db_path = _get_db_path()
    if not db_path:
        return None

    try:
        from fast_agent.mcp.prompt_serialization import from_json
    except ImportError:
        return None

    try:
        conn = sqlite3.connect(db_path, timeout=10)
        _ensure_table(conn)

        row = conn.execute(
            "SELECT context_json FROM agent_context_snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()
        conn.close()

        if not row:
            return None

        messages = from_json(row[0])
        result = []

        for msg in messages:
            role = str(msg.role) if msg.role else "unknown"

            # Extract text from content parts
            text_parts = []
            for part in (msg.content or []):
                text = getattr(part, "text", None)
                if text:
                    text_parts.append(text)

            # Extract tool calls: dict of {call_id: CallToolRequest}
            tool_call_list = []
            if msg.tool_calls:
                for call_id, call_req in msg.tool_calls.items():
                    params = getattr(call_req, "params", None)
                    tool_call_list.append({
                        "id": call_id,
                        "name": getattr(params, "name", "unknown") if params else "unknown",
                        "arguments": getattr(params, "arguments", {}) if params else {},
                    })

            # Extract tool results: dict of {call_id: CallToolResult}
            tool_result_list = []
            if msg.tool_results:
                for call_id, call_result in msg.tool_results.items():
                    result_text_parts = []
                    for part in (getattr(call_result, "content", None) or []):
                        text = getattr(part, "text", None)
                        if text:
                            result_text_parts.append(text)
                    tool_result_list.append({
                        "id": call_id,
                        "content": "\n".join(result_text_parts) if result_text_parts else "(binary/empty)",
                        "is_error": getattr(call_result, "isError", False),
                    })

            # Build content string — prefer text, fall back to tool summary
            if text_parts:
                content = "\n".join(text_parts)
            elif tool_call_list:
                content = "\n".join(
                    f"🔧 {tc['name']}({json.dumps(tc['arguments'], ensure_ascii=False)})"
                    for tc in tool_call_list
                )
            elif tool_result_list:
                content = "\n".join(
                    f"{'❌' if tr['is_error'] else '📄'} {tr['content']}"
                    for tr in tool_result_list
                )
            else:
                content = "(empty)"

            result.append({
                "role": role,
                "content": content,
                "tool_calls": tool_call_list or None,
                "tool_results": tool_result_list or None,
                "has_tool_calls": bool(tool_call_list),
                "has_tool_results": bool(tool_result_list),
                "tool_count": len(tool_call_list),
            })
        return result
    except Exception as exc:
        logger.error("[CONTEXT] Parse failed for snapshot #%d: %s", snapshot_id, exc)
        return None


def delete_agent_snapshots(agent_name: str) -> int:
    """Delete all context snapshots for a given agent.

    Called when agent/team is deleted to prevent orphaned data.
    Returns number of deleted rows.
    """
    db_path = _get_db_path()
    if not db_path:
        return 0

    try:
        conn = sqlite3.connect(db_path, timeout=10)
        _ensure_table(conn)

        cursor = conn.execute(
            "DELETE FROM agent_context_snapshots WHERE agent_name = ?",
            (agent_name,),
        )
        deleted = cursor.rowcount
        conn.commit()
        conn.close()

        if deleted:
            logger.info("[CONTEXT] Deleted %d snapshots for agent '%s'", deleted, agent_name)
        return deleted
    except Exception as exc:
        logger.error("[CONTEXT] Failed to delete snapshots for '%s': %s", agent_name, exc)
        return 0

