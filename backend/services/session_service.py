"""
Session Service - Wraps Fast Agent's native SessionManager.
Single-user mode — no user isolation needed.
"""
import asyncio
import json
import logging
from pathlib import Path
from typing import List, Dict, Optional

from fast_agent.session import get_session_manager, SessionManager, Session

logger = logging.getLogger(__name__)

JARVIS_AGENT_NAME = "Jarvis"

# Key under session.metadata that records the agent the user is conversing
# with. Set on first ``resume_and_send`` for a new session so subsequent
# list/history calls can route to the right ``history_{agent}.json`` without
# the caller having to remember. Not overwritten on later sends — if the user
# switches agents mid-session the *primary* stays put and per-request
# agent_name overrides handle the switch.
PRIMARY_AGENT_META_KEY = "primary_agent"


def _build_send_payload(message: str, files_data: Optional[List[Dict]] = None):
    """Build the message payload for agent_app.send().
    
    Returns a plain string when no files, or PromptMessageExtended for multimodal.
    Uses the same pattern as inject.py's _inject_via_generate.
    """
    if not files_data:
        return message

    from fast_agent.types import PromptMessageExtended
    from mcp.types import TextContent, ImageContent, EmbeddedResource, BlobResourceContents

    content_parts = []
    
    # Add text content if present
    if message:
        content_parts.append(TextContent(type="text", text=message))
    
    # Add file content (images as ImageContent, audio/other as EmbeddedResource)
    for f in files_data:
        ct = f["content_type"]
        if ct.startswith("image/"):
            content_parts.append(ImageContent(
                type="image",
                data=f["data_b64"],
                mimeType=ct,
            ))
        else:
            # Audio or other binary → EmbeddedResource
            content_parts.append(EmbeddedResource(
                type="resource",
                resource=BlobResourceContents(
                    uri=f"file:///{f['filename']}",
                    mimeType=ct,
                    blob=f["data_b64"],
                ),
            ))

    if not content_parts:
        return message

    return PromptMessageExtended(
        role="user",
        content=content_parts,
    )


def _extract_display_messages(history_path: Path) -> List[Dict]:
    """
    Read MCP history JSON and extract messages suitable for UI display.

    Filters:
    - User messages: role=="user", has text content, no tool_results
    - Assistant messages: role=="assistant", stop_reason=="endTurn", has text content
    - Skips: templates, tool calls, tool results
    """
    try:
        with open(history_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

    messages = data.get("messages", [])
    display = []

    for msg in messages:
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue

        # Skip template messages
        if msg.get("is_template"):
            continue

        # Skip tool result messages (user role with tool_results)
        if role == "user" and msg.get("tool_results"):
            continue

        # For assistant: only show final responses (endTurn), not intermediate tool calls
        if role == "assistant" and msg.get("stop_reason") != "endTurn":
            continue

        # Extract text content
        content_parts = msg.get("content", [])
        text_parts = []
        for part in content_parts:
            if isinstance(part, dict) and part.get("type") == "text":
                text_parts.append(part.get("text", ""))

        text = "\n".join(text_parts).strip()
        if not text:
            continue

        entry = {
            "role": role,
            "content": text,
        }
        # Recall blocks carry per-memory lane provenance (fts/dense/graph) in a
        # channel — expose it (ordered, one list per recalled line) so the chat
        # "memories used" chip can show which lane surfaced each memory. Durable
        # (persisted with the block) → survives reload, unlike a live SSE.
        lanes = _recall_lanes_from_channels(msg)
        if lanes is not None:
            entry["recall_lanes"] = lanes
        scores = _recall_scores_from_channels(msg)
        if scores is not None:
            entry["recall_scores"] = scores
        display.append(entry)

    return display


def _recall_lanes_from_channels(msg: Dict) -> Optional[List[List[str]]]:
    """Read the ``jarvis:recall_lanes`` channel off a raw history message. Returns
    one lane-list per recalled memory (same order as the rendered ``- `` lines),
    or ``None`` if this isn't a recall block / predates the feature."""
    from services.memory.retrieval_hook import RECALL_LANES_CHANNEL
    entries = (msg.get("channels") or {}).get(RECALL_LANES_CHANNEL)
    if not entries:
        return None
    out: List[List[str]] = []
    for c in entries:
        text = c.get("text", "") if isinstance(c, dict) else ""
        out.append([lane for lane in text.split(",") if lane])
    return out


def _recall_scores_from_channels(msg: Dict) -> Optional[List[Dict]]:
    """Read the ``jarvis:recall_scores`` channel — one
    ``{rrf, rerank, conf, authority}`` per recalled line (RAW values; see
    RECALL_SCORES_CHANNEL). ``None`` if absent (not a recall block, or recalled
    before this shipped). Back-compat: older blocks stored a 3-field
    ``rel|conf|authority`` where ``rel`` was a single conflated score (``final or
    rrf`` — could be either). We can't know which, so keep it as a NEUTRAL ``rel``
    and let the chip render "score …" rather than mislabel it rrf or rerank."""
    from services.memory.retrieval_hook import RECALL_SCORES_CHANNEL
    entries = (msg.get("channels") or {}).get(RECALL_SCORES_CHANNEL)
    if not entries:
        return None
    out: List[Dict] = []
    for c in entries:
        text = c.get("text", "") if isinstance(c, dict) else ""
        parts = text.split("|")
        if len(parts) >= 4:                       # rrf|rerank|conf|authority
            out.append({
                "rrf": _safe_float(parts[0]),
                "rerank": _safe_float(parts[1]),
                "conf": _safe_float(parts[2]),
                "authority": parts[3],
            })
        else:                                     # legacy rel|conf|authority
            out.append({
                "rrf": None,
                "rerank": None,
                "rel": _safe_float(parts[0] if parts else ""),   # neutral → "score"
                "conf": _safe_float(parts[1] if len(parts) > 1 else ""),
                "authority": parts[2] if len(parts) > 2 else "",
            })
    return out


def _safe_float(s: str) -> Optional[float]:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _resolve_primary_agent(session) -> Optional[str]:
    """Return the agent that owns a session, from metadata only.

    The authoritative source is ``metadata[PRIMARY_AGENT_META_KEY]``, written
    on the first ``resume_and_send`` for the session and stamped onto legacy
    sessions by ``_migrate_legacy_primary_agent`` at service startup.

    Returns ``None`` when the key is missing — the caller must treat this as
    a real data issue (unmigrated or corrupt session), **not** infer the
    agent from history file mtimes. A fallback used to live here; it was
    removed because silent guessing made misrouting bugs untraceable. If you
    see the warning below in logs, investigate the session directory
    directly rather than adding another fallback.
    """
    if session is None:
        return None

    info = getattr(session, "info", None)
    meta = getattr(info, "metadata", None) or {}

    primary = meta.get(PRIMARY_AGENT_META_KEY)
    if isinstance(primary, str) and primary:
        return primary

    session_id = getattr(info, "name", None) or "<unknown>"
    hist_keys = list((meta.get("last_history_by_agent") or {}).keys())
    logger.warning(
        "[SESSION] Primary agent missing for session=%s; history_keys=%s. "
        "Session will be hidden until an agent stamps metadata. Run the "
        "startup migration or send a message to repair.",
        session_id,
        hist_keys,
    )
    return None


def _migrate_legacy_primary_agent(manager: SessionManager) -> int:
    """One-shot migration: stamp PRIMARY_AGENT_META_KEY on legacy sessions.

    For every session missing the primary-agent field, pick the entry in
    ``last_history_by_agent`` whose ``history_{agent}.json`` has the newest
    mtime and persist it. This runs once at SessionService construction and
    is idempotent — sessions already carrying the key are skipped.

    Every migrated session is logged with its chosen agent and the source
    mtime so misrouting can be traced back to the migration decision. Sessions
    with an empty history map can't be migrated and are logged separately.

    Returns the number of sessions stamped.
    """
    migrated = 0
    for info in manager.list_sessions():
        if info.metadata.get(PRIMARY_AGENT_META_KEY):
            continue

        session = manager.get_session(info.name)
        if session is None:
            continue

        hist_map = info.metadata.get("last_history_by_agent") or {}
        if not isinstance(hist_map, dict) or not hist_map:
            logger.info(
                "[SESSION][migrate] Skipping session=%s — no history to infer primary_agent from.",
                info.name,
            )
            continue

        best_agent: Optional[str] = None
        best_mtime: float = -1.0
        session_dir = getattr(session, "directory", None)
        for agent, filename in hist_map.items():
            if session_dir is None:
                continue
            try:
                mtime = (session_dir / filename).stat().st_mtime
            except (OSError, TypeError):
                continue
            if mtime > best_mtime:
                best_mtime = mtime
                best_agent = agent

        if not best_agent:
            logger.warning(
                "[SESSION][migrate] Session=%s has history_map=%s but no "
                "readable history files — leaving unstamped.",
                info.name,
                list(hist_map.keys()),
            )
            continue

        session.info.metadata[PRIMARY_AGENT_META_KEY] = best_agent
        try:
            session._save_metadata()
        except Exception as exc:
            logger.error(
                "[SESSION][migrate] Failed to persist primary_agent for session=%s: %s",
                info.name,
                exc,
            )
            continue

        migrated += 1
        logger.info(
            "[SESSION][migrate] session=%s stamped primary_agent=%s (mtime=%.0f, "
            "candidates=%s)",
            info.name,
            best_agent,
            best_mtime,
            list(hist_map.keys()),
        )

    if migrated:
        logger.info("[SESSION][migrate] Completed — stamped %d legacy session(s).", migrated)
    return migrated


class SessionService:
    """
    Manages chat sessions using Fast Agent's native SessionManager.
    Single-user mode — no user isolation filtering.
    """

    def __init__(self):
        self._manager: SessionManager = get_session_manager()
        self._agent_lock = asyncio.Lock()
        # Backfill primary_agent onto legacy sessions exactly once. This is
        # idempotent: subsequent restarts scan fast and skip already-stamped
        # sessions. Failures are logged but non-fatal — a session that can't
        # be migrated will simply be hidden from list_sessions until its next
        # send, which is the explicit, debuggable behavior we want.
        try:
            _migrate_legacy_primary_agent(self._manager)
        except Exception as exc:
            logger.error("[SESSION] Primary-agent migration failed: %s", exc, exc_info=True)

    def create_session(self, title: str = "New Chat") -> Dict:
        """Create a new session."""
        session = self._manager.create_session(
            metadata={"title": title}
        )
        return {"id": session.info.name, "title": title}

    def ensure_session(self, session_id: Optional[str]) -> str:
        """Resolve ``session_id`` to a real backend session id, creating one
        if necessary.

        The chat route creates SSE progress hooks before calling
        ``resume_and_send``, and those hooks persist tool activities tagged
        with ``session_id``. Without pre-resolution the hook captures either
        ``None`` (very new conversation) or a client-generated UUID that
        doesn't match the backend's actual session id — both cases leave
        tool activity rows orphaned (``session_id=None``) and invisible to
        the history API.

        This helper returns the id the hook should use: the existing session
        if ``session_id`` is known, otherwise a freshly created one.
        """
        if session_id:
            session = self._manager.get_session(session_id)
            if session is not None:
                return session_id
        session = self._manager.create_session(metadata={"title": "New Chat"})
        return session.info.name

    def list_sessions(
        self,
        agent_name: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> Dict:
        """List chat sessions, one row per user-facing conversation.

        A session is listed when any agent has saved history to it — we used
        to hard-filter for ``Jarvis`` only, which hid conversations started
        directly with sub-agents (IoT, Music, …). The primary agent is
        resolved via metadata so the UI can route follow-up sends and history
        reads to the right ``history_{agent}.json``.

        Sessions with no saved history yet (just-created, never sent) are
        skipped — they have no content to display and no agent to attribute.

        Args:
            agent_name: when given, only sessions whose primary agent matches
                are returned (the sidebar is scoped to the active agent).
            limit/offset: page window over the filtered+sorted set. ``limit``
                of ``None`` returns everything (kept for internal callers).

        Returns a paginated envelope ``{"items": [...], "total": N}`` where
        ``total`` is the count AFTER agent filtering but BEFORE the page slice,
        so the UI knows whether more pages exist.

        Scales with session count: cheap metadata (id/title/agent/timestamps)
        is collected for every session, but the expensive per-session history
        read for ``message_count`` is done ONLY for the rows on the requested
        page — not the whole corpus.
        """
        rows = []
        for s in self._manager.list_sessions():
            session = self._manager.get_session(s.name)
            if not session:
                continue

            primary = _resolve_primary_agent(session)
            if not primary:
                # No agent has written history yet → nothing to show.
                continue
            if agent_name and primary != agent_name:
                continue

            rows.append({
                "id": s.name,
                "title": s.metadata.get("title") or s.metadata.get("first_user_preview") or "New Chat",
                "agent_name": primary,
                "created_at": s.created_at.timestamp(),
                "updated_at": s.last_activity.timestamp(),
                "_session": session,  # carried for the deferred message_count
            })

        # Newest first — matches the frontend's updatedAt-desc ordering so page
        # boundaries are stable as the user scrolls.
        rows.sort(key=lambda r: r["updated_at"], reverse=True)
        total = len(rows)

        page = rows[offset:] if limit is None else rows[offset:offset + limit]

        items = []
        for r in page:
            session = r.pop("_session")
            message_count = 0
            history_path = session.latest_history_path(r["agent_name"])
            if history_path and history_path.exists():
                message_count = len(_extract_display_messages(history_path))
            r["message_count"] = message_count
            items.append(r)

        return {"items": items, "total": total}

    def delete_session(self, session_id: str) -> bool:
        """Delete a session."""
        session = self._manager.get_session(session_id)
        if not session:
            return False
        return self._manager.delete_session(session_id)

    def get_display_history(
        self,
        session_id: str,
        agent_name: Optional[str] = None,
    ) -> List[Dict]:
        """Get display-friendly message history for a session.

        Includes tool call/result metadata from the agent_activities table so
        the frontend can display tool usage after page reload.

        ``agent_name`` is optional:
        - If provided, we read ``history_{agent_name}.json`` directly.
        - If omitted, the session's primary agent is resolved from metadata.

        Returns ``[]`` for unknown sessions, missing agents, or agents that
        never wrote history.
        """
        if not session_id:
            return []

        session = self._manager.get_session(session_id)
        if not session:
            return []

        resolved_agent = agent_name or _resolve_primary_agent(session)
        if not resolved_agent:
            return []

        history_path = session.latest_history_path(resolved_agent)
        if not history_path or not history_path.exists():
            return []

        messages = _extract_display_messages(history_path)

        # Enrich with tool call data from activities table, filtered to the
        # agent we're rendering — otherwise a Jarvis view would get Music's
        # tool bubbles glued to its last turn (and vice versa). ``ordered_run_ids``
        # gives the chronological turn ordering so tools can be placed on the
        # assistant message that produced them, not lumped onto the last one.
        try:
            tool_activities = self._get_tool_activities(session_id, resolved_agent)
            ordered_run_ids = self._get_ordered_run_ids(session_id, resolved_agent)
            if tool_activities:
                messages = self._merge_tool_activities(
                    messages, tool_activities, ordered_run_ids,
                )
        except Exception as e:
            logger.warning(f"[SESSION] Failed to enrich history with activities: {e}")

        return messages

    def _get_tool_activities(
        self,
        session_id: str,
        agent_name: Optional[str] = None,
    ) -> List[Dict]:
        """Fetch tool_call/tool_result events for a session.

        When ``agent_name`` is provided, only activities attributed to that
        agent are returned. Legacy rows (pre-agent_name) have ``NULL`` in the
        column and are included unconditionally so old conversations still
        render tool bubbles — assume they belong to the primary agent.
        """
        try:
            import json as _json
            from core.database import get_db_session, AgentActivity
            from sqlalchemy import or_

            db = get_db_session()
            try:
                query = db.query(AgentActivity).filter(
                    AgentActivity.session_id == session_id,
                    AgentActivity.event_type.in_(["tool_call", "tool_result"]),
                )
                if agent_name:
                    query = query.filter(
                        or_(
                            AgentActivity.agent_name == agent_name,
                            AgentActivity.agent_name.is_(None),
                        )
                    )
                rows = query.order_by(AgentActivity.created_at.asc()).all()

                result = []
                for row in rows:
                    data = {}
                    if row.data_json:
                        try:
                            data = _json.loads(row.data_json)
                        except _json.JSONDecodeError:
                            pass
                    result.append({
                        "event_type": row.event_type,
                        "run_id": row.run_id,
                        "agent_name": row.agent_name,
                        "data": data,
                        "created_at": row.created_at,
                    })
                return result
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"[SESSION] Failed to query tool activities: {e}")
            return []
    
    def _get_ordered_run_ids(
        self,
        session_id: str,
        agent_name: Optional[str] = None,
    ) -> List[str]:
        """Return distinct run_ids for session+agent in turn order.

        Every /chat-stream request gets a unique ``request_id`` that is
        persisted as ``run_id`` on every activity row produced by that turn
        (``started``, ``tool_call``, ``tool_result``, ``response``, ``idle``).
        Ordering by the earliest ``created_at`` per run_id reproduces the
        order turns happened in, which is the same order assistant messages
        appear in the history file.

        This is separate from ``_get_tool_activities`` on purpose: a turn
        without tool calls still has ``started``/``response`` rows, so we
        must scan *all* event types to get a complete turn list — otherwise
        the run_id→assistant-message alignment in ``_merge_tool_activities``
        skips turns-without-tools and misplaces later turns' tools.
        """
        try:
            from core.database import get_db_session, AgentActivity
            from sqlalchemy import func, or_

            db = get_db_session()
            try:
                query = db.query(
                    AgentActivity.run_id,
                    func.min(AgentActivity.created_at).label("first_ts"),
                ).filter(
                    AgentActivity.session_id == session_id,
                    AgentActivity.run_id.isnot(None),
                )
                if agent_name:
                    query = query.filter(
                        or_(
                            AgentActivity.agent_name == agent_name,
                            AgentActivity.agent_name.is_(None),
                        )
                    )
                rows = query.group_by(AgentActivity.run_id).order_by("first_ts").all()
                return [r[0] for r in rows if r[0]]
            finally:
                db.close()
        except Exception as e:
            logger.warning(f"[SESSION] Failed to query ordered run_ids: {e}")
            return []

    def _merge_tool_activities(
        self,
        messages: List[Dict],
        activities: List[Dict],
        ordered_run_ids: List[str],
    ) -> List[Dict]:
        """Attach tool call/result bubbles to the assistant messages that
        produced them, aligning by ``run_id``.

        Each /chat-stream turn persists tool_call/tool_result rows sharing one
        ``run_id`` (= request_id). We group activities by run_id, pair
        tool_call→tool_result within each group, then right-anchor the
        ``ordered_run_ids`` list to the ordered assistant messages: the
        newest run_id → newest assistant message, then walk backwards. The
        previous implementation glued **all** activities onto the last
        assistant message, which on reload replaced the current-turn's
        in-memory tool list with a mix of tools from every past turn.

        Right-anchored alignment handles the two realistic skews:
        - ``ordered_run_ids`` can exceed ``assistant_idxs`` when a turn
          errored out (``started`` persisted, no final assistant message).
          The oldest run_ids drop off the front.
        - ``assistant_idxs`` can exceed ``ordered_run_ids`` for sessions
          that pre-date run_id tracking; those older messages just render
          with no tool bubble, which is correct (no data exists).

        Activities with ``run_id=None`` are ignored — we can't place them
        reliably, and speculative placement is worse than no bubble.
        """
        if not activities:
            return messages

        # Group tool events by run_id; skip rows that predate run_id tracking.
        by_run: Dict[str, List[Dict]] = {}
        for act in activities:
            rid = act.get("run_id")
            if not rid:
                continue
            by_run.setdefault(rid, []).append(act)

        def _pair(acts: List[Dict]) -> List[Dict]:
            out: List[Dict] = []
            pending = None
            for a in acts:
                et = a["event_type"]
                if et == "tool_call":
                    pending = a
                elif et == "tool_result" and pending is not None:
                    result_data = a["data"] or {}
                    # Prefer the RESULT event's tool list — each entry carries its
                    # OWN result_preview after the per-tool fix. Fall back to the
                    # call event's tools + the batch-level preview for rows persisted
                    # before the fix (where every tool shared the first result).
                    tools_info = result_data.get("tools") or pending["data"].get("tools", []) or []
                    batch_preview = result_data.get("result_preview")
                    for tool in tools_info:
                        if isinstance(tool, dict):
                            tool_name = tool.get("name", "unknown")
                            tool_args = tool.get("args", {}) or {}
                            preview = tool.get("result_preview")
                            if preview is None:
                                preview = batch_preview
                        else:
                            tool_name = str(tool)
                            tool_args = {}
                            preview = batch_preview
                        out.append({
                            "tool": tool_name,
                            "args": tool_args,
                            "status": "done",
                            "duration_ms": result_data.get("duration_ms"),
                            "result_preview": preview,
                        })
                    pending = None
            return out

        per_run_bubbles = {rid: _pair(acts) for rid, acts in by_run.items()}

        assistant_idxs = [i for i, m in enumerate(messages) if m.get("role") == "assistant"]
        if not assistant_idxs or not ordered_run_ids:
            return messages

        # Right-anchor: last run_id → last assistant, walk backwards.
        n = min(len(ordered_run_ids), len(assistant_idxs))
        for offset in range(1, n + 1):
            rid = ordered_run_ids[-offset]
            bubbles = per_run_bubbles.get(rid)
            if not bubbles:
                continue
            messages[assistant_idxs[-offset]]["tool_calls"] = bubbles

        return messages

    async def resume_and_send(
        self,
        agent_app,
        message: str,
        session_id: Optional[str],
        files_data: Optional[List[Dict]] = None,
        agent_name: Optional[str] = None,
    ) -> tuple:
        """
        Core chat flow: resume session → send message → save session.
        
        Args:
            agent_app: The fast-agent AgentApp instance
            message: User text message
            session_id: Existing session ID or None for new
            files_data: Optional list of file dicts [{filename, content_type, data_b64}]
                        for multimodal content (images, audio)
        
        Returns (response_text, session_id).
        """
        import time as _time
        async with self._agent_lock:
            # Resolve or create session
            session = None
            if session_id:
                session_obj = self._manager.get_session(session_id)
                if session_obj:
                    session = self._manager.load_session(session_id)
                    logger.debug(f"[SESSION] Resumed session={session_id}")

            if not session:
                # Create new session
                session = self._manager.create_session(
                    metadata={"title": "New Chat"}
                )
                session_id = session.info.name
                logger.debug(f"[SESSION] Created new session={session_id}")

            # Resolve target agent name (fallback to Jarvis)
            target_agent_name = agent_name or JARVIS_AGENT_NAME
            if target_agent_name not in agent_app._agents:
                logger.warning(f"[SESSION] Agent '{target_agent_name}' not found, falling back to Jarvis")
                target_agent_name = JARVIS_AGENT_NAME
            
            # Get target agent and restore context
            target_agent = agent_app._agents[target_agent_name]
            history_path = session.latest_history_path(target_agent_name)
            if history_path and history_path.exists():
                from fast_agent.mcp.prompts.prompt_load import load_history_into_agent
                load_history_into_agent(target_agent, history_path)
            else:
                # New session: clear agent history
                target_agent.clear(clear_prompts=True)

            # Build the message payload (multimodal or text-only)
            send_payload = _build_send_payload(message, files_data)

            _msg_preview = message[:60] + "..." if len(message) > 60 else message
            _file_count = len(files_data) if files_data else 0
            logger.debug(f"[SESSION] Sending message len={len(message)} files={_file_count} session={session_id}: \"{_msg_preview}\"")

            # Auto-resume Jarvis if paused — prevent stale pause from blocking chat
            try:
                from services.pause_manager import pause_manager
                if pause_manager.is_paused(JARVIS_AGENT_NAME):
                    pause_manager.resume(JARVIS_AGENT_NAME)
                    logger.info("[SESSION] Auto-resumed Jarvis (was paused from previous session)")
            except Exception:
                pass

            _t0 = _time.time()
            response = await agent_app.send(send_payload, agent_name=target_agent_name)
            _duration = _time.time() - _t0
            _resp_len = len(str(response)) if response else 0
            logger.debug(f"[SESSION] Response received len={_resp_len} duration={_duration:.1f}s session={session_id} agent={target_agent_name}")

            # Cancellation rollback: fast-agent's OpenAI provider catches
            # ``asyncio.CancelledError`` from an interrupted LLM call and
            # returns an empty Prompt instead of re-raising. Without the
            # check below, ``agent.message_history`` (which fast-agent
            # appends to *before* invoking the LLM) would keep the user
            # message + the empty assistant reply, and ``save_history``
            # would persist that phantom turn. On reload the user would
            # see their cancelled utterance plus a blank Jarvis bubble —
            # a "ghost turn" the cancel flow was supposed to erase.
            #
            # ``Task.cancelling()`` returns >0 if cancel() was ever called
            # on this task even when the inner await absorbed it. Python
            # 3.11+; we run on 3.13.
            try:
                cur = asyncio.current_task()
                cancelling = cur is not None and cur.cancelling() > 0
            except Exception:
                cancelling = False
            if cancelling:
                # Drop the half-turn from the agent's in-memory history
                # so the next call (which loads from disk) starts clean.
                # Fast-agent records the user message + assistant response
                # on each send — pop both so neither leaks into context.
                try:
                    history = getattr(target_agent, "message_history", None)
                    if isinstance(history, list) and len(history) >= 2:
                        history.pop()  # the empty assistant message
                        history.pop()  # the just-appended user message
                except Exception:
                    logger.debug("[SESSION] cancellation rollback: history pop failed", exc_info=True)
                logger.info(
                    f"[SESSION] turn cancelled for session={session_id} — "
                    f"rolled back phantom turn, still stamping primary + saving "
                    f"so the conversation stays visible in the list"
                )
                # IMPORTANT: do NOT return early here. We still fall through to
                # stamp the primary agent + save_history below. Returning early
                # (the previous behaviour) skipped the primary-agent stamp, so a
                # session whose FIRST turn was cancelled (e.g. user starts a long
                # crawl turn then navigates away / hits Stop) was left with no
                # primary_agent metadata → _resolve_primary_agent() returns None
                # → list_sessions HIDES it → "the conversation disappeared" bug.
                # History was already rolled back above, so the save below
                # persists clean state (no phantom turn) + the metadata.

            # Stamp the primary agent on first send so list_sessions /
            # get_display_history can route follow-up reads without the caller
            # having to pass agent_name each time. We don't overwrite on later
            # sends — if the user switches agents mid-session the primary
            # stays put; routing to the other agent is driven by the explicit
            # agent_name parameter on each request.
            if not session.info.metadata.get(PRIMARY_AGENT_META_KEY):
                session.info.metadata[PRIMARY_AGENT_META_KEY] = target_agent_name

            # Save session — this also persists the metadata mutation above.
            await session.save_history(target_agent)

            # Auto-set title from first user message
            if not session.info.metadata.get("title") or session.info.metadata.get("title") == "New Chat":
                title = message[:30] + "..." if len(message) > 30 else message
                session.set_title(title)

            return response, session_id
