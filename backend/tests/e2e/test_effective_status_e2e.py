"""E2E regression for ``_compute_effective_status`` with REAL Unix
sockets, REAL SQLite, and the REAL ``AgentChannel.is_alive`` probe.

Why this exists in addition to the unit suite:
The unit tests in ``tests/test_services/test_effective_status.py``
patch ``AgentChannel.is_alive`` directly. That isolates the decision
tree but does not catch regressions in the underlying probe (e.g. the
2026-05-13 ``is_alive`` connect-probe fix — file-stat alone returns
True for orphan sock files left by SIGKILL'd subprocesses, and the
helper relied on a truthful liveness signal). This file exercises both
ends of the contract together: real probe + real DB + real lifecycle
invariant.

What this DOES NOT cover (intentionally):
* Spawning a real isolated_runner subprocess and watching it transition
  through running → idle. That path is covered by ``test_team_spawn``
  and ``test_subprocess_spawn``.
* The HTTP layer (``/api/agents`` route). Adding a full FastAPI test
  would only test JSON shape; the status invariants are entirely the
  helper's domain.
"""

from __future__ import annotations

import asyncio
import os
import socket
import sqlite3
import sys
import time
from pathlib import Path

import pytest

# Make backend/ importable so we can hit ``routes.agents`` and the
# real ``AgentChannel`` from the vendored fast-agent submodule.
_BACKEND = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
_FA_SRC = _BACKEND / "fast-agent" / "src"
if _FA_SRC.exists() and str(_FA_SRC) not in sys.path:
    sys.path.insert(0, str(_FA_SRC))

from routes.agents import _compute_effective_status  # noqa: E402


# ─── Real snapshot DB helpers ─────────────────────────────────────────


def _init_snapshot_db(db_path: Path) -> None:
    """Create the ``agent_context_snapshots`` table matching the
    production schema closely enough for the helper to read.
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS agent_context_snapshots ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "run_id TEXT, agent_name TEXT, session_id TEXT, "
            "team_name TEXT, context_json TEXT, "
            "message_count INTEGER, total_input_tokens INTEGER, "
            "total_output_tokens INTEGER, trigger TEXT, created_at REAL)"
        )
        conn.commit()
    finally:
        conn.close()


def _write_snapshot(
    db_path: Path,
    agent_name: str,
    trigger: str,
    created_at: float,
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO agent_context_snapshots "
            "(agent_name, trigger, created_at, run_id, message_count, "
            " total_input_tokens, total_output_tokens, context_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (agent_name, trigger, created_at, "test-run", 0, 0, 0, "[]"),
        )
        conn.commit()
    finally:
        conn.close()


# ─── Real channel sock fixtures ──────────────────────────────────────


@pytest.fixture
def isolated_channel_dir(tmp_path, monkeypatch):
    """Point ``AgentChannel`` at a tmp dir so we don't collide with the
    real project's channels. The fixture cleans up via tmp_path teardown.
    """
    # _get_sock_dir reads TEAM_WORKSPACE then walks up for .runtime; the
    # test bypass is to set SPAWN_PROJECT_DIR which the resolver also
    # honours (and which doesn't require a .runtime ancestor).
    monkeypatch.setenv("SPAWN_PROJECT_DIR", str(tmp_path))
    monkeypatch.delenv("TEAM_WORKSPACE", raising=False)
    return tmp_path


async def _start_real_channel(agent_name: str):
    """Start a real ``AgentChannel`` listener bound to a real Unix
    socket in the test's isolated channel dir. Returns the channel —
    the caller must await ``channel.stop()`` in finally.
    """
    from fast_agent.spawn.agent_channel import AgentChannel

    channel = AgentChannel(agent_name)
    await channel.start_server()
    return channel


def _create_orphan_sock(agent_name: str) -> Path:
    """Reproduce the SIGKILL-orphan-sock state: a sock FILE exists at
    the canonical path but no listener is bound. The pre-2026-05-13
    file-stat ``is_alive`` lied to the helper here.
    """
    from fast_agent.spawn.agent_channel import _get_sock_dir, _sanitize_name

    sock_path = _get_sock_dir() / f"{_sanitize_name(agent_name)}.sock"
    if sock_path.exists():
        sock_path.unlink()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.bind(str(sock_path))
    finally:
        s.close()
    return sock_path


# ─── Real-channel-alive: keep-alive subprocess ───────────────────────


@pytest.mark.asyncio
async def test_real_channel_alive_with_fresh_idle_snapshot_returns_idle(
    isolated_channel_dir, tmp_path,
):
    """The happy-path keep-alive case, exercised against real
    everything: bind a real channel socket, write a fresh idle
    snapshot, ask the helper, assert ``idle``.
    """
    db = tmp_path / "snapshots.db"
    _init_snapshot_db(db)
    agent = "E2E_Agent_Alive_Idle"
    _write_snapshot(db, agent, "idle", created_at=time.time())

    channel = await _start_real_channel(agent)
    try:
        # last_active_at older than snapshot → fresh idle.
        record = {
            "agent_name": agent,
            "status": "running",
            "lifecycle": "resumable",
            "last_active_at": time.time() - 60,
        }
        assert _compute_effective_status(record, snapshots_db_path=str(db)) == "idle"
    finally:
        await channel.stop()


@pytest.mark.asyncio
async def test_real_channel_alive_with_stale_snapshot_keeps_running(
    isolated_channel_dir, tmp_path,
):
    """Real channel listening, but the bridge has bumped
    ``last_active_at`` more recently than the snapshot was written —
    agent is mid-turn. Helper must keep raw ``running``.
    """
    db = tmp_path / "snapshots.db"
    _init_snapshot_db(db)
    agent = "E2E_Agent_Alive_Active"
    # Snapshot from a turn that ended long ago.
    _write_snapshot(db, agent, "task_complete", created_at=time.time() - 300)

    channel = await _start_real_channel(agent)
    try:
        record = {
            "agent_name": agent,
            "status": "running",
            "lifecycle": "resumable",
            "last_active_at": time.time(),  # bridge just saw a thinking event
        }
        assert _compute_effective_status(record, snapshots_db_path=str(db)) == "running"
    finally:
        await channel.stop()


# ─── Real-channel-dead: lifecycle invariant ──────────────────────────


def test_real_channel_missing_resumable_idle_returns_idle(
    isolated_channel_dir, tmp_path,
):
    """No sock file at all + resumable + idle snapshot.

    The canonical "agile team finished its turn, subprocess hibernating"
    case the user surfaced on 2026-05-13. Helper must return ``idle``,
    matching ``spawn_progress_bridge`` / ``mark_stale_running``.
    """
    db = tmp_path / "snapshots.db"
    _init_snapshot_db(db)
    agent = "E2E_Agent_Resumable_Dead"
    _write_snapshot(db, agent, "idle", created_at=time.time())

    record = {
        "agent_name": agent,
        "status": "running",
        "lifecycle": "resumable",
        "last_active_at": 0,
    }
    # No sock file written for this agent — real is_alive returns False.
    assert _compute_effective_status(record, snapshots_db_path=str(db)) == "idle"


def test_real_orphan_sock_resumable_idle_returns_idle(
    isolated_channel_dir, tmp_path,
):
    """The exact SIGKILL trap from 2026-05-12. An orphan sock file
    exists on disk (bind+close without unlink) — pre-fix
    ``is_alive`` returned True from file-stat alone. With the
    connect-probe ``is_alive`` and the lifecycle-aware helper, the
    result must still be ``idle`` for a resumable team agent.
    """
    db = tmp_path / "snapshots.db"
    _init_snapshot_db(db)
    agent = "E2E_Agent_Orphan_Sock"
    _write_snapshot(db, agent, "idle", created_at=time.time())

    sock_path = _create_orphan_sock(agent)
    try:
        assert sock_path.exists(), "test setup: orphan sock missing"
        record = {
            "agent_name": agent,
            "status": "running",
            "lifecycle": "resumable",
            "last_active_at": 0,
        }
        assert _compute_effective_status(record, snapshots_db_path=str(db)) == "idle"
    finally:
        sock_path.unlink(missing_ok=True)


def test_real_channel_dead_oneshot_returns_completed(
    isolated_channel_dir, tmp_path,
):
    """Companion to the resumable test — oneshot must still terminalise
    as ``completed`` to keep the canonical lifecycle invariant.
    """
    db = tmp_path / "snapshots.db"
    _init_snapshot_db(db)
    agent = "E2E_Agent_Oneshot_Dead"
    _write_snapshot(db, agent, "task_complete", created_at=time.time())

    record = {
        "agent_name": agent,
        "status": "running",
        "lifecycle": "oneshot",
        "last_active_at": 0,
    }
    assert _compute_effective_status(record, snapshots_db_path=str(db)) == "completed"


def test_real_channel_dead_error_snapshot_returns_error(
    isolated_channel_dir, tmp_path,
):
    """Error snapshot supersedes lifecycle: regardless of resumable or
    oneshot, the helper must return ``error`` so the dashboard can
    flag the agent for inspection.
    """
    db = tmp_path / "snapshots.db"
    _init_snapshot_db(db)
    agent = "E2E_Agent_Error"
    _write_snapshot(db, agent, "error", created_at=time.time())

    for lifecycle in ("resumable", "oneshot", ""):
        record = {
            "agent_name": agent,
            "status": "running",
            "lifecycle": lifecycle,
            "last_active_at": 0,
        }
        out = _compute_effective_status(record, snapshots_db_path=str(db))
        assert out == "error", f"lifecycle={lifecycle!r}: got {out!r}"


# ─── Spawn race: channel not yet bound, no snapshot ──────────────────


def test_real_fresh_spawn_no_snapshot_keeps_raw_running(
    isolated_channel_dir, tmp_path,
):
    """Brand-new spawn: channel hasn't bound yet, no snapshot written.
    Helper must NOT misclassify this as terminal. It must trust raw
    (``running``) so the UI shows the still-initialising agent. The
    next backend restart's ``mark_stale_running`` will catch any true
    crashes.
    """
    db = tmp_path / "snapshots.db"
    _init_snapshot_db(db)  # empty DB, no rows
    record = {
        "agent_name": "E2E_Agent_Fresh_Spawn",
        "status": "running",
        "lifecycle": "resumable",
        "last_active_at": 0,
    }
    assert _compute_effective_status(record, snapshots_db_path=str(db)) == "running"


# ─── Canonical-value contract ────────────────────────────────────────


def test_real_helper_only_returns_canonical_statuses(
    isolated_channel_dir, tmp_path,
):
    """Sweep every combination that has well-defined behaviour and
    verify the helper only returns members of the canonical
    ``SpawnStatus`` set. Pre-fix the helper invented
    ``completed_unknown`` which broke downstream consumers.
    """
    from fast_agent.spawn.spawn_registry import SpawnStatus

    canonical = {s.value for s in SpawnStatus} | {
        # Also accept raw pass-throughs the helper may surface.
        "starting", "resumed", "paused", "unknown", "failed",
    }
    db = tmp_path / "snapshots.db"
    _init_snapshot_db(db)
    agent = "E2E_Agent_Sweep"

    matrix = [
        # (trigger, lifecycle, raw, last_active_at)
        ("idle", "resumable", "running", 0),
        ("idle", "oneshot", "running", 0),
        ("task_complete", "resumable", "running", 0),
        ("task_complete", "oneshot", "running", 0),
        ("error", "resumable", "running", 0),
        ("idle", "", "idle", 0),
        ("task_complete", "resumable", "running", time.time() * 2),  # stale snap
    ]
    for trigger, lifecycle, raw, last_active in matrix:
        # Use fresh DB per case so each test sees only its own snapshot.
        single_db = tmp_path / f"sweep_{trigger}_{lifecycle or 'none'}_{raw}.db"
        _init_snapshot_db(single_db)
        _write_snapshot(single_db, agent, trigger, created_at=time.time())
        record = {
            "agent_name": agent,
            "status": raw,
            "lifecycle": lifecycle,
            "last_active_at": last_active,
        }
        out = _compute_effective_status(record, snapshots_db_path=str(single_db))
        assert out in canonical, (
            f"Helper returned non-canonical value {out!r} for "
            f"trigger={trigger!r}, lifecycle={lifecycle!r}, raw={raw!r}, "
            f"last_active={last_active}"
        )
        assert out != "completed_unknown", (
            "completed_unknown is a deprecated, non-canonical state — "
            "the helper must not surface it."
        )
