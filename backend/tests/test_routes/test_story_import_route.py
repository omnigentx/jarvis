"""Real multipart requests; only synthetic data."""
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth import verify_api_key
from routes.story_import import router


def client():
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[verify_api_key] = lambda: True
    return TestClient(app)


def test_invalid_multipart_and_nested_files_leave_no_story(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with client() as c:
        response = c.post('/api/stories/import', data={'title': 'Synthetic', 'request_id': str(uuid.uuid4())},
                          files=[('files', ('root/sub/1.txt', b'Synthetic', 'text/plain'))])
        assert response.status_code == 422
        assert response.json()['detail']['code'] == 'nested_folder'
        assert not list((tmp_path / 'data/stories').glob('import-*'))
        assert c.post('/api/stories/import', json={}).status_code == 400


def test_success_retry_read_and_delete(tmp_path, monkeypatch):
    from routes.stories import router as stories
    from core.database import init_db
    monkeypatch.chdir(tmp_path)
    init_db()
    c = client()
    c.app.include_router(stories)
    key = str(uuid.uuid4())
    payload = {'title': 'Synthetic upload', 'request_id': key}
    uploads = [('files', ('root/1_First.txt', b'First synthetic chapter', 'text/plain')),
               ('files', ('root/2_Second.txt', b'Second synthetic chapter', 'text/plain'))]
    with c:
        response = c.post('/api/stories/import', data=payload, files=uploads)
        assert response.status_code == 200, response.text
        story = response.json()
        assert story['chapters'] == 2
        assert c.post('/api/stories/import', data=payload, files=uploads).json() == story
        chapters = c.get(f"/api/stories/{story['id']}/chapters").json()
        assert len(chapters) == 2
        filename = chapters[0]['file']
        assert c.get(f"/api/stories/{story['id']}/chapters/{filename}").json()['content'] == 'First synthetic chapter'
        assert c.delete(f"/api/stories/{story['id']}").status_code == 200
        assert not (tmp_path / 'data/stories' / story['id']).exists()


def test_request_budget_and_auth_before_upload(tmp_path, monkeypatch):
    import routes.story_import as route
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(route, 'MAX_REQUEST_BYTES', 50)
    with client() as c:
        response = c.post('/api/stories/import', data={'title': 'Test', 'request_id': str(uuid.uuid4())},
                          files={'files': ('a.txt', b'A' * 200, 'text/plain')})
        assert response.status_code == 413
    from fastapi import HTTPException
    c = client()
    def denied():
        raise HTTPException(401)
    c.app.dependency_overrides[verify_api_key] = denied
    with c:
        assert c.post('/api/stories/import', content=b'broken').status_code == 401


import asyncio
import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize('cancelled', [False, True])
async def test_interrupted_multipart_closes_allocated_files(monkeypatch, cancelled):
    from starlette.requests import Request
    import starlette.formparsers as parsers
    from routes.story_import import upload_story
    allocated = []
    original = parsers.SpooledTemporaryFile
    def tracked(*args, **kwargs):
        file = original(*args, **kwargs)
        allocated.append(file)
        return file
    monkeypatch.setattr(parsers, 'SpooledTemporaryFile', tracked)
    received = False
    async def receive():
        nonlocal received
        if not received:
            received = True
            return {'type': 'http.request', 'more_body': True, 'body': (
                b'--sample\r\nContent-Disposition: form-data; name="files"; filename="a.txt"\r\n'
                b'Content-Type: text/plain\r\n\r\nSynthetic incomplete chapter')}
        if cancelled:
            raise asyncio.CancelledError()
        return {'type': 'http.disconnect'}
    request = Request({'type': 'http', 'method': 'POST', 'path': '/api/stories/import',
                       'headers': [(b'content-type', b'multipart/form-data; boundary=sample')]}, receive)
    result = await upload_story(request, True)
    assert result.status_code == 400
    assert allocated and all(file.closed for file in allocated)


def test_progress_stream_contains_counts_without_content(tmp_path, monkeypatch):
    from services.activity_stream import activity_stream_manager
    from core.database import init_db
    monkeypatch.chdir(tmp_path)
    init_db()
    subscriber, queue = activity_stream_manager.subscribe()
    try:
        with client() as c:
            key = str(uuid.uuid4())
            result = c.post('/api/stories/import', data={'title': 'Synthetic', 'request_id': key},
                            files={'files': ('a.txt', b'Private synthetic chapter body', 'text/plain')})
            assert result.status_code == 200
        events = []
        while not queue.empty(): events.append(queue.get_nowait())
        assert events[-1]['data'] == {'request_id': key, 'completed': 1, 'total': 1, 'phase': 'done'}
        assert 'Private synthetic' not in str(events)
        assert 'a.txt' not in str(events)
    finally:
        activity_stream_manager.unsubscribe(subscriber)
