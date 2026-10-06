"""Playback races with real tasks/files; only speech network is scripted."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from starlette.requests import Request


@pytest.fixture
def playback(tmp_path, monkeypatch):
    import routes.tts as route
    import services.shared_state as state
    cache = tmp_path / 'audio.mp3'
    text = 'Synthetic chapter for playback acceptance. ' * 400
    monkeypatch.setattr(route, 'tts_cache', MagicMock())
    route.tts_cache.get_tts_text.return_value = text
    monkeypatch.setattr(route, 'library_manager', MagicMock())
    route.library_manager.get_book.return_value = None
    monkeypatch.setattr(route, 'get_audio_cache_path', lambda _: str(cache))
    monkeypatch.setattr(state, 'bg_scheduler', None)
    monkeypatch.setattr(route, 'generation_tasks', {})
    gate = asyncio.Event()
    started = asyncio.Event()
    calls = []

    class Provider:
        async def stream_audio(self, text):
            calls.append(text)
            yield b'A' * 18000
            started.set()
            await gate.wait()
            yield b'B' * 100000

    monkeypatch.setattr(state, 'tts_stories_provider', Provider())
    return SimpleNamespace(route=route, cache=cache, gate=gate, started=started, calls=calls)


def request(method='GET'):
    return Request({'type': 'http', 'method': method, 'headers': []})


@pytest.mark.asyncio
async def test_second_get_does_not_delete_active_audio_or_duplicate_writer(playback):
    env = playback
    first = await env.route.tts_endpoint('story_synthetic_01.txt', request())
    try:
        first_bytes = await asyncio.wait_for(anext(first.body_iterator), 2)
        assert first_bytes
        await env.started.wait()
        second = await env.route.tts_endpoint('story_synthetic_01.txt', request())
        assert env.cache.exists() or Path(str(env.cache) + '.tmp').exists()
        assert len(env.calls) == 1
        env.gate.set()
        one = first_bytes + b''.join([c async for c in first.body_iterator])
        two = b''.join([c async for c in second.body_iterator])
        assert one == two == b'A' * 18000 + b'B' * 100000
    finally:
        env.gate.set()
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_distinct_request_ids_for_same_text_share_one_writer(playback):
    env = playback
    first = await env.route.tts_endpoint('story_synthetic_01.txt', request())
    try:
        await asyncio.wait_for(anext(first.body_iterator), 2)
        await env.started.wait()
        second = await env.route.tts_endpoint('story_alias_01.txt', request())
        assert len(env.calls) == 1
        env.gate.set()
        data = b''.join([c async for c in second.body_iterator])
        assert data == b'A' * 18000 + b'B' * 100000
    finally:
        env.gate.set()
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_provider_failure_is_502_not_success_json_and_can_retry(playback, monkeypatch):
    from fastapi import HTTPException
    import services.shared_state as state
    from services.audio_generation import active_generation

    class Broken:
        async def stream_audio(self, text):
            raise RuntimeError('Synthetic provider failure')
            yield

    monkeypatch.setattr(state, 'tts_stories_provider', Broken())
    with pytest.raises(HTTPException) as exc:
        await playback.route.tts_endpoint('story_failure_01.txt', request())
    assert exc.value.status_code == 502
    assert active_generation(str(playback.cache)) is None
    assert not playback.cache.exists()
    assert not Path(str(playback.cache) + '.tmp').exists()
    assert not Path(str(playback.cache) + '.lock').exists()


@pytest.mark.asyncio
async def test_disconnect_does_not_cancel_shared_generation(playback):
    env = playback
    first = await env.route.tts_endpoint('story_disconnect_01.txt', request())
    await anext(first.body_iterator)
    await first.body_iterator.aclose()
    second = await env.route.tts_endpoint('story_disconnect_01.txt', request())
    assert len(env.calls) == 1
    env.gate.set()
    data = b''.join([c async for c in second.body_iterator])
    assert data == b'A' * 18000 + b'B' * 100000
    assert env.cache.read_bytes() == data
    assert not Path(str(env.cache) + '.tmp').exists()
    assert not Path(str(env.cache) + '.lock').exists()


@pytest.mark.asyncio
async def test_head_is_side_effect_free_and_live_has_no_static_length(playback):
    env = playback
    idle_head = await env.route.tts_endpoint('story_head_01.txt', request('HEAD'))
    assert 'x-tts-generating' not in idle_head.headers
    assert not env.calls
    first = await env.route.tts_endpoint('story_head_01.txt', request())
    try:
        await anext(first.body_iterator)
        head = await env.route.tts_endpoint('story_head_01.txt', request('HEAD'))
        assert head.headers['x-tts-generating'] == '1'
        assert 'content-length' not in first.headers
        assert 'accept-ranges' not in first.headers
    finally:
        env.gate.set()
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_stalled_provider_releases_cache_lease(playback, monkeypatch):
    from fastapi import HTTPException
    import services.shared_state as state
    import services.tts_audio_response as response
    from services.audio_generation import active_generation
    monkeypatch.setattr(response, 'PROGRESS_TIMEOUT', .01)

    class Stalled:
        async def stream_audio(self, text):
            await asyncio.Event().wait()
            yield

    monkeypatch.setattr(state, 'tts_stories_provider', Stalled())
    with pytest.raises(HTTPException):
        await playback.route.tts_endpoint('story_stalled_01.txt', request())
    assert active_generation(str(playback.cache)) is None
    assert not Path(str(playback.cache) + '.lock').exists()


@pytest.mark.asyncio
async def test_other_process_lease_returns_retryable_503(playback):
    import fcntl
    from fastapi import HTTPException
    env = playback
    with open(str(env.cache) + '.lock', 'wb') as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(HTTPException) as exc:
            await env.route.tts_endpoint('story_foreign_01.txt', request())
        assert exc.value.status_code == 503
        assert exc.value.headers['Retry-After'] == '2'
        assert not env.calls


@pytest.mark.asyncio
async def test_http_static_ranges_and_auth(playback, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from core.auth import verify_api_key
    import core.auth as auth
    monkeypatch.setattr(auth, "JARVIS_API_KEY", "synthetic-playback-test-key")
    env = playback
    env.cache.write_bytes(b'A' * 100000)
    app = FastAPI()
    app.include_router(env.route.router)
    app.dependency_overrides[verify_api_key] = lambda: True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        res = await client.get('/api/tts/story_ranges_01.txt', headers={'Range': 'bytes=10-19'})
        assert res.status_code == 206
        assert res.content == b'A' * 10
        assert res.headers['content-range'] == 'bytes 10-19/100000'
        suffix = await client.get('/api/tts/story_ranges_01.txt', headers={'Range': 'bytes=-7'})
        assert suffix.status_code == 206 and suffix.content == b'A' * 7
        invalid = await client.get('/api/tts/story_ranges_01.txt', headers={'Range': 'bytes=100000-'})
        assert invalid.status_code == 416
        app.dependency_overrides.clear()
        unauth = await client.get('/api/tts/story_ranges_01.txt')
        assert unauth.status_code in (401, 403)
    assert not env.calls


@pytest.mark.asyncio
async def test_status_stream_pushes_ready_without_status_requests(playback):
    env = playback
    first = await env.route.tts_endpoint('story_status_01.txt', request())
    await anext(first.body_iterator)
    stream = await env.route.audio_status_stream('story_status_01.txt')
    initial = await anext(stream.body_iterator)
    assert initial['data'] == '{"status": "generating"}'
    env.gate.set()
    statuses = [event async for event in stream.body_iterator]
    assert statuses[-1]['data'] == '{"status": "ready"}'
    assert len(env.calls) == 1


@pytest.mark.asyncio
async def test_midstream_error_removes_partial_and_pushes_error(playback, monkeypatch):
    import services.shared_state as state
    env = playback
    gate = asyncio.Event()
    class BrokenLater:
        async def stream_audio(self, text):
            yield b'A' * 18000
            await gate.wait()
            raise RuntimeError('Synthetic later failure')
    monkeypatch.setattr(state, 'tts_stories_provider', BrokenLater())
    first = await env.route.tts_endpoint('story_later_01.txt', request())
    await anext(first.body_iterator)
    stream = await env.route.audio_status_stream('story_later_01.txt')
    await anext(stream.body_iterator)
    gate.set()
    statuses = [event async for event in stream.body_iterator]
    assert statuses[-1]['data'] == '{"status": "error"}'
    with pytest.raises(RuntimeError):
        [part async for part in first.body_iterator]
    assert not env.cache.exists()
    assert not Path(str(env.cache) + '.tmp').exists()
