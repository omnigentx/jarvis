"""HTTP audio delivery: one producer, atomic cache, event-driven readers."""
from __future__ import annotations

import asyncio
import logging
import os

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from services.tts import TTSProvider, EdgeTTSProvider
from services.audio_retry import retry_after, record_failure, clear_failure
from services.tts_checkpoints import stream_story_audio, discard_checkpoints
from services.library_manager import LibraryManager, AudioBook
from services.background_jobs import BackgroundJobScheduler
from services.audio_generation import GenerationBusy, active_generation, claim_generation, PROGRESS_TIMEOUT

logger = logging.getLogger(__name__)


async def audio_response(
    path: str, text: str | None, request_id: str, request: Request,
    provider: TTSProvider, generation_tasks: dict[str, asyncio.Task],
    library_manager: LibraryManager, book: AudioBook | None,
    scheduler: BackgroundJobScheduler | None, *, wait_complete: bool = False,
) -> Response:
    generation = active_generation(path)
    headers = {'Cache-Control': 'no-cache, no-store, must-revalidate'}
    if request.method == 'HEAD':
        headers['Content-Type'] = 'audio/mpeg'
        if generation or os.path.exists(path + '.lock'):
            headers['X-TTS-Generating'] = '1'
        elif os.path.isfile(path):
            headers['Content-Length'] = str(os.path.getsize(path))
        return Response(headers=headers)

    # Never inspect/delete a growing file as a completed cache entry.
    if not generation and os.path.isfile(path):
        if not os.path.exists(path + '.lock') and (not text or os.path.getsize(path) >= len(text) * 3):
            return FileResponse(path, media_type='audio/mpeg', headers=headers)
        if not os.path.exists(path + '.lock'):
            os.remove(path)  # Legacy incomplete cache, no active producer.

    if scheduler and scheduler.is_running():
        if not generation:
            scheduler.request_cancel()
        else:
            scheduler.request_resume()
    checkpointed = isinstance(provider, EdgeTTSProvider) and not wait_complete
    if checkpointed and text and not generation:
        delay = retry_after(text, provider.voice, provider.rate)
        if delay:
            raise HTTPException(503, 'Speech generation failed; please retry later',
                                headers={'Retry-After': str(delay)})
    try:
        generation, owns = claim_generation(path)
    except GenerationBusy as exc:
        raise HTTPException(503, 'Audio is being prepared; please retry',
                            headers={'Retry-After': '2'}) from exc

    if owns:
        if not text:
            generation.finish(False)
            raise HTTPException(404, 'Audio source is unavailable')

        async def produce() -> None:
            success = False
            try:
                if book:
                    library_manager.set_status(book.id, 'generating')
                iterator = (stream_story_audio(provider, text, path) if checkpointed
                            else provider.stream_audio(text)).__aiter__()
                try:
                    with open(generation.temporary_path, 'wb') as audio:
                        while True:
                            try:
                                chunk = await asyncio.wait_for(anext(iterator), PROGRESS_TIMEOUT)
                            except StopAsyncIteration:
                                break
                            if chunk:
                                audio.write(chunk)
                                audio.flush()
                                generation.notify(audio.tell())
                finally:
                    await iterator.aclose()
                if not generation.size:
                    raise RuntimeError('Provider returned no audio')
                os.replace(generation.temporary_path, path)
                success = True
                if checkpointed:
                    try:
                        clear_failure(text, provider.voice, provider.rate)
                        await asyncio.to_thread(discard_checkpoints, provider, text, path)
                    except Exception:
                        logger.warning('[TTS] Audio ready; retry/checkpoint housekeeping failed', exc_info=True)
                if book:
                    library_manager.set_status(book.id, 'ready')
            except asyncio.CancelledError:
                if book:
                    library_manager.set_status(book.id, 'ready' if success else 'pending')
                raise
            except Exception as exc:
                if checkpointed and not success:
                    try:
                        record_failure(text, provider.voice, provider.rate, type(exc).__name__)
                    except Exception:
                        logger.warning("[TTS] Unable to persist retry delay", exc_info=True)
                logger.error('[TTS] Audio producer failed', exc_info=True)
                if book:
                    library_manager.set_status(book.id, 'error')
            finally:
                generation.finish(success)
                task = asyncio.current_task()
                for alias in [key for key, value in generation_tasks.items() if value is task]:
                    del generation_tasks[alias]
                if scheduler:
                    scheduler.request_resume()
                    scheduler.notify_tts_done()

        generation.task = asyncio.create_task(produce())
        # A task cancelled before its coroutine starts never runs finally.
        generation.task.add_done_callback(lambda _: generation.finish(success=False) if not generation.done else None)
    task = getattr(generation, 'task', None)
    if task:
        generation_tasks[request_id] = task
    try:
        while (not generation.size or wait_complete) and not generation.done:
            await generation.wait(generation.revision)
        if generation.error:
            raise HTTPException(502, 'Unable to generate audio; please retry')
    except asyncio.TimeoutError as exc:
        raise HTTPException(504, 'Audio generation timed out; please retry') from exc
    if generation.done:
        return FileResponse(path, media_type='audio/mpeg', headers=headers)
    headers['X-TTS-Generating'] = '1'
    return StreamingResponse(generation.stream(), media_type='audio/mpeg', headers=headers)
