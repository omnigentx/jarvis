"""Authenticated, bounded multipart upload for local text chapters."""
import asyncio
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect

from core.auth import verify_api_key
from services.activity_stream import activity_stream_manager
from services.story_import import ImportProblem, MAX_FILES, MAX_REQUEST_BYTES, import_story

logger = logging.getLogger(__name__)
router = APIRouter(prefix='/api/stories', tags=['stories'])


@router.post('/import')
async def upload_story(request: Request, _=Depends(verify_api_key)):
    form = None
    too_large = False
    try:
        if not request.headers.get('content-type', '').startswith('multipart/form-data;'):
            raise ImportProblem('invalid_multipart', 400)

        async def bounded_stream():
            nonlocal too_large
            size = 0
            try:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_REQUEST_BYTES:
                        too_large = True
                        raise MultiPartException('request_too_large')
                    yield chunk
            except (ClientDisconnect, asyncio.CancelledError):
                # Parser cleanup runs only for MultiPartException, including
                # temporary uploads allocated before an interrupted body.
                raise MultiPartException('upload_interrupted') from None

        parser = MultiPartParser(request.headers, bounded_stream(), max_files=MAX_FILES,
                                 max_fields=2, max_part_size=4096)
        form = await parser.parse()
        if any(k not in {'title', 'request_id', 'files'} for k, _ in form.multi_items()):
            raise ImportProblem('invalid_fields', 400)
        titles, keys, files = (form.getlist(k) for k in ('title', 'request_id', 'files'))
        if (len(titles) != 1 or len(keys) != 1 or not isinstance(titles[0], str)
                or not isinstance(keys[0], str) or any(not isinstance(f, UploadFile) for f in files)):
            raise ImportProblem('invalid_fields', 400)
        key = keys[0]
        loop = asyncio.get_running_loop()

        def progress(completed: int, total: int):
            event = {'event_type': 'story_import', 'data': {
                'request_id': key, 'completed': completed, 'total': total, 'phase': 'validating'}}
            loop.call_soon_threadsafe(activity_stream_manager.broadcast, event)

        worker = asyncio.create_task(asyncio.to_thread(import_story, titles[0], key, files,
                                                       progress=progress))
        try:
            result = await asyncio.shield(worker)
        except asyncio.CancelledError:
            # The worker may already be publishing. Keep upload handles alive
            # until it stops; a same-key retry returns the committed result.
            await worker
            raise
        activity_stream_manager.broadcast({'event_type': 'story_import', 'data': {
            'request_id': key, 'completed': len(files), 'total': len(files), 'phase': 'done'}})
        return result
    except ImportProblem as exc:
        return JSONResponse(status_code=exc.status, content={'detail': {
            'code': exc.code, 'index': exc.index}})
    except MultiPartException:
        return JSONResponse(status_code=413 if too_large else 400,
                            content={'detail': {'code': 'request_too_large' if too_large else 'invalid_multipart'}})
    except Exception:
        logger.exception('[STORY_IMPORT] Import failed')
        return JSONResponse(status_code=500, content={'detail': {'code': 'import_failed'}})
    finally:
        if form is not None:
            await form.close()
