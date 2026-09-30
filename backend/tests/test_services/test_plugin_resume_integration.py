"""A resumed OS process restores reviewed skills; stale inventory is rejected."""

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine

from routes.plugins import verified_record
from services import shared_state
from services.plugins.ipc import request_update, socket_path
from services.plugins.lifecycle import PluginLifecycle
from services.plugins.targets import team_binding


async def read_evidence(process):
    async with asyncio.timeout(15):
        while line := await process.stdout.readline():
            if line.startswith(b"EVIDENCE:"):
                return json.loads(line[len("EVIDENCE:") :])
    raise AssertionError("No child runtime evidence")


async def stop(process):
    if process.returncode is None:
        process.stdin.write(b"quit\n")
        await process.stdin.drain()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except TimeoutError:
            process.kill()
            await process.wait()


@pytest.mark.asyncio
async def test_resume_restores_binding_and_inventory_requires_live_ack(
    tmp_path, monkeypatch
):
    database = tmp_path / "jarvis.db"
    engine = create_engine("sqlite:///" + str(database))
    lifecycle = PluginLifecycle(engine, tmp_path / "plugins")
    source = tmp_path / "source"
    (source / "skills/review").mkdir(parents=True)
    (source / "plugin.json").write_text('{"name":"resume-review"}')
    (source / "skills/review/SKILL.md").write_text(
        "---\nname: review\ndescription: Review\n---\nRESUMED_MARKER"
    )
    candidate = lifecycle.stage(
        source, repo="openai/plugins", commit="a" * 40, subdirectory=""
    )
    root = Path(__file__).resolve().parents[2]
    record = {
        "run_id": str(uuid.uuid4()),
        "session_id": "resume-session",
        "agent_name": "ResumeEvidence",
        "status": "running",
    }
    binding = team_binding(record)
    monkeypatch.setattr(
        shared_state,
        "registry_db",
        SimpleNamespace(get_all=lambda: {record["run_id"]: record}),
    )
    installer = SimpleNamespace(lifecycle=lifecycle)

    async def launch():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(root / "tests/fixtures/plugin_child_runtime.py"),
            json.dumps(record),
            env={
                **os.environ,
                "SPAWN_REGISTRY_DB": str(database),
                "PYTHONPATH": str(root) + ":" + str(root / "fast-agent/src"),
            },
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await read_evidence(process)
        return process

    process = await launch()
    try:

        async def apply(root, package, target):
            return await request_update(
                socket_path(str(database), record["run_id"]),
                run_id=record["run_id"],
                candidate=candidate["id"],
                digest=package.digest,
                operation="activate",
            )

        installed = await lifecycle.activate(
            candidate["id"], binding, approve=AsyncMock(return_value=True), apply=apply
        )
        assert (await verified_record(installed, installer))["status"] == "ready"
        await stop(process)
        assert (await verified_record(lifecycle.get(candidate["id"]), installer))[
            "status"
        ] == "needs_reactivation"
        record["run_id"] = str(uuid.uuid4())
        process = await launch()
        assert (await verified_record(lifecycle.get(candidate["id"]), installer))[
            "status"
        ] == "ready"
        process.stdin.write(b"inspect\n")
        await process.stdin.drain()
        restored = await read_evidence(process)
        assert restored["skills"] == ["resume-review:review"]
        assert "RESUMED_MARKER" in restored["skill_body"]
    finally:
        await stop(process)
        engine.dispose()
        assert process.returncode == 0, (await process.stderr.read()).decode()
