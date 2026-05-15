"""Agent Timeline API — unified view of all agent activities.

Merges three data sources into one chronological timeline:
1. Meeting transcripts  (.runtime/data/workspaces/*/meetings/*/transcript.json)
2. Inbox messages       (.runtime/state/messages/*_inbox.jsonl)
3. Spawn lifecycle      (SQLite agent_activities table)

Meeting SSE streams are **event-driven** — no polling.  The
``MeetingEventManager`` pushes events directly to connected clients.
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from services.meeting_events import meeting_event_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["agent-timeline"])

RUNTIME_DIR = Path(".runtime")


def _safe_load_activity_data(raw_json: str | None):
    """Parse activity JSON safely so malformed rows are skipped, not fatal."""
    if not raw_json:
        return None
    try:
        return json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return None


# ── Helpers ────────────────────────────────────────────────────────────


def _load_meetings() -> list[dict]:
    """Load all meetings from SQLite (meetings + meeting_transcripts tables)."""
    import sqlite3 as _sqlite3

    db_path = str(Path("data/jarvis.db").resolve())
    meetings = []
    try:
        conn = _sqlite3.connect(db_path, timeout=5)
        conn.execute("PRAGMA busy_timeout=3000")
        conn.row_factory = _sqlite3.Row

        rows = conn.execute(
            "SELECT meeting_id, config_json, state_json, created_at "
            "FROM meetings ORDER BY created_at DESC"
        ).fetchall()

        # Bulk-load transcripts per meeting
        transcripts: dict[str, list[dict]] = {}
        for t_row in conn.execute(
            "SELECT meeting_id, message, agent, type "
            "FROM meeting_transcripts ORDER BY id"
        ).fetchall():
            mid = t_row["meeting_id"]
            transcripts.setdefault(mid, []).append({
                "agent": t_row["agent"],
                "message": t_row["message"],
                "type": t_row["type"],
            })

        conn.close()

        for r in rows:
            config = json.loads(r["config_json"]) if r["config_json"] else {}
            state = json.loads(r["state_json"]) if r["state_json"] else {}
            mid = r["meeting_id"]

            # config_json: write-once setup (agenda, description, created_by,
            # created_at). state_json: ALL mutable fields, including
            # participants, max_rounds, turn pointers (post-B1). Fall back
            # to config_json for legacy rows written before the refactor.
            meetings.append({
                "meeting_id": mid,
                "agenda": config.get("agenda", ""),
                "description": config.get("description", ""),
                "participants": state.get("participants") or config.get("participants", []),
                "max_rounds": state.get("max_rounds") or config.get("max_rounds"),
                "created_by": config.get("created_by", ""),
                "created_at": r["created_at"],
                "outcome": state.get("outcome"),
                "ended": state.get("ended", False),
                "started": state.get("started", False),
                "joined": state.get("joined", []),
                "current_round": state.get("current_round"),
                "current_turn": state.get("current_turn", 0),
                "turn_started_at": state.get("turn_started_at"),
                "transcript": transcripts.get(mid, []),
            })
    except Exception as e:
        logger.warning("Failed to load meetings from SQLite: %s", e)

    return meetings


def _load_inbox_messages() -> list[dict]:
    """Load all agent inbox messages from JSONL files."""
    messages = []
    messages_dirs = [
        RUNTIME_DIR / "state" / "messages",
    ]
    # Also check per-session message dirs
    for d in (RUNTIME_DIR / "state" / "messages").iterdir() if (RUNTIME_DIR / "state" / "messages").exists() else []:
        if d.is_dir():
            messages_dirs.append(d)

    seen_dirs = set()
    for msg_dir in messages_dirs:
        if not msg_dir.exists() or msg_dir in seen_dirs:
            continue
        seen_dirs.add(msg_dir)
        for inbox_file in msg_dir.glob("*_inbox.jsonl"):
            try:
                for line in inbox_file.read_text(encoding="utf-8").strip().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                        messages.append(msg)
                    except json.JSONDecodeError:
                        continue
            except OSError as e:
                logger.warning("Failed to read inbox %s: %s", inbox_file, e)

    return messages


def _load_spawn_activities(
    agent: Optional[str] = None, limit: int = 200
) -> list[dict]:
    """Load spawn lifecycle events from SQLite."""
    try:
        from core.database import AgentActivity, get_db_session

        db = get_db_session()
        try:
            query = db.query(AgentActivity).order_by(
                AgentActivity.created_at.desc()
            )
            if agent:
                query = query.filter(AgentActivity.agent_name == agent)
            query = query.limit(limit)

            activities = []
            for row in query.all():
                activities.append(
                    {
                        "id": row.id,
                        "agent_name": row.agent_name,
                        "run_id": row.run_id,
                        "event_type": row.event_type,
                        "message": row.message,
                        "data": _safe_load_activity_data(row.data_json),
                        "created_at": row.created_at,
                    }
                )
            return activities
        finally:
            db.close()
    except Exception as e:
        logger.warning("Failed to load spawn activities: %s", e)
        return []


def _parse_timestamp(ts) -> float:
    """Convert ISO string or float timestamp to float epoch."""
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            from datetime import datetime

            # Try ISO format
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.timestamp()
        except (ValueError, TypeError):
            return 0.0
    return 0.0


def _build_unified_timeline(
    agent: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """Merge all data sources into a single chronological timeline."""
    events: list[dict] = []

    # 1. Meeting transcript entries
    for meeting in _load_meetings():
        for entry in meeting.get("transcript", []):
            entry_agent = entry.get("agent", "")
            if agent and agent.lower() not in entry_agent.lower():
                continue
            if event_type and event_type not in ("meeting", "all"):
                continue

            events.append(
                {
                    "source": "meeting",
                    "type": f"meeting_{entry.get('type', 'speak')}",
                    "agent": entry_agent,
                    "content": entry.get("message", ""),
                    "timestamp": _parse_timestamp(entry.get("timestamp", "")),
                    "timestamp_display": entry.get("timestamp", ""),
                    "metadata": {
                        "meeting_id": meeting["meeting_id"],
                        "workspace": meeting["workspace"],
                        "agenda": meeting["agenda"],
                        "round": entry.get("round"),
                        "turn": entry.get("turn"),
                        "outcome": meeting.get("outcome"),
                    },
                }
            )

    # 2. Inbox messages
    if not event_type or event_type in ("message", "all"):
        for msg in _load_inbox_messages():
            msg_agent = msg.get("from_name", "")
            if agent and agent.lower() not in msg_agent.lower():
                continue

            events.append(
                {
                    "source": "message",
                    "type": f"inbox_{msg.get('message_type', 'task')}",
                    "agent": msg_agent,
                    "content": msg.get("content", ""),
                    "timestamp": _parse_timestamp(msg.get("timestamp", 0)),
                    "timestamp_display": "",
                    "metadata": {
                        "message_id": msg.get("message_id"),
                        "to": msg.get("to_name"),
                        "priority": msg.get("priority", "normal"),
                        "reply_to": msg.get("reply_to"),
                        "status": msg.get("status"),
                    },
                }
            )

    # 3. Spawn lifecycle events
    if not event_type or event_type in ("spawn", "all"):
        for act in _load_spawn_activities(agent=agent, limit=limit * 2):
            events.append(
                {
                    "source": "spawn",
                    "type": f"spawn_{act['event_type']}",
                    "agent": act["agent_name"],
                    "content": act.get("message", ""),
                    "timestamp": act.get("created_at", 0),
                    "timestamp_display": "",
                    "metadata": {
                        "run_id": act.get("run_id"),
                        "data": act.get("data"),
                    },
                }
            )

    # Sort by timestamp descending (newest first)
    events.sort(key=lambda e: e.get("timestamp", 0), reverse=True)

    return events[:limit]


# ── Endpoints ──────────────────────────────────────────────────────────


@router.get("/timeline")
async def get_timeline(
    agent: Optional[str] = Query(None, description="Filter by agent name"),
    type: Optional[str] = Query(
        None,
        description="Filter event type: meeting, message, spawn, all",
    ),
    limit: int = Query(100, ge=1, le=500),
):
    """Unified timeline of all agent activities (meetings + messages + spawn events)."""
    events = _build_unified_timeline(agent=agent, event_type=type, limit=limit)
    return {"events": events, "total": len(events)}


@router.get("/timeline/stream")
async def timeline_stream(
    agent: Optional[str] = Query(None, description="Filter by agent name"),
    type: Optional[str] = Query(None, description="Filter event type"),
):
    """SSE stream for real-time timeline updates.

    Polls for new events every 2 seconds and sends them as SSE.
    Also listens to the ActivityStreamManager for spawn events.
    """

    async def event_generator():
        last_ts = time.time()
        yield f"data: {json.dumps({'type': 'connected', 'timestamp': last_ts})}\n\n"

        while True:
            await asyncio.sleep(2)

            # Check for new events since last_ts
            events = _build_unified_timeline(agent=agent, event_type=type, limit=50)
            new_events = [e for e in events if e.get("timestamp", 0) > last_ts]

            if new_events:
                last_ts = max(e.get("timestamp", 0) for e in new_events)
                for event in reversed(new_events):  # oldest first
                    yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/meetings")
async def list_meetings():
    """List all meetings with metadata (config + state + transcript summary)."""
    meetings = _load_meetings()

    # Return meetings without full transcript (summary only)
    result = []
    for m in meetings:
        transcript = m.pop("transcript", [])
        m["turn_count"] = len(transcript)
        m["round_count"] = max(
            (e.get("round", 0) for e in transcript), default=0
        )
        m["participant_count"] = len(m.get("participants", []))
        # Current speaker
        participants = m.get("participants", [])
        current_turn = m.get("current_turn", 0)
        if not m.get("ended") and current_turn < len(participants):
            m["current_speaker"] = participants[current_turn]
        else:
            m["current_speaker"] = None
        # Include last message as preview
        if transcript:
            last = transcript[-1]
            m["last_message"] = {
                "agent": last.get("agent", ""),
                "content": (
                    last.get("message", "")[:100]
                    + ("..." if len(last.get("message", "")) > 100 else "")
                ),
                "timestamp": last.get("timestamp", ""),
            }
        result.append(m)

    # Sort by created_at desc
    result.sort(key=lambda m: m.get("created_at", ""), reverse=True)
    return {"meetings": result, "total": len(result)}


@router.get("/meetings/{meeting_id}/transcript")
async def get_meeting_transcript(meeting_id: str):
    """Get full transcript for a specific meeting."""
    for meeting in _load_meetings():
        if meeting.get("meeting_id") == meeting_id:
            return meeting

    return {"error": "Meeting not found", "meeting_id": meeting_id}


@router.get("/meetings/{meeting_id}/stream")
async def meeting_transcript_stream(meeting_id: str):
    """SSE stream for a specific meeting's transcript.

    **Event-driven** — no polling!  Uses ``MeetingEventManager`` queues.
    Falls back to initial state load, then waits for push events.
    """

    async def event_generator():
        # 1. Send initial state snapshot
        meeting = None
        for m in _load_meetings():
            if m.get("meeting_id") == meeting_id:
                meeting = m
                break

        connected_event = {
            "type": "connected",
            "meeting_id": meeting_id,
        }
        yield f"data: {json.dumps(connected_event)}\n\n"

        # Send existing transcript entries
        if meeting:
            for entry in meeting.get("transcript", []):
                event = {
                    "type": "transcript_entry",
                    "meeting_id": meeting_id,
                    "data": {"entry": entry},
                    "timestamp": _parse_timestamp(entry.get("timestamp", "")),
                }
                yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"

            # Send current state
            state_event = {
                "type": "state_changed",
                "meeting_id": meeting_id,
                "data": {
                    "state": {
                        "ended": meeting.get("ended", False),
                        "started": meeting.get("started", False),
                        "outcome": meeting.get("outcome"),
                        "current_round": meeting.get("current_round"),
                        "current_turn": meeting.get("current_turn", 0),
                        "joined": meeting.get("joined", []),
                        "participants": meeting.get("participants", []),
                    }
                },
            }
            yield f"data: {json.dumps(state_event)}\n\n"

        # 2. Subscribe to real-time events via queue
        q = meeting_event_manager.create_meeting_queue(meeting_id)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30)
                    yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"

                    # Stop streaming when meeting ends
                    if event.get("type") == "meeting_ended":
                        break
                except asyncio.TimeoutError:
                    # Send heartbeat to keep connection alive
                    yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
        finally:
            meeting_event_manager.remove_meeting_queue(meeting_id, q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/meetings/stream")
async def meetings_list_stream():
    """SSE stream for the global meeting list.

    **Event-driven** — pushes meeting lifecycle events (created, ended,
    state_changed) to connected dashboard clients in real time.
    """

    async def event_generator():
        yield f"data: {json.dumps({'type': 'connected'})}\n\n"

        q = meeting_event_manager.create_global_queue()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=30)
                    yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
        finally:
            meeting_event_manager.remove_global_queue(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/messages")
async def list_messages(
    agent: Optional[str] = Query(None, description="Filter by sender agent"),
    limit: int = Query(50, ge=1, le=200),
):
    """List inbox messages across all agents."""
    messages = _load_inbox_messages()

    if agent:
        messages = [
            m
            for m in messages
            if agent.lower() in m.get("from_name", "").lower()
            or agent.lower() in m.get("to_name", "").lower()
        ]

    # Sort by timestamp desc
    messages.sort(key=lambda m: m.get("timestamp", 0), reverse=True)
    return {"messages": messages[:limit], "total": len(messages)}
