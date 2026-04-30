"""
Inject Prompt API Route.
POST /api/agents/{agent_name}/inject — inject a prompt into any agent.

Accepts both JSON body and multipart form data (for image/audio attachments).

Three code paths based on agent state:
  Path A (Running/Pending/Paused): MessageBus — queue message inline, no flow disruption.
  Path B (Idle/Completed/Error):   Resume with context from DB — agent wakes up,
         processes inject, then continues team flow via _check_and_resume_on_inbox.
  Path C (Static/Predefined):      agent.generate() — maintains conversation history.
"""
import base64
import logging
import time
import uuid

from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel

from core.auth import verify_api_key
from agent import fast
import services.shared_state as state
from services.activity_stream import activity_stream_manager
from services.sse_progress import create_progress_hooks, merge_hooks

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["inject"])


class InjectResponse(BaseModel):
    status: str  # "queued" | "resumed" | "responded"
    agent_name: str
    path: str  # "message_bus" | "resume_with_context" | "generate"
    response: str | None = None


# ── Statuses where agent process is alive and can read MessageBus ─────────
_PROCESS_ALIVE_STATUSES = {"running", "pending", "paused"}

# ── Statuses where agent process is dead and needs resume ─────────────────
_RESUMABLE_STATUSES = {"idle", "completed", "error", "cancelled", "timeout"}


@router.post("/{agent_name}/inject", dependencies=[Depends(verify_api_key)])
async def inject_prompt(
    agent_name: str,
    request: Request,
):
    """Inject a prompt into an agent (any state).

    Agents are nodes in the team tree. Inject provides additional context
    or influences behavior WITHOUT disrupting the orchestration flow.

    Accepts:
    - JSON body: {"message": "...", "priority": "normal"}
    - Multipart form: message field + optional files (images/audio)
    """
    # ── Parse request body ────────────────────────────────────────────────
    message, files_data = await _parse_request(request)

    if not message and not files_data:
        raise HTTPException(status_code=400, detail="Message or files required")

    # ── Broadcast inject event to activity stream ─────────────────────────
    attachment_desc = ""
    if files_data:
        names = [f["filename"] for f in files_data]
        attachment_desc = f" [+{len(files_data)} file(s): {', '.join(names)}]"

    activity_stream_manager.broadcast({
        "event_type": "inject",
        "agent_name": agent_name,
        "message": f"Prompt injected: {message[:80]}{'…' if len(message) > 80 else ''}{attachment_desc}",
        "timestamp": time.time(),
        "data": {"source": "dashboard", "has_files": bool(files_data)},
    })

    # ── Determine agent state and route accordingly ───────────────────────
    if state.registry_db:
        try:
            records = state.registry_db.find_by_name(agent_name)

            if records:
                # Path A: Process alive → MessageBus (inline delivery, no flow disruption)
                alive = next(
                    (r for r in records if r.get("status") in _PROCESS_ALIVE_STATUSES),
                    None,
                )
                if alive:
                    return await _inject_via_message_bus(agent_name, message, alive)

                # Path B: Process dead → Resume with context from DB
                latest = records[0]  # sorted by started_at DESC
                if latest.get("original_config"):
                    # Broadcast "started" so Team Monitor reflects active state
                    activity_stream_manager.broadcast({
                        "event_type": "started",
                        "agent_name": agent_name,
                        "message": f"Resuming for inject: {message[:60]}{'…' if len(message) > 60 else ''}",
                        "timestamp": time.time(),
                    })
                    return await _inject_via_resume(agent_name, message, latest)

                # No original_config → can't resume
                _broadcast_error_and_idle(agent_name, "Agent has no saved config. Cannot resume.")
                raise HTTPException(
                    status_code=409,
                    detail=f"Agent '{agent_name}' has no saved config. Cannot resume.",
                )

        except HTTPException:
            raise
        except Exception as e:
            logger.warning("[INJECT] Registry lookup failed for %s: %s", agent_name, e)

    # ── Path C: Static agent via generate() ───────────────────────────────
    agent_data = fast.agents.get(agent_name)
    if not agent_data:
        _broadcast_error_and_idle(agent_name, f"Agent '{agent_name}' not found")
        raise HTTPException(status_code=404, detail=f"Agent '{agent_name}' not found")

    # Broadcast "started" for static agents
    activity_stream_manager.broadcast({
        "event_type": "started",
        "agent_name": agent_name,
        "message": f"Processing inject: {message[:60]}{'…' if len(message) > 60 else ''}",
        "timestamp": time.time(),
    })

    return await _inject_via_generate(agent_name, message, files_data)


# ── Request parsing ───────────────────────────────────────────────────────


async def _parse_request(request: Request) -> tuple[str, list[dict]]:
    """Parse inject request — supports JSON and multipart form data."""
    content_type = request.headers.get("content-type", "")
    files_data: list[dict] = []

    if "multipart/form-data" in content_type:
        form = await request.form()
        message = form.get("message", "")
        message = message.strip() if isinstance(message, str) else ""

        for key in form:
            if key == "files":
                upload_files = form.getlist("files")
                for f in upload_files:
                    if hasattr(f, 'read'):
                        file_content = await f.read()
                        files_data.append({
                            "filename": f.filename or "unnamed",
                            "content_type": f.content_type or "application/octet-stream",
                            "data_b64": base64.standard_b64encode(file_content).decode("ascii"),
                            "size": len(file_content),
                        })
    else:
        body = await request.json()
        message = body.get("message", "").strip()

    return message, files_data


# ── Path A: MessageBus (process alive) ────────────────────────────────────


async def _inject_via_message_bus(
    agent_name: str, message: str, spawn_record: dict,
) -> InjectResponse:
    """Inject message into spawned agent via MessageBus.

    Agent process is alive — message is queued to inbox and processed
    inline without disrupting the team flow.
    """
    import os
    from pathlib import Path

    try:
        from fast_agent.spawn.message_bus import MessageBus

        # Determine messages directory from spawn record
        workspace = spawn_record.get("workspace")
        if workspace:
            messages_dir = Path(workspace) / ".runtime" / "state" / "messages"
        else:
            session_id = spawn_record.get("session_id", "")
            project_dir = os.environ.get("SPAWN_PROJECT_DIR", "")
            if not project_dir or not session_id:
                raise ValueError(
                    f"Cannot find messages dir for '{agent_name}': "
                    f"no workspace, SPAWN_PROJECT_DIR={project_dir!r}, session_id={session_id!r}"
                )
            messages_dir = Path(project_dir) / ".runtime" / "state" / "messages" / session_id

        if not messages_dir.exists():
            messages_dir.mkdir(parents=True, exist_ok=True)

        bus = MessageBus(messages_dir=str(messages_dir))
        bus.send(from_name="Dashboard", to_name=agent_name, content=message)

        logger.info("[INJECT] MessageBus: Dashboard → %s (queued)", agent_name)

        return InjectResponse(
            status="queued",
            agent_name=agent_name,
            path="message_bus",
            response=None,
        )
    except Exception as e:
        logger.error("[INJECT] MessageBus failed for %s: %s", agent_name, e, exc_info=True)
        _broadcast_error_and_idle(agent_name, f"MessageBus error: {str(e)[:100]}")
        raise HTTPException(status_code=500, detail=f"Failed to inject via MessageBus: {e}")


# ── Path B: Resume with DB context (process dead) ────────────────────────


async def _inject_via_resume(
    agent_name: str, message: str, spawn_record: dict,
) -> InjectResponse:
    """Resume non-running agent with context from DB + inject message.

    Agent wakes up with full conversation history, processes the inject,
    then checks inbox for team messages and continues orchestration flow.
    """
    try:
        from services.inject_resume import resume_with_inject

        result = await resume_with_inject(
            agent_name=agent_name,
            inject_message=message,
            spawn_record=spawn_record,
            bridge=state.spawn_bridge,
        )

        logger.info(
            "[INJECT] Resumed %s → run_id=%s",
            agent_name, result.get("run_id"),
        )

        return InjectResponse(
            status="resumed",
            agent_name=agent_name,
            path="resume_with_context",
            response=f"Agent resumed (run_id: {result['run_id']}). Processing inject message...",
        )
    except ValueError as e:
        _broadcast_error_and_idle(agent_name, str(e))
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        logger.error("[INJECT] Resume failed for %s: %s", agent_name, e, exc_info=True)
        _broadcast_error_and_idle(agent_name, f"Resume error: {str(e)[:100]}")
        raise HTTPException(status_code=500, detail=f"Failed to resume agent: {e}")


# ── Path C: Static agent via generate() ──────────────────────────────────


async def _inject_via_generate(
    agent_name: str, message: str, files_data: list[dict] | None = None,
) -> InjectResponse:
    """Inject message into static agent via agent.generate().

    Supports multimodal content (text + images/audio) via PromptMessageExtended.
    Attaches ToolRunnerHooks to ALL agents so tool_call, thinking, and sub-agent
    events are broadcast to the activity stream (Team Monitor).
    """
    try:
        from fast_agent.types import PromptMessageExtended
        from mcp.types import TextContent, ImageContent, EmbeddedResource, BlobResourceContents

        agent_app = state.agent_app
        if not agent_app:
            raise HTTPException(status_code=503, detail="Agent runtime not initialized")

        agent = getattr(agent_app, agent_name, None)
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_name}' not accessible on runtime")

        # Build content list for the injection message
        content_parts = []

        if message:
            content_parts.append(TextContent(type="text", text=message))

        if files_data:
            for f in files_data:
                ct = f["content_type"]
                if ct.startswith("image/"):
                    content_parts.append(ImageContent(
                        type="image",
                        data=f["data_b64"],
                        mimeType=ct,
                    ))
                else:
                    content_parts.append(EmbeddedResource(
                        type="resource",
                        resource=BlobResourceContents(
                            uri=f"file:///{f['filename']}",
                            mimeType=ct,
                            blob=f["data_b64"],
                        ),
                    ))

        if not content_parts:
            raise HTTPException(status_code=400, detail="No content to inject")

        inject_msg = PromptMessageExtended(
            role="user",
            content=content_parts,
        )

        # Attach ToolRunnerHooks to ALL agents (same pattern as chat.py)
        request_id = f"inject-{uuid.uuid4().hex[:8]}"
        original_hooks = {}
        progress_hooks = create_progress_hooks(request_id)

        from services.pause_manager import pause_manager

        for name, ag in agent_app._agents.items():
            original_hooks[name] = getattr(ag, 'tool_runner_hooks', None)
            existing = original_hooks[name]

            pause_hooks = pause_manager.create_pause_hooks(name)
            combined = merge_hooks(progress_hooks, pause_hooks)

            if existing:
                ag.tool_runner_hooks = merge_hooks(existing, combined)
            else:
                ag.tool_runner_hooks = combined

        try:
            result = await agent.generate(inject_msg)
            response_text = result.last_text() if hasattr(result, 'last_text') else str(result)
        finally:
            for name, ag in agent_app._agents.items():
                original = original_hooks.get(name)
                ag.tool_runner_hooks = original if original else None

        file_count = len(files_data) if files_data else 0
        logger.info(
            "[INJECT] generate(): %s → response=%d chars, files=%d",
            agent_name, len(response_text), file_count,
        )

        activity_stream_manager.broadcast({
            "event_type": "response",
            "agent_name": agent_name,
            "message": f"Inject response: {response_text[:100]}{'…' if len(response_text) > 100 else ''}",
            "full_message": response_text,
            "timestamp": time.time(),
        })

        return InjectResponse(
            status="responded",
            agent_name=agent_name,
            path="generate",
            response=response_text,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("[INJECT] generate() failed for %s: %s", agent_name, e, exc_info=True)
        _broadcast_error_and_idle(agent_name, f"Generate error: {str(e)[:100]}")
        raise HTTPException(status_code=500, detail=f"Failed to inject via generate(): {e}")


# ── Helpers ───────────────────────────────────────────────────────────────


def _broadcast_error_and_idle(agent_name: str, error_msg: str) -> None:
    """Broadcast error then idle so dashboard exits 'running' state."""
    activity_stream_manager.broadcast({
        "event_type": "error",
        "agent_name": agent_name,
        "message": f"Inject failed: {error_msg}",
        "timestamp": time.time(),
    })
    activity_stream_manager.broadcast({
        "event_type": "idle",
        "agent_name": agent_name,
        "message": f"💤 {agent_name} inject failed",
        "timestamp": time.time(),
    })
