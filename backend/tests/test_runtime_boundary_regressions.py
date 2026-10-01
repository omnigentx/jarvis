"""Real SQLite/Unix-socket regressions for isolated DB and bounded shutdown."""

import asyncio
import json
import sqlite3
import tempfile
import shutil
from pathlib import Path
from unittest.mock import Mock
import pytest
from sqlalchemy import create_engine
from services import meeting_hooks_bridge as meetings
from services.spawn_event_socket import SpawnEventSocketServer


def seed(path, meeting):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE meeting_events(id INTEGER PRIMARY KEY,event_type TEXT,meeting_id TEXT,data_json TEXT,created_at REAL)"
        )
        conn.execute(
            "INSERT INTO meeting_events VALUES(1,?,?,?,0)",
            ("updated", meeting, json.dumps({"marker": meeting})),
        )


def test_meeting_bridge_uses_canonical_engine_database(tmp_path, monkeypatch):
    from core import database

    first = tmp_path / "first.db"
    second = tmp_path / "second.db"
    seed(first, "first")
    seed(second, "second")
    monkeypatch.setattr(database, "engine", create_engine("sqlite:///" + str(first)))
    monkeypatch.setenv("SPAWN_REGISTRY_DB", str(second))
    bridge = meetings.create_meeting_bridge(Mock())
    assert [x["meeting_id"] for x in bridge._poll()] == ["first"]
    monkeypatch.setattr(database, "engine", create_engine("sqlite:///" + str(second)))
    assert [
        x["meeting_id"] for x in meetings.create_meeting_bridge(Mock())._poll()
    ] == ["second"]


@pytest.mark.asyncio
async def test_socket_shutdown_does_not_wait_for_idle_child_disconnect(tmp_path):
    tmp_path = Path(tempfile.mkdtemp(prefix="socket-test-", dir="/tmp"))
    path = str(tmp_path / "events.sock")
    processed = asyncio.Event()
    bridge = Mock()
    bridge.process_event.side_effect = lambda _: processed.set()
    server = SpawnEventSocketServer(path, bridge)
    await server.start()
    reader, writer = await asyncio.open_unix_connection(path)
    writer.write(b'{"event":"test"}\n')
    await writer.drain()
    await asyncio.wait_for(processed.wait(), timeout=1)
    try:
        await asyncio.wait_for(server.stop(), timeout=0.5)
        assert not (tmp_path / "events.sock").exists()
        assert await asyncio.wait_for(reader.read(), timeout=0.5) == b""
    finally:
        writer.close()
        await writer.wait_closed()
        await server.stop()
        shutil.rmtree(tmp_path, ignore_errors=True)


@pytest.mark.asyncio
async def test_socket_can_restart_same_instance():
    directory = Path(tempfile.mkdtemp(prefix="socket-reuse-", dir="/tmp"))
    server = SpawnEventSocketServer(str(directory / "events.sock"), Mock())
    try:
        await server.start()
        await server.stop()
        await server.start()
        reader, writer = await asyncio.open_unix_connection(
            str(directory / "events.sock")
        )
        writer.write(b"{}\n")
        await writer.drain()
        # An open idle client must remain accepted after restart.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(reader.read(1), timeout=0.05)
        await asyncio.wait_for(server.stop(), timeout=0.5)
        writer.close()
        await writer.wait_closed()
    finally:
        await server.stop()
        shutil.rmtree(directory, ignore_errors=True)


@pytest.mark.asyncio
async def test_canonical_meeting_database_event_reaches_push_subscriber(
    tmp_path, monkeypatch
):
    from core import database
    from services.meeting_events import MeetingEventManager

    first = tmp_path / "source.db"
    second = tmp_path / "other.db"
    seed(first, "canonical")
    seed(second, "wrong-database")
    engine = create_engine("sqlite:///" + str(first))
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setenv("SPAWN_REGISTRY_DB", str(second))
    manager = MeetingEventManager()
    delivered = asyncio.Future()
    manager.subscribe("updated", lambda event: delivered.set_result(event))
    watcher = asyncio.create_task(
        meetings.create_meeting_bridge(manager).watch(poll_interval=0.001)
    )
    try:
        event = await asyncio.wait_for(delivered, 1)
        assert event.meeting_id == "canonical"
        assert event.data == {"marker": "canonical"}
    finally:
        watcher.cancel()
        with pytest.raises(asyncio.CancelledError):
            await watcher
        engine.dispose()
