"""
Spawn Progress Bridge — Cross-process forwarding of SpawnEvents to SSE.

Architecture:
  MCP subprocess (agent_spawner_server.py)
  → connects to Unix domain socket (SPAWN_EVENT_SOCKET env var)
  → sends SpawnEvents as JSON lines over socket

  Main backend process (SpawnEventSocketServer + this module)
  → receives events via socket
  → pushes events into ProgressEventManager's SSE queue (chat-stream)
  → persists events into agent_activities SQLite table
  → broadcasts events via ActivityStreamManager (global SSE)
  → upserts spawn_records SQLite table

Also logs all events to the spawn_activity logger for ops/debugging.
"""

import json
import logging
import os
from typing import Any, Protocol

logger = logging.getLogger("spawn_activity")

_EMAIL_TOOL_NAMES = {"send_email", "email__send_email"}


def _sanitize_tool_args_preview(tool_name: str, args_preview: str) -> str:
    """Redact sensitive email body content before broadcasting over SSE."""
    preview = (args_preview or "").strip()
    if tool_name not in _EMAIL_TOOL_NAMES or not preview:
        return preview[:60]

    try:
        parsed = json.loads(preview)
        if isinstance(parsed, dict):
            safe = {}
            if parsed.get("to"):
                safe["to"] = parsed["to"]
            if parsed.get("cc"):
                safe["cc"] = parsed["cc"]
            if parsed.get("subject"):
                safe["subject"] = str(parsed["subject"])[:40]
            if "no_reply" in parsed:
                safe["no_reply"] = parsed["no_reply"]
            if "priority" in parsed:
                safe["priority"] = parsed["priority"]
            safe["content_redacted"] = True
            return json.dumps(safe, ensure_ascii=False)[:60]
    except (TypeError, json.JSONDecodeError):
        pass

    lowered = preview.lower()
    body_idx = lowered.find("body=")
    if body_idx >= 0:
        safe_preview = preview[:body_idx].strip()
        suffix = "content=[REDACTED]"
        return f"{safe_preview} {suffix}".strip()[:60]

    return "email payload [REDACTED]"[:60]




def _sanitize_event_data(event_type_str: str, data: dict | None) -> dict:
    """Return a copy of event data safe for UI-facing broadcast/persistence."""
    safe_data = dict(data or {})
    if event_type_str != "tool_call":
        return safe_data

    tool_name = safe_data.get("tool_name", "")
    if tool_name not in _EMAIL_TOOL_NAMES:
        return safe_data

    preview = _sanitize_tool_args_preview(tool_name, safe_data.get("args_preview", ""))
    if preview:
        safe_data["args_preview"] = preview
    else:
        safe_data.pop("args_preview", None)
    return safe_data

class _ProgressManager(Protocol):
    """Minimal interface for ProgressEventManager."""
    def push(self, request_id: str, event_type: str, data: dict) -> None: ...


# Map SpawnEvent.event → (SSE event_type, message_template)
_EVENT_MAP = {
    "started": ("spawn_started", "🚀 {agent_name} starting..."),
    "mcp_connected": ("spawn_mcp", "📡 {agent_name}: {server_name} {status}"),
    "thinking": ("spawn_thinking", "🤔 {agent_name} thinking..."),
    "response": ("spawn_response", "💬 {agent_name} responded"),
    "tool_call": ("spawn_tool_call", "🔧 {agent_name} calling {tool_name}"),
    "tool_result": ("spawn_tool_done", "✅ {agent_name} completed {tool_name}"),
    "result": ("spawn_result", "✅ {agent_name} finished ({duration_seconds:.0f}s)"),
    "error": ("spawn_error", "❌ {agent_name} error: {error_msg}"),
    "removed": ("spawn_removed", "🗑️ {agent_name} removed"),
    "idle": ("spawn_idle", "💤 {agent_name} waiting for new task..."),
    "resumed": ("spawn_resumed", "⚡ {agent_name} received new task"),
    "agent_completed": ("spawn_agent_completed", "📋 {agent_name} completed — status: {status}"),
    # Lifecycle hook events (from SpawnLifecycleHooks)
    "lifecycle_pre_spawn": ("spawn_lifecycle", "⏳ {agent_name} preparing to spawn..."),
    "lifecycle_spawn_registered": ("spawn_lifecycle", "📝 {agent_name} registered in registry"),
    "lifecycle_process_started": ("spawn_lifecycle", "🔄 {agent_name} process started"),
    "lifecycle_completed": ("spawn_lifecycle", "✅ {agent_name} completed"),
    "lifecycle_error": ("spawn_lifecycle", "❌ {agent_name} lifecycle error"),
    "lifecycle_cancelled": ("spawn_lifecycle", "🚫 {agent_name} cancelled"),
    "lifecycle_pre_cleanup": ("spawn_lifecycle", "🧹 {agent_name} cleaning up..."),
    "lifecycle_after_cleanup": ("agent_removed", "🗑️ {agent_name} removed"),
    # Pause/Resume events (from PauseSignalHandler in subprocess)
    "agent_paused": ("agent_paused", "⏸️ {agent_name} đã tạm dừng"),
    "agent_resumed": ("agent_resumed", "▶️ {agent_name} đã tiếp tục"),
    "token_usage": ("spawn_token_usage", "📊 {agent_name} token usage"),
    "runtime_config": ("spawn_runtime_config", "⚙️ {agent_name} runtime config loaded"),
    "mcp_status": ("spawn_mcp_status", "🔌 {agent_name} MCP: {status}"),
    # Team-level lifecycle events
    "team_spawned": ("team_spawned", "🏗️ Team {team_name} initialized"),
    "team_member_spawned": ("team_member_spawned", "👤 {agent_name} ({role}) joined team {team_name}"),
}


class SpawnProgressBridge:
    """Processes SpawnEvents received via Unix domain socket.

    Events are received by SpawnEventSocketServer and forwarded here
    for processing: SSE broadcast, DB persistence, token tracking.

    Usage::

        bridge = SpawnProgressBridge(progress_manager, registry_db=db)

        # Socket server calls bridge.process_event(raw_line) for each event

        # Before chat processing:
        bridge.set_request_id(request_id)
        # After chat processing:
        bridge.set_request_id(None)
    """

    def __init__(self, progress_manager: _ProgressManager, registry_db=None):
        self._pm = progress_manager
        self._request_id: str | None = None
        self._registry_db = registry_db
        self._token_accumulators: dict[str, dict] = {}  # key → last-seen cumulative values
        self._orch_notify_hash: dict[str, str] = {}  # team_name → status hash (dedup)

    def set_request_id(self, request_id: str | None) -> None:
        """Set the active chat request ID for SSE routing."""
        self._request_id = request_id

    def process_event(self, raw_line: str) -> None:
        """Process a raw JSON line event from the socket server.

        This is the public entry point called by SpawnEventSocketServer.
        """
        self._process_event_line(raw_line)

    def _process_event_line(self, line: str) -> None:
        """Parse a JSON line and forward to SSE."""
        try:
            event_data = json.loads(line)
        except json.JSONDecodeError:
            return

        agent_name = event_data.get("agent_name", "agent")
        event_type_str = event_data.get("event_type", "")
        data = event_data.get("data", {})
        run_id = event_data.get("run_id") or data.get("run_id")

        # 1. Always log to spawn_activity logger
        logger.info(
            "[SPAWN] [%s] %s | %s",
            agent_name,
            event_type_str,
            json.dumps(data, ensure_ascii=False, default=str)[:500],
        )

        # 1b. message_turn — forward directly to activity stream after trimming
        # large content blocks. Source-of-truth shape comes from the subprocess
        # ``child_agent.message_history``; we don't synthesize anything here.
        if event_type_str == "message_turn":
            self._forward_message_turn(agent_name, data, event_data)
            return

        # 2. Persist to agent_activities DB (always, regardless of active request).
        # Kept for the legacy AgentDetail "Activity" tab and audit log; no
        # current monitor UI reads it as primary source.
        self._persist_activity(agent_name, event_type_str, data, event_data)

        # 3. Broadcast via ActivityStreamManager (global SSE for monitoring).
        # tool_call / tool_result / response are intentionally NOT broadcast —
        # the message_turn channel (handled at top of this function) is the
        # canonical source for those. We still broadcast lifecycle events
        # (started/idle/error/agent_paused/agent_resumed/...) and "thinking"
        # so the running-status pulse appears between turns.
        if event_type_str not in {"tool_call", "tool_result", "response"}:
            self._broadcast_activity(agent_name, event_type_str, data, event_data)

        # 4. Upsert spawn_records on lifecycle events
        self._upsert_spawn_record(agent_name, event_type_str, data, event_data)

        # 4·R2. Refresh ``last_active_at`` whenever the agent does
        # something concrete (LLM call or tool round-trip). Lets
        # ``get_team_status`` distinguish "agent X stuck for 60s" from
        # "agent X actively working" — the visibility gap that left PM
        # in incident b61af7db idling out after seeing identical
        # ``running`` snapshots.
        if (
            run_id
            and self._registry_db
            and event_type_str in {"thinking", "response", "tool_call", "tool_result"}
        ):
            try:
                import time as _time
                self._registry_db.upsert_record(
                    run_id, {"last_active_at": _time.time()}
                )
            except Exception as _exc:
                logger.debug(
                    "[REGISTRY] last_active_at update failed for %s: %s",
                    agent_name, _exc,
                )

        # 4a. Check team completion — notify when ALL team agents are done
        if event_type_str in ("result", "idle", "agent_completed"):
            self._check_team_completion(agent_name, event_data)

        # 4b. Consolidated orchestrator notification — all workers idle
        if event_type_str in ("result", "idle", "agent_completed"):
            self._notify_orchestrator_on_members_idle(agent_name, event_data)

        # 4c. Handle removal events — clean DB records
        if event_type_str == "removed":
            self._handle_removal(data)
            return  # Don't push removal events to chat SSE

        # 4d. Handle lifecycle cleanup — broadcast agent_removed for UI sync
        if event_type_str == "lifecycle_after_cleanup":
            self._broadcast_agent_removed(agent_name, data, event_data)
            return  # Don't push to chat SSE

        # 5. Handle token_usage events — persist + broadcast (monitoring only, not chat SSE)
        if event_type_str == "token_usage":
            self._handle_token_usage(agent_name, data, event_data)
            return

        # 5b. Handle runtime_config — persist agent's resolved config (monitoring only)
        if event_type_str == "runtime_config":
            self._handle_runtime_config(agent_name, data, event_data)
            return

        # 5c. Handle mcp_status — persist MCP health into spawn_registry
        if event_type_str == "mcp_status":
            self._handle_mcp_status(agent_name, data, event_data)
            return

        # 5d. Team lifecycle events — broadcast to activity stream only, not chat SSE
        if event_type_str.startswith("team_"):
            self._broadcast_activity(agent_name, event_type_str, data, event_data)
            return

        # 6. Push to chat SSE queue if there is an active chat request
        if not self._request_id:
            return

        # Skip thinking events — they have no reasoning content and clutter the UI
        if event_type_str == "thinking":
            return

        # Skip lifecycle events from chat SSE (monitoring only)
        if event_type_str.startswith("lifecycle_"):
            return

        sse_event_type, sse_data = self._map_event(agent_name, event_type_str, data)
        self._pm.push(self._request_id, sse_event_type, sse_data)

    def _map_event(
        self, agent_name: str, event_type_str: str, data: dict
    ) -> tuple[str, dict]:
        """Map a parsed event to an SSE progress event type + data dict."""
        template = _EVENT_MAP.get(event_type_str)
        if not template:
            return "spawn_info", {
                "agent": agent_name,
                "agent_display": agent_name,
                "message": f"ℹ️ {agent_name}: {event_type_str}",
            }

        event_type, msg_template = template

        # Build message string with safe formatting
        fmt_vars = {
            "agent_name": agent_name,
            "role": agent_name,  # backward compat
            "tool_name": data.get("tool_name", ""),
            "server_name": data.get("server_name", ""),
            "status": "✓" if data.get("status", "ok") == "ok" else "✗",
            "duration_seconds": data.get("duration_seconds", 0) or 0,
            "error_msg": str(data.get("message", ""))[:100],
        }
        try:
            message = msg_template.format(**fmt_vars)
        except (KeyError, ValueError):
            message = f"{agent_name}: {event_type_str}"

        sse_data: dict[str, Any] = {
            "agent": agent_name,
            "agent_display": agent_name,
            "message": message,
        }

        # Add tool info for tool_call events
        if event_type_str == "tool_call":
            tool_name = data.get("tool_name", "")
            preview = _sanitize_tool_args_preview(tool_name, data.get("args_preview", ""))
            sse_data["tools"] = [{
                "name": tool_name,
                "args": {"preview": preview} if preview else {},
            }]

        # Add duration for tool_result events
        if event_type_str == "tool_result" and data.get("duration_ms"):
            sse_data["duration_ms"] = int(data["duration_ms"])

        return event_type, sse_data

    # ── DB Persistence ──

    def _persist_activity(self, role: str, event_type_str: str, data: dict, raw: dict) -> None:
        """Insert event into agent_activities table."""
        try:
            from core.database import AgentActivity, get_db_session
            import time

            db = get_db_session()
            try:
                # Build human-readable message
                _, sse_data = self._map_event(role, event_type_str, data)
                message = sse_data.get("message", f"{role}: {event_type_str}")

                safe_data = _sanitize_event_data(event_type_str, data)
                activity = AgentActivity(
                    agent_name=role,
                    run_id=raw.get("run_id") or safe_data.get("run_id"),
                    event_type=event_type_str,
                    message=message,
                    data_json=json.dumps(safe_data, ensure_ascii=False, default=str) if safe_data else None,
                    created_at=raw.get("timestamp") or time.time(),
                )
                db.add(activity)
                db.commit()
            except Exception as e:
                db.rollback()
                logger.warning("Failed to persist activity: %s", e)
            finally:
                db.close()
        except Exception as e:
            logger.warning("Could not import DB for activity persistence: %s", e)

    def _forward_message_turn(self, agent_name: str, data: dict, raw: dict) -> None:
        """Forward a subprocess ``message_turn`` event to the activity stream.

        The subprocess sends the FULL PromptMessageExtended dump; we cache
        that full payload, then apply truncation before broadcasting so
        SSE chunks stay reasonable on the wire. The cache lets the
        ``/messages`` and ``/turns/{idx}/full`` endpoints serve the
        agent's history after the subprocess exits — same dual-track
        contract used for in-process clones.
        """
        try:
            import json as _json
            import time
            from services.activity_stream import activity_stream_manager
            from services.agent_message_stream import (
                trim_message_for_stream,
                _record_recent_turn,
            )

            full = data.get("message") or {}
            turn_idx = data.get("turn_idx")
            if isinstance(turn_idx, int):
                _record_recent_turn(agent_name, turn_idx, full)

            try:
                trimmed = trim_message_for_stream(_json.loads(_json.dumps(full)))
            except Exception:
                trimmed = full

            activity_stream_manager.broadcast({
                "agent_name": agent_name,
                "event_type": "message_turn",
                "run_id": raw.get("run_id") or data.get("run_id"),
                "timestamp": raw.get("timestamp") or time.time(),
                "data": {
                    "turn_idx": turn_idx,
                    "role": data.get("msg_role") or trimmed.get("role"),
                    "message": trimmed,
                },
            })
        except Exception as e:
            logger.warning("[message_stream] forward failed for %s: %s", agent_name, e)

    def _broadcast_activity(self, role: str, event_type_str: str, data: dict, raw: dict) -> None:
        """Broadcast event via ActivityStreamManager for realtime monitoring."""
        try:
            from services.activity_stream import activity_stream_manager
            import time

            safe_data = _sanitize_event_data(event_type_str, data)
            _, sse_data = self._map_event(role, event_type_str, safe_data)
            activity_stream_manager.broadcast({
                "agent_name": role,
                "event_type": event_type_str,
                "message": sse_data.get("message", ""),
                "data": safe_data,
                "run_id": raw.get("run_id") or safe_data.get("run_id"),
                "timestamp": raw.get("timestamp") or time.time(),
            })
        except Exception as e:
            logger.warning("Failed to broadcast activity: %s", e)

    def _broadcast_agent_removed(self, agent_name: str, data: dict, raw: dict) -> None:
        """Broadcast agent_removed event via ActivityStreamManager.

        This is the critical handler for oneshot agent cleanup:
        when a spawned agent completes and is cleaned up, this broadcasts
        an 'agent_removed' event so the dashboard can update its UI
        (e.g., remove the agent card or show 'completed & removed' status).
        """
        try:
            from services.activity_stream import activity_stream_manager
            import time

            run_id = raw.get("run_id") or data.get("run_id", "")
            lifecycle = data.get("lifecycle", "oneshot")
            reason = data.get("reason", "cleanup")

            activity_stream_manager.broadcast({
                "agent_name": agent_name,
                "event_type": "agent_removed",
                "message": f"🗑️ {agent_name} removed ({reason})",
                "data": {
                    "run_id": run_id,
                    "lifecycle": lifecycle,
                    "reason": reason,
                    "agent_name": data.get("agent_name", agent_name),
                    "team_name": data.get("team_name", ""),
                },
                "run_id": run_id,
                "timestamp": raw.get("timestamp") or time.time(),
            })

            logger.info(
                "[LIFECYCLE] Agent removed: %s (run_id=%s, lifecycle=%s, reason=%s)",
                agent_name, run_id, lifecycle, reason,
            )

            # Drop the per-agent turn cache for this name. Without this
            # the global ``_recent_turns`` keyset grows unbounded across
            # team spawns (each new team adds N agent names; old names
            # never get evicted). Per-agent bucket is already capped at
            # 200 turns, but keyset growth across weeks of spawns adds
            # up. Clearing on lifecycle removal is the natural pairing.
            try:
                from services.agent_message_stream import reset_recent_turns
                reset_recent_turns(agent_name)
            except Exception as _evict_exc:
                logger.warning(
                    "Failed to evict _recent_turns for %s: %s",
                    agent_name, _evict_exc,
                )
        except Exception as e:
            logger.warning("Failed to broadcast agent_removed: %s", e)

    def _handle_removal(self, data: dict) -> None:
        """Delete spawn_registry and agent_activities for removed agents."""
        agent_names = data.get("agent_names", [])
        run_ids = data.get("run_ids", [])

        if not agent_names and not run_ids:
            return

        try:
            import sqlite3 as _sqlite3

            # 1. Delete from spawn_registry table (raw sqlite3)
            deleted_records = 0
            if run_ids:
                try:
                    db_path = os.environ.get("SPAWN_REGISTRY_DB", "data/jarvis.db")
                    with _sqlite3.connect(db_path, timeout=10) as conn:
                        placeholders = ",".join("?" * len(run_ids))
                        cursor = conn.execute(
                            f"DELETE FROM spawn_registry WHERE run_id IN ({placeholders})",
                            run_ids,
                        )
                        deleted_records = cursor.rowcount
                except Exception as e:
                    logger.warning("Failed to delete spawn_registry records: %s", e)

            # 2. Delete from agent_activities (SQLAlchemy)
            deleted_activities = 0
            try:
                from core.database import AgentActivity, get_db_session

                db = get_db_session()
                try:
                    if agent_names:
                        deleted_activities += db.query(AgentActivity).filter(
                            AgentActivity.agent_name.in_(agent_names)
                        ).delete(synchronize_session=False)
                    elif run_ids:
                        deleted_activities += db.query(AgentActivity).filter(
                            AgentActivity.run_id.in_(run_ids)
                        ).delete(synchronize_session=False)
                    db.commit()
                except Exception as e:
                    db.rollback()
                    logger.warning("Failed to clean agent_activities on removal: %s", e)
                finally:
                    db.close()
            except Exception as e:
                logger.warning("Could not import DB for activity cleanup: %s", e)

            # 3. Delete context window snapshots
            deleted_snapshots = 0
            try:
                from services.context_persistence import delete_agent_snapshots

                for name in agent_names:
                    if name:
                        deleted_snapshots += delete_agent_snapshots(name)
            except Exception as e:
                logger.warning("Failed to clean context snapshots on removal: %s", e)

            logger.info(
                "[REMOVAL] Cleaned DB: %d spawn_registry, %d activities, %d snapshots for %s",
                deleted_records,
                deleted_activities,
                deleted_snapshots,
                agent_names or run_ids,
            )
        except Exception as e:
            logger.warning("Failed to handle removal: %s", e)

    def _upsert_spawn_record(self, role: str, event_type_str: str, data: dict, raw: dict) -> None:
        """Upsert spawn_records on lifecycle events (started, result, error)."""
        if not self._registry_db:
            return
        if event_type_str not in (
            "started",
            "result",
            "error",
            "lifecycle_spawn_registered",
            "idle",
            "resumed",
            "agent_paused",
            "agent_resumed",
        ):
            return

        try:
            import time
            run_id = raw.get("run_id") or data.get("run_id")
            if not run_id:
                return

            # Read existing DB record for enrichment
            db_rec: dict = {}
            try:
                db_rec = self._registry_db.get_record(run_id) or {}
            except Exception:
                pass

            # Primary source: event data carries lifecycle/team_name
            # (emitted by isolated_runner.py in the 'started' event)
            evt_lifecycle = data.get("lifecycle", "")
            evt_team_name = data.get("team_name", "")

            # Fallback: cross-read from spawn_registry table
            if (not evt_lifecycle and not db_rec.get("lifecycle")) or \
               (not evt_team_name and not db_rec.get("team_name")):
                try:
                    import sqlite3 as _sqlite3
                    import json as _json
                    db_path = os.environ.get("SPAWN_REGISTRY_DB")
                    if db_path:
                        _conn = _sqlite3.connect(db_path, timeout=5)
                        _row = _conn.execute(
                            "SELECT data_json FROM spawn_registry WHERE run_id = ?",
                            (run_id,),
                        ).fetchone()
                        _conn.close()
                        if _row:
                            sr_rec = _json.loads(_row[0])
                            for key in ("lifecycle", "team_name", "role", "task",
                                        "session_id", "pid", "original_config", "servers"):
                                if sr_rec.get(key) and not db_rec.get(key):
                                    db_rec[key] = sr_rec[key]
                except Exception:
                    pass

            # Resolve lifecycle: event > db_rec
            lifecycle = evt_lifecycle or db_rec.get("lifecycle", "")
            team_name = evt_team_name or db_rec.get("team_name", "")

            # Determine status based on lifecycle
            if event_type_str in ("started", "lifecycle_pre_spawn"):
                status = "running"
            elif event_type_str == "lifecycle_registered":
                status = "starting"
            elif event_type_str == "idle":
                status = "idle"
            elif event_type_str == "resumed":
                status = "running"
            elif event_type_str == "agent_paused":
                status = "paused"
            elif event_type_str == "agent_resumed":
                status = "running"
            elif event_type_str == "result":
                status = "idle" if lifecycle == "resumable" else "completed"
            elif event_type_str == "lifecycle_spawn_registered":
                status = data.get("status") or db_rec.get("status") or "starting"
            else:
                status = "error"

            record_data = {
                "agent_name": role,
                "name": role,
                "status": status,
            }
            # Only set started_at on the spawn events — later events (idle,
            # resumed, paused) would otherwise overwrite the original spawn
            # time and corrupt ordering/orchestrator-detection logic that
            # sorts by started_at.
            if event_type_str in ("started", "lifecycle_spawn_registered"):
                record_data["started_at"] = raw.get("timestamp") or time.time()

            # Set lifecycle and team_name (resolved above)
            if lifecycle:
                record_data["lifecycle"] = lifecycle
            if team_name:
                record_data["team_name"] = team_name

            # Enrich from existing DB record
            if db_rec.get("role"):
                record_data["role"] = db_rec["role"]
            if db_rec.get("pid"):
                record_data["pid"] = db_rec["pid"]
            if db_rec.get("task"):
                record_data["task"] = str(db_rec["task"])[:500]
            if db_rec.get("session_id"):
                record_data["session_id"] = db_rec["session_id"]
            if db_rec.get("original_config"):
                record_data["original_config"] = db_rec["original_config"]

            if event_type_str == "result":
                record_data["completed_at"] = raw.get("timestamp") or time.time()
                record_data["result"] = str(data.get("message", ""))[:500]
            elif event_type_str == "error":
                record_data["completed_at"] = raw.get("timestamp") or time.time()
                record_data["error"] = str(data.get("message", ""))[:500]

            self._registry_db.upsert_record(run_id, record_data)
        except Exception as e:
            logger.warning("Failed to upsert spawn record: %s", e)

    def _handle_token_usage(self, agent_name: str, data: dict, raw: dict) -> None:
        """Persist and broadcast DELTA token usage from spawned child processes.

        The subprocess reports cumulative totals that grow with each LLM call.
        We track the last-seen cumulative values per (agent_name, run_id) and
        only forward the DELTA to avoid double-counting on the dashboard.
        """
        try:
            from services.sse_progress import _persist_and_broadcast_token_usage

            run_id = raw.get("run_id") or data.get("run_id") or ""
            key = f"{agent_name}:{run_id}"

            # Current cumulative from subprocess
            cum_input = data.get("input_tokens", 0)
            cum_output = data.get("output_tokens", 0)
            cum_cache_hit = data.get("cache_hit_tokens", 0)
            cum_cache_read = data.get("cache_read_tokens", 0)
            cum_cache_write = data.get("cache_write_tokens", 0)
            cum_reasoning = data.get("reasoning_tokens", 0)

            # Get previous cumulative values
            prev = self._token_accumulators.get(key, {})
            prev_input = prev.get("input", 0)
            prev_output = prev.get("output", 0)
            prev_cache_hit = prev.get("cache_hit", 0)
            prev_cache_read = prev.get("cache_read", 0)
            prev_cache_write = prev.get("cache_write", 0)
            prev_reasoning = prev.get("reasoning", 0)

            # Calculate deltas (clamp to 0 in case of reset)
            delta_input = max(0, cum_input - prev_input)
            delta_output = max(0, cum_output - prev_output)
            delta_cache_hit = max(0, cum_cache_hit - prev_cache_hit)
            delta_cache_read = max(0, cum_cache_read - prev_cache_read)
            delta_cache_write = max(0, cum_cache_write - prev_cache_write)
            delta_reasoning = max(0, cum_reasoning - prev_reasoning)

            # Update accumulator with current cumulative values
            self._token_accumulators[key] = {
                "input": cum_input,
                "output": cum_output,
                "cache_hit": cum_cache_hit,
                "cache_read": cum_cache_read,
                "cache_write": cum_cache_write,
                "reasoning": cum_reasoning,
            }

            # Skip if no delta (duplicate event)
            if delta_input == 0 and delta_output == 0:
                return

            tokens = {
                "input": delta_input,
                "output": delta_output,
                "total": delta_input + delta_output,
                "model": data.get("model", "unknown"),
                "cache_hit": delta_cache_hit,
                "cache_read": delta_cache_read,
                "cache_write": delta_cache_write,
                "reasoning": delta_reasoning,
            }
            _persist_and_broadcast_token_usage(agent_name, run_id, tokens)
            logger.info(
                "[TOKEN] Spawned agent %s: model=%s Δin=%d Δout=%d Δcache=%d (cum: in=%d out=%d)",
                agent_name,
                tokens["model"],
                delta_input,
                delta_output,
                delta_cache_hit + delta_cache_read,
                cum_input,
                cum_output,
            )
        except Exception as e:
            logger.warning("Failed to handle spawned token_usage: %s", e)

    def _handle_runtime_config(self, agent_name: str, data: dict, raw: dict) -> None:
        """Persist runtime-resolved config from a spawned agent.

        The isolated_runner emits this event after the agent is fully initialized,
        containing the resolved instruction (with skills injected), loaded skill
        manifests, and per-server tool lists — all read from the live agent instance.
        """
        run_id = raw.get("run_id") or data.get("run_id")
        if not run_id:
            return

        runtime_config = {
            "resolved_instruction": data.get("resolved_instruction", ""),
            "skills": data.get("skills", []),
            "tools": data.get("tools", {}),
        }

        # 1. Update SQLite spawn record
        if self._registry_db:
            try:
                self._registry_db.upsert_record(run_id, {
                    "agent_name": agent_name,
                    "runtime_config": runtime_config,
                })
            except Exception as e:
                logger.debug("Failed to update SQLite with runtime_config: %s", e)

        # 3. Upsert per-server tool lists into mcp_server_tools table
        tools_data = runtime_config.get("tools", {})
        if tools_data and self._registry_db:
            try:
                updated = self._registry_db.bulk_upsert_server_tools(tools_data)
                if updated:
                    logger.debug("[RUNTIME_CONFIG] Cached tools for %d servers", updated)
            except Exception as e:
                logger.debug("Failed to cache server tools: %s", e)

        logger.info(
            "[RUNTIME_CONFIG] %s: skills=%d servers=%d",
            agent_name,
            len(runtime_config["skills"]),
            len(runtime_config["tools"]),
        )

    def _handle_mcp_status(self, agent_name: str, data: dict, raw: dict) -> None:
        """Persist MCP health status from spawned agent.

        Reports which configured MCP servers connected vs failed,
        enabling dashboard monitoring and debugging of silent failures.
        """
        run_id = raw.get("run_id") or data.get("run_id")
        total_configured = data.get("total_configured", 0)
        total_connected = data.get("total_connected", 0)
        total_failed = data.get("total_failed", 0)
        servers = data.get("servers", {})

        # Persist to spawn_registry
        if run_id and self._registry_db:
            try:
                self._registry_db.upsert_record(run_id, {
                    "agent_name": agent_name,
                    "mcp_status": {
                        "total_configured": total_configured,
                        "total_connected": total_connected,
                        "total_failed": total_failed,
                        "servers": servers,
                    },
                })
            except Exception as e:
                logger.debug("Failed to update SQLite with mcp_status: %s", e)

        # Log summary with pass/fail
        if total_failed > 0:
            failed_names = [s for s, v in servers.items() if v.get("status") == "failed"]
            logger.warning(
                "[MCP_STATUS] ❌ %s: %d/%d connected, FAILED: %s",
                agent_name, total_connected, total_configured, failed_names,
            )
        else:
            logger.info(
                "[MCP_STATUS] ✅ %s: %d/%d all connected",
                agent_name, total_connected, total_configured,
            )

    # ── Active-meeting awareness for completion notifications ──

    @staticmethod
    def _active_meetings_with_members(member_names: set[str]) -> list[dict]:
        """Return active meetings (``ended=0``) whose participants overlap
        with ``member_names``.

        Used by both completion-notification paths to suppress the misleading
        "team finished" message when the team is actually idled mid-meeting
        (incident b61af7db: PM saw "All members finished | No output" while
        meeting was hanging on a stuck speaker).

        Returns a list of dicts with the fields needed for messaging:
        ``meeting_id``, ``agenda``, ``current_speaker``, ``current_round``,
        ``max_rounds``, ``last_action_at``, ``last_action_ago``,
        ``last_3_turns``. Empty list if no overlap or DB unavailable.
        """
        import sqlite3 as _sqlite3
        import time as _time

        if not member_names:
            return []

        db_path = os.environ.get("SPAWN_REGISTRY_DB", "data/jarvis.db")
        results: list[dict] = []

        try:
            with _sqlite3.connect(db_path, timeout=5) as conn:
                conn.row_factory = _sqlite3.Row
                rows = conn.execute(
                    "SELECT meeting_id, config_json, state_json "
                    "FROM meetings "
                    "WHERE json_extract(state_json, '$.ended') = 0"
                ).fetchall()

                for r in rows:
                    try:
                        config = json.loads(r["config_json"]) if r["config_json"] else {}
                        state = json.loads(r["state_json"]) if r["state_json"] else {}
                    except (json.JSONDecodeError, TypeError):
                        continue

                    parts = state.get("participants") or []
                    if not (set(parts) & member_names):
                        continue

                    current_turn = state.get("current_turn", 0)
                    speaker = parts[current_turn] if current_turn < len(parts) else "(unknown)"

                    last_at = state.get("turn_started_at") or 0.0
                    ago_sec = max(0, int(_time.time() - last_at)) if last_at else None

                    # Pull last 3 turns for context preview
                    turn_rows = conn.execute(
                        "SELECT agent, message, round FROM meeting_transcripts "
                        "WHERE meeting_id = ? ORDER BY id DESC LIMIT 3",
                        (r["meeting_id"],),
                    ).fetchall()
                    last_3 = [
                        {
                            "agent": tr["agent"],
                            "round": tr["round"],
                            "message_preview": (tr["message"] or "")[:160].replace("\n", " "),
                        }
                        for tr in reversed(turn_rows)
                    ]

                    results.append({
                        "meeting_id": r["meeting_id"],
                        "agenda": config.get("agenda", ""),
                        "current_speaker": speaker,
                        "current_round": state.get("current_round", 1),
                        "max_rounds": state.get("max_rounds", 0),
                        "last_action_at": last_at,
                        "last_action_ago_sec": ago_sec,
                        "last_3_turns": last_3,
                    })
        except Exception as exc:
            logger.debug("[ACTIVE_MEETING] Lookup failed: %s", exc)
            return []

        return results

    @staticmethod
    def _format_active_meetings_warning(active: list[dict]) -> str:
        """Render the meeting-stalled warning block (markdown).

        Caller embeds this into the team-completion message so the
        orchestrator (PM) sees actionable state — bottleneck speaker,
        time since last turn, last 3 turns — instead of "No output".
        """
        if not active:
            return ""

        lines = ["", "⚠️ **Active meetings detected — team is NOT done:**", ""]
        for m in active:
            ago_sec = m.get("last_action_ago_sec")
            ago_str = f"{ago_sec}s ago" if ago_sec is not None else "(unknown)"
            lines.append(
                f"- Meeting `{m['meeting_id']}` — round {m['current_round']}/{m['max_rounds']}, "
                f"waiting on **{m['current_speaker']}** (last action: {ago_str})"
            )
            agenda = (m.get("agenda") or "").strip()
            if agenda:
                lines.append(f"  - Agenda: {agenda[:120]}")
            for t in m.get("last_3_turns", []):
                lines.append(
                    f"  - [{t['agent']} R{t['round']}] {t['message_preview']}"
                )
        lines.append("")
        lines.append(
            "Action: review transcript and either resume the bottleneck "
            "speaker, end the meeting via verdict, or use ``leave_meeting`` "
            "to release stuck participants."
        )
        return "\n".join(lines)

    # ── Team Completion Notification ──

    def _check_team_completion(self, trigger_agent: str, raw: dict) -> None:
        """Check if all team agents are done → create notification if so.

        Uses spawn_registry DB as single source of truth.
        """
        if not self._registry_db:
            return

        # Resolve team_name from registry for this agent
        run_id = raw.get("run_id") or raw.get("data", {}).get("run_id")
        if not run_id:
            return

        record = self._registry_db.get_record(run_id)
        if not record:
            return

        team_name = record.get("team_name", "")
        if not team_name:
            return  # Solo agent, not part of a team

        # Restrict to members of THIS session — team_name is reused across
        # spawns ("toolset-self-audit" may be re-run daily), so filtering
        # by team_name alone would mix yesterday's idle agents into
        # today's completion check and dedupe against an unrelated past
        # notification. The session_id is the unique handle.
        session_id = record.get("session_id", "")
        if not session_id:
            return  # Pre-session agents (very old rows) — skip safely

        members = [
            m for m in self._registry_db.find_by_team_name(team_name)
            if m.get("session_id") == session_id
        ]
        if not members:
            return

        # Only consider spawned agents (exclude "available" = not yet spawned)
        _terminal = {"completed", "idle", "error", "timeout", "cancelled"}
        spawned = [m for m in members if m.get("status") not in ("available", None)]
        if not spawned:
            return

        all_done = all(m.get("status") in _terminal for m in spawned)
        if not all_done:
            return

        # Dedupe per-session, not per-team-name: a re-spawn of the same
        # named team is a brand new completion event and deserves its
        # own notification.
        if self._has_team_notification(session_id):
            return

        # Find orchestrator result
        orchestrator = self._find_orchestrator(spawned)
        result_text = orchestrator.get("result", "") if orchestrator else ""
        orch_name = orchestrator.get("agent_name", team_name) if orchestrator else team_name

        self._create_team_notification(
            team_name, orch_name, result_text, spawned,
            session_id=session_id,
        )

    def _find_orchestrator(self, members: list[dict]) -> dict | None:
        """Find the orchestrator agent from team members.

        Preference: role containing 'pm' or 'orchestrator'.
        Fallback: first spawned agent (orchestrator spawns first).
        """
        for m in members:
            role = (m.get("role") or "").lower()
            if "pm" in role or "orchestrator" in role:
                return m
        # Fallback: first agent by started_at (orchestrator spawns first)
        members_sorted = sorted(members, key=lambda m: m.get("started_at", 0))
        return members_sorted[0] if members_sorted else None

    def _compose_team_result_body(
        self, *, team_name: str, agent_name: str, result: str
    ) -> tuple[str, str]:
        """Render notification (preview, content) from the orchestrator's
        ``spawn_registry.result``.

        Fails loud when the field is empty: emits an ERROR log so the
        gap is visible in ops, and renders a notification body that
        explicitly states which agent and run is missing data. We do
        NOT substitute a generic "Team done" placeholder — that hides
        the bug from the user. The proper data source is the per-turn
        write hook in ``services.context_persistence.save_agent_context``;
        if you see this body, that hook did not fire for this run.
        """
        if result and result.strip():
            preview = result[:200].replace("\n", " ").strip()
            return preview, result

        logger.error(
            "[TEAM_NOTIFY] Orchestrator result MISSING for team=%s agent=%s — "
            "spawn_registry.result was empty at team_completion. Expected the "
            "context_persistence write hook to have mirrored the last assistant "
            "text on the agent's final turn. Check that save_agent_context ran "
            "for this run and that the agent produced text content (not only "
            "tool_calls) on its last turn.",
            team_name, agent_name,
        )
        preview = (
            f"⚠️ BUG: orchestrator result missing for {agent_name} "
            f"(team {team_name}) — see logs"
        )
        content = (
            f"## ⚠️ Orchestrator result missing\n\n"
            f"- **Team:** `{team_name}`\n"
            f"- **Orchestrator:** `{agent_name}`\n\n"
            f"`spawn_registry.result` was empty when the team-completion "
            f"notification fired. The notification creator does not fall "
            f"back to other sources (per project policy: fail loud, no "
            f"silent fallbacks).\n\n"
            f"### Likely causes\n"
            f"- The agent's last turn was a tool_call without any "
            f"assistant text content.\n"
            f"- `services.context_persistence.save_agent_context` did not "
            f"run for this run (snapshot trigger missed).\n"
            f"- `spawn_registry` row for this run was missing when the "
            f"snapshot hook tried to mirror the result.\n\n"
            f"Open the agent's latest snapshot in `agent_context_snapshots` "
            f"to recover the response manually."
        )
        return preview, content

    def _has_team_notification(self, session_id: str) -> bool:
        """Dedupe: check if a team_completion notification already
        exists FOR THIS SESSION.

        Keyed by ``session_id`` (not ``team_name``): a reusable team
        name ("toolset-self-audit") spawned twice on different days
        produces two distinct completion events — each deserves its
        own notification. Pre-2026-05-14 this dedupe was per
        ``team_name`` and silently swallowed the second day's
        notification.
        """
        if not session_id:
            return False
        try:
            from core.database import NotificationModel, get_db_session

            db = get_db_session()
            try:
                existing = db.query(NotificationModel).filter(
                    NotificationModel.metadata_json.contains(f'"session_id": "{session_id}"'),
                    NotificationModel.metadata_json.contains('"source": "team_completion"'),
                ).first()
                return existing is not None
            finally:
                db.close()
        except Exception as e:
            logger.warning("Failed to check team notification dedupe: %s", e)
            return False

    def _create_team_notification(
        self, team_name: str, agent_name: str, result: str, members: list[dict],
        *, session_id: str = "",
    ) -> None:
        """Create notification + broadcast SSE for team completion.

        If any team member is still a participant in an active meeting,
        the notification is reframed from "Team finished" to "Team idled
        with active meeting" — this surfaces meeting state (bottleneck
        speaker, last action time, recent turns) so the user can
        intervene instead of believing the team is done.
        """
        try:
            import time
            from core.database import NotificationModel, get_db_session
            from services.cron_scheduler import scheduler_stream_manager

            total = len(members)
            errors = sum(1 for m in members if m.get("status") == "error")

            # Detect "idled mid-meeting" — flips the framing of this notification.
            member_names = {
                m.get("agent_name", "") for m in members if m.get("agent_name")
            }
            active_meetings = self._active_meetings_with_members(member_names)

            if active_meetings:
                meeting_count = len(active_meetings)
                title = (
                    f"⏳ Team {team_name} idled with {meeting_count} active "
                    f"meeting{'s' if meeting_count > 1 else ''} — needs intervention"
                )
                preview = (
                    f"{meeting_count} meeting(s) still open — "
                    f"last waiting on {active_meetings[0]['current_speaker']}"
                )
                content = (
                    (result or "(no orchestrator result)")
                    + self._format_active_meetings_warning(active_meetings)
                )
            elif errors:
                title = f"⚠️ Team {team_name} hoàn thành ({errors}/{total} lỗi)"
                preview, content = self._compose_team_result_body(
                    team_name=team_name, agent_name=agent_name, result=result,
                )
            else:
                title = f"✅ Team {team_name} hoàn thành ({total} agents)"
                preview, content = self._compose_team_result_body(
                    team_name=team_name, agent_name=agent_name, result=result,
                )

            db = get_db_session()
            try:
                notif = NotificationModel(
                    type="agent_result",
                    title=title,
                    preview=preview,
                    content=content,
                    content_type="markdown",
                    is_read=0,
                    created_at=time.time(),
                    # ⚠️ JSON SPACING IS LOAD-BEARING — do NOT change separators.
                    #
                    # ``json.dumps()`` default is ``separators=(', ', ': ')``.
                    # ``routes/agents.delete_team`` and
                    # ``_has_team_notification`` both query this column with
                    # ``metadata_json.contains('"team_name": "X"')`` (note the
                    # space after the colon). If you switch to
                    # ``separators=(',', ':')`` here, BOTH queries silently
                    # match nothing → dedupe breaks (duplicate notifs) AND
                    # delete_team cleanup breaks (orphan notifs blocking
                    # re-spawn of same team_name).
                    #
                    # Future migration: switch both sides to a real JSON1
                    # query (``json_extract(metadata_json, '$.team_name')``)
                    # so spacing becomes irrelevant.
                    metadata_json=json.dumps({
                        "agent": agent_name,
                        "team_name": team_name,
                        "session_id": session_id,
                        "total_agents": total,
                        "errors": errors,
                        "source": "team_completion",
                    }),
                )
                db.add(notif)
                db.commit()
                db.refresh(notif)

                # Broadcast via scheduler SSE (dashboard already handles new_notification)
                scheduler_stream_manager.broadcast({
                    "type": "new_notification",
                    "id": notif.id,
                    "notif_type": "agent_result",
                    "title": title,
                    "preview": preview,
                    "created_at": notif.created_at,
                })

                logger.info(
                    "[TEAM_NOTIFY] Created notification for team '%s': %d agents, %d errors",
                    team_name, total, errors,
                )
            finally:
                db.close()
        except Exception as e:
            logger.warning("Failed to create team notification: %s", e, exc_info=True)

    # ── Consolidated Orchestrator Notification ──

    def _notify_orchestrator_on_members_idle(
        self, trigger_agent: str, raw: dict,
    ) -> None:
        """Send consolidated status report when ALL non-orch members stop running.

        Trigger: agent_completed / result / idle events.
        Delivery: MessageBus → orchestrator inbox.
        If orchestrator is idle → explicitly triggers resume.
        Dedup: status hash prevents duplicate notifications for same state.
        """
        if not self._registry_db:
            return

        # 1. Resolve team from trigger agent
        run_id = raw.get("run_id") or raw.get("data", {}).get("run_id")
        if not run_id:
            return
        record = self._registry_db.get_record(run_id)
        if not record:
            return
        team_name = record.get("team_name", "")
        if not team_name:
            return  # Solo agent

        # 2. Get all team members
        members = self._registry_db.find_by_team_name(team_name)
        if not members:
            return

        # 3. Separate orchestrator vs workers
        orchestrator = self._find_orchestrator(members)
        if not orchestrator:
            return
        orch_name = orchestrator.get("agent_name", "")

        _non_running = {"idle", "completed", "error", "timeout", "cancelled"}
        workers = [
            m for m in members
            if m.get("agent_name") != orch_name
            and m.get("status") not in ("available", None)
        ]
        if not workers:
            return

        # 4. ALL workers must be non-running
        if not all(m.get("status") in _non_running for m in workers):
            return

        # 5. Dedup via status hash
        status_parts = sorted(
            f"{m.get('run_id', '')}:{m.get('status', '')}" for m in workers
        )
        status_hash = "|".join(status_parts)
        if self._orch_notify_hash.get(team_name) == status_hash:
            return  # Already notified for this exact state
        self._orch_notify_hash[team_name] = status_hash

        # 6. Build status report — framing depends on whether the team is
        # idled mid-meeting (b61af7db incident: "All finished | No output"
        # was misleading because the team had stalled inside a meeting).
        status_icons = {
            "idle": "✅", "completed": "✅",
            "error": "❌", "timeout": "⏰", "cancelled": "🚫",
        }

        member_names = {m.get("agent_name", "") for m in workers if m.get("agent_name")}
        member_names.add(orch_name)
        active_meetings = self._active_meetings_with_members(member_names)

        if active_meetings:
            header = (
                f"⏳ **Team Status Update** — members are idle but {len(active_meetings)} "
                f"meeting(s) still open. Team is NOT done."
            )
        else:
            header = "📋 **Team Status Update** — All members have finished."
        lines = [header, ""]
        lines.append("| Member | Status | Summary |")
        lines.append("|--------|--------|---------|")
        for m in workers:
            name = m.get("agent_name", "?")
            status = m.get("status", "?")
            icon = status_icons.get(status, "❓")
            if status == "error":
                summary = (m.get("error", "") or "Unknown error")[:80]
            else:
                summary = (m.get("result", "") or "No output")[:80]
            summary = summary.replace("\n", " ").replace("|", "/").strip()
            lines.append(f"| {name} | {icon} {status} | {summary} |")

        if active_meetings:
            lines.append(self._format_active_meetings_warning(active_meetings))
        else:
            lines.append("\nReview outputs and decide next actions.")
        report = "\n".join(lines)

        # 7. Resolve messages_dir and send via MessageBus
        messages_dir = self._resolve_messages_dir(members)
        if not messages_dir:
            logger.warning(
                "[TEAM_NOTIFY] Cannot resolve messages_dir for team %s",
                team_name,
            )
            return

        try:
            from fast_agent.spawn.message_bus import MessageBus

            bus = MessageBus(messages_dir=messages_dir)
            bus.send(
                from_name="System",
                to_name=orch_name,
                content=report,
                message_type="notification",
                priority="high",
            )
            logger.info(
                "[TEAM_NOTIFY] Sent consolidated status to %s (%d workers, team=%s)",
                orch_name, len(workers), team_name,
            )
        except Exception as e:
            logger.warning("[TEAM_NOTIFY] Failed to send via MessageBus: %s", e, exc_info=True)
            return

        # 8. If orchestrator is idle, trigger resume so it processes the report
        orch_status = orchestrator.get("status", "")
        if orch_status in _non_running:
            import asyncio
            try:
                loop = asyncio.get_event_loop()
                loop.create_task(
                    self._trigger_orchestrator_resume(orchestrator, team_name)
                )
            except RuntimeError:
                logger.warning(
                    "[TEAM_NOTIFY] No event loop — cannot resume idle orchestrator %s",
                    orch_name,
                )

    def _resolve_messages_dir(self, members: list[dict]) -> str:
        """Resolve TEAM_MESSAGES_DIR from any team member's original_config."""
        from pathlib import Path

        for m in members:
            env_vars = m.get("original_config", {}).get("env_vars", {})
            messages_dir = env_vars.get("TEAM_MESSAGES_DIR", "")
            if messages_dir and Path(messages_dir).exists():
                return messages_dir
        return ""

    async def _trigger_orchestrator_resume(
        self, orch_record: dict, team_name: str,
    ) -> None:
        """Resume idle orchestrator to process team status notification.

        Uses the same inject_resume pattern as prompt injection — loads
        context from DB, spawns new subprocess with full conversation history.
        The team status report is already in MessageBus inbox, so
        _check_and_resume_on_inbox will pick it up during spawn.
        """
        orch_name = orch_record.get("agent_name", "")
        try:
            from services.inject_resume import resume_with_inject

            result = await resume_with_inject(
                agent_name=orch_name,
                inject_message=(
                    "Check your inbox for team status updates. "
                    "Review member results and decide next actions."
                ),
                spawn_record=orch_record,
                bridge=self,
            )
            logger.info(
                "[TEAM_NOTIFY] Resumed orchestrator %s → run_id=%s (team=%s)",
                orch_name, result.get("run_id"), team_name,
            )
        except Exception as e:
            logger.warning(
                "[TEAM_NOTIFY] Failed to resume orchestrator %s: %s",
                orch_name, e, exc_info=True,
            )

