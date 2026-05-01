"""Chat routes: /api/chat, /api/chat-stream, /api/chat-audio, /api/health, /api/background/status."""
import os
import re
import uuid
import json
import base64
import shutil
import asyncio
import logging
import time as _time

import speech_recognition as sr
from fastapi import APIRouter, Request, Depends, UploadFile, File
from pydantic import BaseModel

from core.auth import verify_api_key
from helpers.text_processing import process_agent_response, clean_text_for_tts
from helpers.tts_rules import prepend_tts_rules
from helpers.story_reader import handle_read_local, check_pending_read
from services.shared_state import (
    session_service, library_manager, tts_cache,
)
import services.shared_state as _state
from services.sse_progress import progress_manager, create_progress_hooks, merge_hooks, _persist_activity
from tools.story_server import get_chapter_content

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["chat"])


async def _transcribe_audio(audio_bytes: bytes, original_filename: str) -> str | None:
    """Transcribe audio bytes to text via ffmpeg + speech_recognition.
    
    Uses the same STT pipeline as /chat-audio endpoint.
    Returns transcribed text or None on failure.
    """
    import subprocess
    import tempfile
    import speech_recognition as sr
    
    ext = original_filename.rsplit(".", 1)[-1] if "." in original_filename else "webm"
    
    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as src:
        src.write(audio_bytes)
        src_path = src.name
    
    wav_path = src_path.rsplit(".", 1)[0] + ".wav"
    
    try:
        # Convert to WAV for speech_recognition
        result = subprocess.run(
            ["ffmpeg", "-i", src_path, "-ac", "1", "-ar", "16000", wav_path, "-y"],
            capture_output=True, timeout=15,
        )
        if result.returncode != 0:
            logger.warning("[STT] ffmpeg conversion failed: %s", result.stderr.decode()[:200])
            return None
        
        # Check audio volume
        import wave
        import audioop
        with wave.open(wav_path, 'rb') as wf:
            frames = wf.readframes(wf.getnframes())
            rms = audioop.rms(frames, wf.getsampwidth())
            if rms < 100:
                logger.warning("[STT] Audio too quiet (rms=%d)", rms)
                return None
        
        # Transcribe
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio_data = recognizer.record(source)
            try:
                text = recognizer.recognize_google(audio_data, language="vi-VN")
                logger.info('[STT] Transcribed: "%s"', text[:80] if text else "")
                return text if text else None
            except sr.UnknownValueError:
                logger.warning("[STT] Could not understand audio")
                return None
            except sr.RequestError as e:
                logger.warning("[STT] Recognition service error: %s", e)
                return None
    except Exception as e:
        logger.warning("[STT] Transcription error: %s", e)
        return None
    finally:
        for p in (src_path, wav_path):
            try:
                os.remove(p)
            except OSError:
                pass


def _get_base_url(request) -> str:
    """Derive base URL from incoming request (works in any environment)."""
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", ""))
    if not host:
        return ""
    return f"{scheme}://{host}"


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None


class ChatResponse(BaseModel):
    response: str
    audio: str | None = None
    playback_url: str | None = None
    user_text: str | None = None
    conversation_id: str | None = None


def _process_response_tags(response_text, tts_text, book_id):
    """Process story/library/local tags in response text. Returns (response_text, tts_text, book_id)."""
    
    # Check READ_STORY tag
    if not book_id:
        match = re.search(r"\[\[\[READ_STORY: (.*?)\]\]\]", response_text)
        if match:
            story_url = match.group(1).strip()
            logger.debug(f"Detected Story URL: {story_url}")
            try:
                story_content = get_chapter_content(story_url)
                if story_content and not story_content.startswith("Lỗi") and not story_content.startswith("Không tìm thấy"):
                    tts_text = story_content
                    response_text = response_text.replace(match.group(0), "").strip()
                    
                    # Try metadata first
                    title = "Unknown Story"
                    chapter = "Unknown Chapter"
                    meta_match = re.search(r"\[\[METADATA:\s*(.*?)\]\]", story_content, re.DOTALL)
                    if meta_match:
                        full_title = meta_match.group(1).strip()
                        story_content = story_content.replace(meta_match.group(0), "").strip()
                        tts_text = story_content
                        if " - " in full_title:
                            parts = full_title.rsplit(" - ", 1)
                            title = parts[0].strip()
                            chapter = parts[1].strip()
                        else:
                            chapter = full_title
                            title = "Truyện"
                    else:
                        parts = story_url.strip("/").split("/")
                        title = parts[-2].replace("-", " ").title() if len(parts) >= 2 else "Unknown Story"
                        chapter = parts[-1].replace("-", " ").title() if len(parts) >= 1 else "Unknown Chapter"
                    
                    book = library_manager.add_book(title, chapter, story_url)
                    book_id = book.id
                else:
                    response_text += f"\n\n🛑 {story_content}"
                    tts_text = story_content
            except Exception as e:
                logger.error(f"Failed to fetch story content: {e}")
                response_text += f"\n(Lỗi hệ thống: {e})"
                tts_text = f"Gặp lỗi khi tải truyện: {str(e)}"

    # Check READ_LIBRARY tag
    match_lib = re.search(r"\[\[\[READ_LIBRARY: (.*?)\]\]\]", response_text)
    if match_lib and not book_id:
        lib_id = match_lib.group(1).strip()
        found_book = library_manager.get_book(lib_id)
        if found_book:
            book_id = found_book.id
            tts_text = "Content from library"
            response_text = response_text.replace(match_lib.group(0), "").strip()
        else:
            logger.warning(f"Library tag found but ID not in library: {lib_id}")
            response_text += "\n(Không tìm thấy ID trong thư viện)"

    # Check READ_LOCAL tag
    match_local = re.search(r"\[\[\[READ_LOCAL:\s*(.*?)\|(.*?)\]\]\]", response_text)
    if match_local and not book_id:
        local_story_title = match_local.group(1).strip()
        local_chapter_file = match_local.group(2).strip()
        result = handle_read_local(local_story_title, local_chapter_file, library_manager, tts_cache)
        if "error" not in result:
            book_id = result["book_id"]
            tts_text = result["tts_text"]
            response_text = response_text.replace(match_local.group(0), "").strip()
        else:
            logger.warning(f"READ_LOCAL failed: {result['error']}")
            response_text += f"\n(Lỗi đọc local: {result['error']})"

    return response_text, tts_text, book_id


def _prepare_audio_url(book_id, tts_text, base_url=""):
    """Prepare audio URL and playback URL. Returns (audio_url, playback_url, request_id)."""
    playback_url = None
    if book_id:
        request_id = book_id
        tts_cache.save_tts_text(request_id, tts_text)
        audio_url = f"/api/tts/{request_id}"
        if base_url:
            from urllib.parse import quote
            playback_url = f"{base_url}/api/tts/{quote(request_id, safe='')}"
    else:
        request_id = str(uuid.uuid4())
        tts_cache.save_tts_text(request_id, tts_text)
        audio_url = f"/api/tts/{request_id}"

    return audio_url, playback_url, request_id


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, raw_request: Request = None, _=Depends(verify_api_key)):
    from services.shared_state import agent_app
    _t0 = _time.time()
    _msg_preview = request.message[:80] + "..." if len(request.message) > 80 else request.message
    logger.info(f'[REQUEST] POST /chat cid={request.conversation_id} msg="{_msg_preview}"')
    try:
        raw_response, cid = await session_service.resume_and_send(
            agent_app, prepend_tts_rules(request.message), request.conversation_id
        )
        response_text = process_agent_response(raw_response)
        tts_text = response_text
        book_id = None

        # Priority: Check pending read action
        pending = check_pending_read(library_manager, tts_cache, get_chapter_content)
        if pending:
            book_id = pending["book_id"]
            tts_text = pending["tts_text"]

        # Process tags in response
        response_text, tts_text, book_id = _process_response_tags(response_text, tts_text, book_id)

        # Clean text for TTS
        if tts_text:
            tts_text = clean_text_for_tts(tts_text)
            if _state.bg_scheduler and _state.bg_scheduler.is_running():
                logger.debug("[KB3] Pausing pre-gen for chat TTS")
                _state.bg_scheduler.request_pause()

        base_url = _get_base_url(raw_request) if raw_request else ""
        audio_url, playback_url, _ = _prepare_audio_url(book_id, tts_text, base_url)

        _duration = _time.time() - _t0
        logger.info(f"[RESPONSE] POST /chat cid={cid} duration={_duration:.1f}s status=ok")
        return ChatResponse(response=str(response_text), audio=audio_url, playback_url=playback_url, conversation_id=cid)

    except Exception as e:
        _duration = _time.time() - _t0
        logger.error(f"[RESPONSE] POST /chat duration={_duration:.1f}s status=error: {e}", exc_info=True)
        return ChatResponse(response=f"Error: {str(e)}")


@router.post("/chat-stream")
async def chat_stream(raw_request: Request, _=Depends(verify_api_key)):
    """SSE endpoint: streams progress events during agent processing, then sends final result.
    
    Accepts:
    - JSON body: {"message": "...", "conversation_id": "..."}
    - Multipart form: message + conversation_id fields + optional files (images/audio)
    """
    from sse_starlette.sse import EventSourceResponse
    from services.shared_state import agent_app
    
    _t0 = _time.time()
    
    # Parse request: multipart/form-data or JSON
    content_type = raw_request.headers.get("content-type", "")
    files_data: list[dict] = []
    
    if "multipart/form-data" in content_type:
        form = await raw_request.form()
        message = str(form.get("message", "")).strip()
        conversation_id = str(form.get("conversation_id", "")).strip() or None
        agent_name = str(form.get("agent_name", "")).strip() or None
        
        # Process uploaded files
        for key in form:
            if key == "files":
                upload_files = form.getlist("files")
                for f in upload_files:
                    if hasattr(f, 'read'):
                        file_content = await f.read()
                        content_type_f = f.content_type or "application/octet-stream"
                        data_b64 = base64.standard_b64encode(file_content).decode("ascii")
                        filename = f.filename or "unnamed"
                        
                        # Transcribe audio to text (LLMs don't support audio EmbeddedResource)
                        if content_type_f.startswith("audio/"):
                            transcribed = await _transcribe_audio(file_content, filename)
                            if transcribed:
                                # Prepend transcription to message
                                stt_prefix = f"[Voice message]: {transcribed}"
                                message = f"{stt_prefix}\n{message}" if message else stt_prefix
                                logger.info("[STT] Audio transcribed, appended to message")
                            else:
                                logger.warning("[STT] Failed to transcribe %s", filename)
                            continue  # Don't add audio file to files_data
                        
                        files_data.append({
                            "filename": filename,
                            "content_type": content_type_f,
                            "data_b64": data_b64,
                            "size": len(file_content),
                        })
    else:
        body = await raw_request.json()
        message = body.get("message", "").strip()
        conversation_id = body.get("conversation_id") or None
        agent_name = body.get("agent_name") or None
    
    if not message and not files_data:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=400, content={"detail": "Message or files required"})
    
    _msg_preview = message[:80] + "..." if len(message) > 80 else message
    _file_desc = f" [+{len(files_data)} file(s)]" if files_data else ""
    logger.info(f'[REQUEST] POST /chat-stream cid={conversation_id} msg="{_msg_preview}"{_file_desc}')
    
    request_id = str(uuid.uuid4())
    queue = progress_manager.create(request_id)
    progress_manager.push(request_id, "start", {"message": "Processing..."})

    # Resolve (or create) the real backend session id up front. Must happen
    # BEFORE create_progress_hooks so every tool_call/tool_result row is
    # tagged with the same id the frontend will later query. Without this,
    # client-generated UUIDs or first-send-no-id cases leave rows with
    # session_id=None and the reload path returns no tool bubbles.
    conversation_id = session_service.ensure_session(conversation_id)

    # Transition #1: idle → running
    from services.activity_stream import activity_stream_manager
    # Resolve display name for activity stream
    agent_display = agent_name or "Jarvis"
    started_msg = f"🚀 {agent_display} processing..."
    activity_stream_manager.broadcast({
        "agent_name": agent_display,
        "event_type": "started",
        "message": started_msg,
        "run_id": request_id,
        "timestamp": _time.time(),
    })
    _persist_activity(agent_display, "started", started_msg, run_id=request_id, session_id=conversation_id)

    # Wire spawn bridge to this request's SSE queue
    import services.shared_state as _state
    if _state.spawn_bridge:
        _state.spawn_bridge.set_request_id(request_id)

    async def run_chat():
        try:
            original_hooks = {}
            progress_hooks = create_progress_hooks(request_id, session_id=conversation_id)
            
            # Merge pause hooks for in-process agents
            from services.pause_manager import pause_manager
            
            for name, agent in agent_app._agents.items():
                original_hooks[name] = getattr(agent, 'tool_runner_hooks', None)
                existing = original_hooks[name]
                
                # Create pause hooks for this agent
                pause_hooks = pause_manager.create_pause_hooks(name)
                combined = merge_hooks(progress_hooks, pause_hooks)
                
                if existing:
                    agent.tool_runner_hooks = merge_hooks(existing, combined)
                else:
                    agent.tool_runner_hooks = combined
            
            try:
                # Expose conversation_id so spawn tools can auto-capture it
                _state.current_conversation_id = conversation_id
                raw_response, cid = await session_service.resume_and_send(
                    agent_app, prepend_tts_rules(message), conversation_id,
                    files_data=files_data if files_data else None,
                    agent_name=agent_name,
                )
                _state.current_conversation_id = cid  # update with resolved cid
                
                # Save context window snapshot for the active agent
                # Reuses the same centralized service as spawned agents
                target_name = agent_name or "Jarvis"
                try:
                    from services.context_persistence import save_agent_context
                    target_agent_obj = agent_app._agents.get(target_name)
                    if target_agent_obj:
                        await save_agent_context(
                            target_agent_obj,
                            request_id,
                            trigger="chat_complete",
                            agent_name=target_name,
                            session_id=cid,
                        )
                except Exception as _ctx_err:
                    logger.warning("[CONTEXT] Failed to save built-in agent context: %s", _ctx_err)
                
                response_text = process_agent_response(raw_response)
                tts_text = response_text
                book_id = None
                
                pending = check_pending_read(library_manager, tts_cache, get_chapter_content)
                if pending:
                    book_id = pending["book_id"]
                    tts_text = pending["tts_text"]
                
                response_text, tts_text, book_id = _process_response_tags(response_text, tts_text, book_id)
                
                if tts_text:
                    tts_text = clean_text_for_tts(tts_text)
                    if _state.bg_scheduler and _state.bg_scheduler.is_running():
                        logger.debug("[KB3] Pausing pre-gen for chat TTS")
                        _state.bg_scheduler.request_pause()
                
                base_url = _get_base_url(raw_request) if raw_request else ""
                audio_url, playback_url, _ = _prepare_audio_url(book_id, tts_text, base_url)
                
                # Collect total token usage
                total_tokens = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0}
                for name, agent in agent_app._agents.items():
                    try:
                        acc = getattr(agent, 'usage_accumulator', None)
                        if acc and acc.turns:
                            total_tokens["input"] += acc.cumulative_input_tokens
                            total_tokens["output"] += acc.cumulative_output_tokens
                            total_tokens["reasoning"] += acc.cumulative_reasoning_tokens
                            total_tokens["cache_read"] += acc.cumulative_cache_read_tokens
                    except Exception:
                        pass
                
                _duration = _time.time() - _t0
                _resp_preview = str(response_text)[:100]
                _total_tok = total_tokens['input'] + total_tokens['output']
                logger.info(f'[RESPONSE] POST /chat-stream cid={cid} duration={_duration:.1f}s tokens={_total_tok} resp="{_resp_preview}..."')
                
                progress_manager.push(request_id, "done", {
                    "response": str(response_text),
                    "audio": audio_url,
                    "playback_url": playback_url,
                    "conversation_id": cid,
                    "total_tokens": total_tokens,
                })
                
                # Broadcast idle event to global activity stream
                # so dashboard cards update from "Running" → "Idle"
                from services.activity_stream import activity_stream_manager
                for name, agent in agent_app._agents.items():
                    agent_display = name
                    idle_msg = f"💤 {agent_display} waiting for next task..."
                    activity_stream_manager.broadcast({
                        "agent_name": name,
                        "event_type": "idle",
                        "message": idle_msg,
                        "run_id": request_id,
                        "timestamp": _time.time(),
                    })
                # Only persist idle for Jarvis (main agent) — skip team agents
                _persist_activity(
                    "Jarvis", "idle",
                    f"💤 Jarvis waiting for next task...",
                    run_id=request_id, session_id=cid,
                )
                
            finally:
                _state.current_conversation_id = None  # cleanup
                for name, agent in agent_app._agents.items():
                    if name in original_hooks:
                        original = original_hooks[name]
                        if original:
                            agent.tool_runner_hooks = original
                        else:
                            agent.tool_runner_hooks = None
                            
        except asyncio.CancelledError:
            raise
        except BaseException as e:
            _duration = _time.time() - _t0
            logger.error(f"[RESPONSE] POST /chat-stream duration={_duration:.1f}s status=error: {e}", exc_info=True)
            progress_manager.push(request_id, "error", {"message": str(e)})
            
            # Broadcast idle even on error so dashboard cards don't stay "Running"
            try:
                from services.activity_stream import activity_stream_manager
                for name, agent in agent_app._agents.items():
                    activity_stream_manager.broadcast({
                        "agent_name": name,
                        "event_type": "idle",
                        "message": f"💤 {name} waiting for next task...",
                        "run_id": request_id,
                        "timestamp": _time.time(),
                    })
            except Exception:
                pass
    
    asyncio.create_task(run_chat())
    
    async def event_generator():
        try:
            while True:
                try:
                    # Use 30s timeout between events — if no event, send keepalive ping
                    # This keeps SSE alive during long-running operations (e.g. approval waits)
                    event = await asyncio.wait_for(queue.get(), timeout=30)
                    yield {"data": json.dumps(event, ensure_ascii=False)}
                    if event.get("type") in ("done", "error"):
                        break
                except asyncio.TimeoutError:
                    # Send keepalive ping to prevent browser/proxy from closing connection
                    yield {"data": json.dumps({"type": "ping"}, ensure_ascii=False)}
        finally:
            # Disconnect spawn bridge from this request
            if _state.spawn_bridge:
                _state.spawn_bridge.set_request_id(None)
            progress_manager.remove(request_id)
    
    return EventSourceResponse(event_generator())


@router.post("/chat-audio", response_model=ChatResponse)
async def chat_audio(conversation_id: str = None, file: UploadFile = File(...),
                     raw_request: Request = None,
                     _=Depends(verify_api_key)):
    from services.shared_state import agent_app
    _t0 = _time.time()
    logger.info(f"[REQUEST] POST /chat-audio cid={conversation_id} file={file.filename}")
    
    TEMP_DIR = "temp_audio"
    os.makedirs(TEMP_DIR, exist_ok=True)
    
    temp_filename = os.path.join(TEMP_DIR, f"temp_{uuid.uuid4()}.m4a")
    wav_filename = os.path.join(TEMP_DIR, f"temp_{uuid.uuid4()}.wav")
    
    try:
        try:
            with open(temp_filename, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
                
            import subprocess
            try:
                subprocess.run(["ffmpeg", "-i", temp_filename, "-ac", "1", "-ar", "16000", wav_filename, "-y"], check=True, capture_output=True)
                
                import wave
                import audioop
                with wave.open(wav_filename, 'rb') as wf:
                    frames = wf.readframes(wf.getnframes())
                    rms = audioop.rms(frames, wf.getsampwidth())
                    if rms < 100:
                        return ChatResponse(response="Lỗi: Âm thanh quá nhỏ.")

            except subprocess.CalledProcessError as e:
                logger.error(f"FFmpeg error: {e.stderr.decode()}")
                return ChatResponse(response="Lỗi xử lý âm thanh.")
                
            recognizer = sr.Recognizer()
            with sr.AudioFile(wav_filename) as source:
                audio_data = recognizer.record(source)
                try:
                    text = recognizer.recognize_google(audio_data, language="vi-VN")
                    logger.info(f'[STT] Recognized: "{text[:80]}"')
                    if not text: return ChatResponse(response="Không nghe thấy gì.")
                except sr.UnknownValueError:
                    return ChatResponse(response="Xin lỗi, tôi không nghe rõ.")
                except sr.RequestError as e:
                    return ChatResponse(response=f"Lỗi STT: {e}")
        
        finally:
            if os.path.exists(temp_filename):
                try: os.remove(temp_filename)
                except: pass
            if os.path.exists(wav_filename):
                try: os.remove(wav_filename)
                except: pass
        
        raw_response, cid = await session_service.resume_and_send(
            agent_app, prepend_tts_rules(text), conversation_id
        )
        response_text = process_agent_response(raw_response)
        tts_text = response_text
        book_id = None

        pending = check_pending_read(library_manager, tts_cache, get_chapter_content)
        if pending:
            book_id = pending["book_id"]
            tts_text = pending["tts_text"]

        response_text, tts_text, book_id = _process_response_tags(response_text, tts_text, book_id)

        if tts_text:
            tts_text = clean_text_for_tts(tts_text)

        base_url = _get_base_url(raw_request) if raw_request else ""
        audio_url, playback_url, _ = _prepare_audio_url(book_id, tts_text, base_url)

        _duration = _time.time() - _t0
        logger.info(f"[RESPONSE] POST /chat-audio cid={cid} duration={_duration:.1f}s status=ok")
        return ChatResponse(response=str(response_text), audio=audio_url, playback_url=playback_url, user_text=text, conversation_id=cid)

    except Exception as e:
        _duration = _time.time() - _t0
        logger.error(f"[RESPONSE] POST /chat-audio duration={_duration:.1f}s status=error: {e}", exc_info=True)
        return ChatResponse(response=f"Error: {str(e)}")


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/background/status")
async def background_status(_=Depends(verify_api_key)):
    """Unified status for all background jobs."""
    if _state.bg_scheduler:
        return _state.bg_scheduler.get_all_status()
    return {"status": "disabled"}
