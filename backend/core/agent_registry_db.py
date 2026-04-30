"""
Agent Registry DB — thin query layer over the ``spawn_registry`` SQLite table.

The `spawn_registry` table is owned by
``fast_agent.spawn.registry_backends.SqliteBackend``.
This module only *reads* from that table (plus provides helper queries
like mark_stale_running) and manages the separate ``mcp_server_tools``
table.

No more dual JSON+SQLite — single source of truth in SQLite.
"""

import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def _get_db_path() -> str:
    """Resolve the SQLite DB file path from env var."""
    return os.environ.get("SPAWN_REGISTRY_DB", "data/jarvis.db")


def _connect() -> sqlite3.Connection:
    db_path = _get_db_path()
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def _parse_record(row: sqlite3.Row) -> dict:
    """Parse a spawn_registry row (run_id, data_json) into a dict."""
    data = json.loads(row["data_json"])
    data["run_id"] = row["run_id"]  # ensure run_id is in the dict
    # compat alias
    if "name" not in data and "agent_name" in data:
        data["name"] = data["agent_name"]
    return data


class AgentRegistryDB:
    """Query layer for the spawn_registry SQLite table.

    All spawn persistence is handled by SqliteBackend in fast-agent.
    This class provides rich queries for the Jarvis API layer.
    """

    def __init__(self) -> None:
        self._ensure_team_sessions_table()

    def _ensure_team_sessions_table(self) -> None:
        """Create team_sessions table if it doesn't exist (one-time migration)."""
        with _connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS team_sessions (
                    session_id TEXT PRIMARY KEY,
                    data_json  TEXT NOT NULL
                )"""
            )

    # ── Spawn Registry Queries ──────────────────────────────────────

    def get_all(self) -> dict[str, dict]:
        """Get all records as a dict keyed by run_id."""
        try:
            with _connect() as conn:
                rows = conn.execute("SELECT run_id, data_json FROM spawn_registry").fetchall()
                return {row["run_id"]: _parse_record(row) for row in rows}
        except Exception as e:
            logger.warning("[REGISTRY] get_all failed: %s", e)
            return {}

    def find_by_name(self, agent_name: str) -> list[dict]:
        """Find all records for an agent name."""
        try:
            with _connect() as conn:
                rows = conn.execute(
                    "SELECT run_id, data_json FROM spawn_registry"
                ).fetchall()
                results = []
                for row in rows:
                    rec = _parse_record(row)
                    if rec.get("agent_name") == agent_name:
                        results.append(rec)
                # Sort by started_at descending
                results.sort(key=lambda r: r.get("started_at", 0), reverse=True)
                return results
        except Exception as e:
            logger.warning("[REGISTRY] find_by_name failed: %s", e)
            return []

    def get_record(self, run_id: str) -> dict | None:
        """Get a single spawn record by run_id."""
        try:
            with _connect() as conn:
                row = conn.execute(
                    "SELECT run_id, data_json FROM spawn_registry WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                return _parse_record(row) if row else None
        except Exception as e:
            logger.warning("[REGISTRY] get_record failed: %s", e)
            return None

    def list_running(self) -> list[dict]:
        """List records with status running or pending (for shutdown cleanup)."""
        try:
            with _connect() as conn:
                rows = conn.execute(
                    "SELECT run_id, data_json FROM spawn_registry"
                ).fetchall()
                results = []
                for row in rows:
                    rec = _parse_record(row)
                    if rec.get("status") in ("running", "pending"):
                        results.append(rec)
                return results
        except Exception as e:
            logger.warning("[REGISTRY] list_running failed: %s", e)
            return []

    def delete_by_run_id(self, run_id: str) -> bool:
        """Delete a spawn record by run_id."""
        try:
            with _connect() as conn:
                cursor = conn.execute(
                    "DELETE FROM spawn_registry WHERE run_id = ?", (run_id,)
                )
                return cursor.rowcount > 0
        except Exception as e:
            logger.warning("[REGISTRY] delete_by_run_id failed: %s", e)
            return False

    def delete_by_name(self, agent_name: str) -> int:
        """Delete all spawn records for an agent name. Returns count deleted."""
        try:
            with _connect() as conn:
                rows = conn.execute(
                    "SELECT run_id, data_json FROM spawn_registry"
                ).fetchall()
                to_delete = []
                for row in rows:
                    rec = _parse_record(row)
                    if rec.get("agent_name") == agent_name:
                        to_delete.append(row["run_id"])
                if to_delete:
                    placeholders = ",".join("?" * len(to_delete))
                    conn.execute(
                        f"DELETE FROM spawn_registry WHERE run_id IN ({placeholders})",
                        to_delete,
                    )
                return len(to_delete)
        except Exception as e:
            logger.warning("[REGISTRY] delete_by_name failed: %s", e)
            return 0

    def find_by_team_name(self, team_name: str) -> list[dict]:
        """Find all spawn records belonging to a team (read-only query).

        Used to check team completion status from DB (single source of truth).
        """
        try:
            with _connect() as conn:
                rows = conn.execute(
                    "SELECT run_id, data_json FROM spawn_registry"
                ).fetchall()
                results = []
                for row in rows:
                    rec = _parse_record(row)
                    if rec.get("team_name") == team_name:
                        results.append(rec)
                return results
        except Exception as e:
            logger.warning("[REGISTRY] find_by_team_name failed: %s", e)
            return []

    def delete_by_team(self, team_name: str) -> tuple[int, list[str]]:
        """Delete all spawn records for a team. Returns (count, agent_names)."""
        try:
            with _connect() as conn:
                rows = conn.execute(
                    "SELECT run_id, data_json FROM spawn_registry"
                ).fetchall()
                to_delete = []
                agent_names = []
                for row in rows:
                    rec = _parse_record(row)
                    if rec.get("team_name") == team_name:
                        to_delete.append(row["run_id"])
                        name = rec.get("agent_name", "")
                        if name:
                            agent_names.append(name)
                if to_delete:
                    placeholders = ",".join("?" * len(to_delete))
                    conn.execute(
                        f"DELETE FROM spawn_registry WHERE run_id IN ({placeholders})",
                        to_delete,
                    )
                return len(to_delete), agent_names
        except Exception as e:
            logger.warning("[REGISTRY] delete_by_team failed: %s", e)
            return 0, []

    def upsert_record(self, run_id: str, data: dict) -> None:
        """Upsert a spawn record. Used by SpawnProgressBridge for real-time updates."""
        try:
            with _connect() as conn:
                existing = conn.execute(
                    "SELECT data_json FROM spawn_registry WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if existing:
                    # Merge new data into existing
                    record = json.loads(existing["data_json"])
                    record.update(data)
                else:
                    record = data
                    record["run_id"] = run_id

                conn.execute(
                    "INSERT OR REPLACE INTO spawn_registry (run_id, data_json) VALUES (?, ?)",
                    (run_id, json.dumps(record, ensure_ascii=False)),
                )
        except Exception as e:
            logger.warning("[REGISTRY] upsert_record failed: %s", e)

    def mark_stale_running(self) -> int:
        """Clean up stale spawn records on startup.

        Handles two categories:
        1. **Active statuses** (running/pending/idle): Check if PID is dead or
           orphaned (PPID=1). Dead → mark completed/idle. Orphaned → kill + mark.
        2. **Terminal statuses** (cancelled/error): If process is still alive,
           kill it — these are zombies from dead team runs.

        Called at startup to recover from unclean shutdowns (e.g. kill -9).
        Returns count of records cleaned up.
        """
        cleaned = 0
        my_pid = os.getpid()
        active_statuses = ("running", "pending", "idle", "paused")
        zombie_statuses = ("cancelled", "error")
        try:
            with _connect() as conn:
                rows = conn.execute(
                    "SELECT run_id, data_json FROM spawn_registry"
                ).fetchall()
                for row in rows:
                    rec = json.loads(row["data_json"])
                    status = rec.get("status")
                    pid = rec.get("pid")

                    # ── Category 2: Kill zombies in terminal states ──
                    if status in zombie_statuses and pid:
                        try:
                            os.kill(pid, 0)  # Check if alive
                            # Still alive → kill it
                            try:
                                os.kill(pid, 15)  # SIGTERM
                                logger.info(
                                    "[REGISTRY] Killed zombie process PID=%d (%s, status=%s)",
                                    pid, rec.get("agent_name", "?"), status,
                                )
                                cleaned += 1
                            except Exception as e:
                                logger.warning(
                                    "[REGISTRY] Failed to kill zombie PID=%d: %s", pid, e,
                                )
                        except (ProcessLookupError, PermissionError):
                            pass  # Already dead, nothing to do
                        continue

                    # ── Category 1: Clean up active-status records ──
                    if status not in active_statuses:
                        continue
                    lifecycle = rec.get("lifecycle", "")
                    # Target status: resumable agents → idle; oneshot → completed
                    target_status = "idle" if lifecycle == "resumable" else "completed"

                    if not pid:
                        rec["status"] = target_status
                        conn.execute(
                            "INSERT OR REPLACE INTO spawn_registry (run_id, data_json) VALUES (?, ?)",
                            (row["run_id"], json.dumps(rec, ensure_ascii=False)),
                        )
                        cleaned += 1
                        continue
                    try:
                        os.kill(pid, 0)  # Check if process exists
                        # Process is alive — check if orphaned (PPID=1 means parent died)
                        try:
                            import subprocess as _sp
                            result = _sp.run(
                                ["ps", "-p", str(pid), "-o", "ppid="],
                                capture_output=True, text=True, timeout=3,
                            )
                            ppid = int(result.stdout.strip()) if result.stdout.strip() else -1
                        except Exception:
                            ppid = -1

                        if ppid == 1:
                            # Orphaned process — kill it and mark stale
                            logger.info(
                                "[REGISTRY] Killing orphaned subprocess PID=%d (%s)",
                                pid, rec.get("agent_name", "?"),
                            )
                            try:
                                os.kill(pid, 15)  # SIGTERM
                            except Exception:
                                pass
                            rec["status"] = target_status
                            rec["completed_at"] = datetime.now().timestamp()
                            conn.execute(
                                "INSERT OR REPLACE INTO spawn_registry (run_id, data_json) VALUES (?, ?)",
                                (row["run_id"], json.dumps(rec, ensure_ascii=False)),
                            )
                            cleaned += 1
                        # else: process is alive with a real parent → leave it
                    except ProcessLookupError:
                        rec["status"] = target_status
                        rec["completed_at"] = datetime.now().timestamp()
                        conn.execute(
                            "INSERT OR REPLACE INTO spawn_registry (run_id, data_json) VALUES (?, ?)",
                            (row["run_id"], json.dumps(rec, ensure_ascii=False)),
                        )
                        cleaned += 1
                    except PermissionError:
                        pass  # Process exists but we can't signal it
                if cleaned > 0:
                    logger.info("[REGISTRY] Cleaned up %d stale/zombie records at startup", cleaned)
        except Exception as e:
            logger.warning("[REGISTRY] Failed to clean stale records: %s", e)
        return cleaned

    # ── MCP Server Tools (separate table, still uses SQLAlchemy) ───

    def upsert_server_tools(self, server_name: str, tools: list[dict]) -> int:
        """Replace all tools for a server with new data."""
        from core.database import McpServerToolModel, get_db_session
        db = get_db_session()
        try:
            db.query(McpServerToolModel).filter_by(server_name=server_name).delete()
            now = datetime.now().timestamp()
            for tool in tools:
                db.add(McpServerToolModel(
                    server_name=server_name,
                    tool_name=tool.get("name", ""),
                    description=tool.get("description", ""),
                    updated_at=now,
                ))
            db.commit()
            return len(tools)
        except Exception as e:
            db.rollback()
            logger.error("[REGISTRY] Failed to upsert server tools for %s: %s", server_name, e)
            return 0
        finally:
            db.close()

    def get_server_tools(self, server_names: list[str]) -> dict[str, list[dict]]:
        """Get cached tools for the given server names."""
        from core.database import McpServerToolModel, get_db_session
        if not server_names:
            return {}
        db = get_db_session()
        try:
            rows = db.query(McpServerToolModel).filter(
                McpServerToolModel.server_name.in_(server_names)
            ).all()
            result: dict[str, list[dict]] = {}
            for row in rows:
                result.setdefault(row.server_name, []).append({
                    "name": row.tool_name,
                    "description": row.description or "",
                })
            return result
        except Exception as e:
            logger.error("[REGISTRY] Failed to get server tools: %s", e)
            return {}
        finally:
            db.close()

    def bulk_upsert_server_tools(self, tools_by_server: dict[str, list[dict]]) -> int:
        """Upsert tools for multiple servers at once."""
        count = 0
        for server_name, tools in tools_by_server.items():
            if tools:
                self.upsert_server_tools(server_name, tools)
                count += 1
        return count

    # ── Team Sessions ───────────────────────────────────────

    def get_team_session(self, session_id: str) -> dict | None:
        """Get a team session record by session_id."""
        with _connect() as conn:
            row = conn.execute(
                "SELECT data_json FROM team_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            return json.loads(row["data_json"]) if row else None

    def list_team_sessions(self) -> list[dict]:
        """List all team sessions ordered by session_id descending."""
        with _connect() as conn:
            rows = conn.execute(
                "SELECT data_json FROM team_sessions ORDER BY session_id DESC"
            ).fetchall()
            return [json.loads(row["data_json"]) for row in rows]
