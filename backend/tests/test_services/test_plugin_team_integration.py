"""Install into an already-running child and prove actual capability use."""

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine

from services.plugins.ipc import request_update, socket_path
from services.plugins.lifecycle import PluginLifecycle
from services.plugins.targets import team_binding


async def evidence(process):
    async with asyncio.timeout(20):
        while line := await process.stdout.readline():
            if line.startswith(b"EVIDENCE:"):
                return json.loads(line[len("EVIDENCE:") :])
    raise AssertionError("Child exited without runtime evidence")


@pytest.mark.asyncio
async def test_skill_install_use_remove_in_same_live_child_process(tmp_path):
    database = tmp_path / "jarvis.db"
    engine = create_engine("sqlite:///" + str(database))
    lifecycle = PluginLifecycle(engine, tmp_path / "plugins")
    source = tmp_path / "source"
    (source / "skills/review").mkdir(parents=True)
    (source / "plugin.json").write_text('{"name":"child-review"}')
    (source / "skills/review/SKILL.md").write_text(
        "---\nname: review\ndescription: Review changes\n---\nCHILD_PLUGIN_MARKER"
    )
    candidate = lifecycle.stage(
        source, repo="openai/plugins", commit="a" * 40, subdirectory=""
    )
    record = {
        "run_id": str(uuid.uuid4()),
        "session_id": "session-evidence",
        "agent_name": "ChildEvidence",
    }
    binding = team_binding(record)
    root = Path(__file__).resolve().parents[2]
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
    try:
        initial = await evidence(process)

        async def apply(root, package, target):
            assert target == binding
            return await request_update(
                socket_path(str(database), record["run_id"]),
                run_id=record["run_id"],
                candidate=candidate["id"],
                digest=package.digest,
                operation="activate",
            )

        result = await lifecycle.activate(
            candidate["id"], binding, approve=AsyncMock(return_value=True), apply=apply
        )
        assert result["status"] == "ready"
        process.stdin.write(b"inspect\n")
        await process.stdin.drain()
        installed = await evidence(process)
        assert installed["identity"] == initial["identity"]
        assert installed["skills"] == ["child-review:review"]
        assert "CHILD_PLUGIN_MARKER" in installed["skill_body"]

        async def remove(root, package, target):
            return await request_update(
                socket_path(str(database), record["run_id"]),
                run_id=record["run_id"],
                candidate=candidate["id"],
                digest=package.digest,
                operation="deactivate",
            )

        assert (await lifecycle.deactivate(candidate["id"], binding, remove=remove))[
            "status"
        ] == "disabled"
        process.stdin.write(b"inspect\n")
        await process.stdin.drain()
        removed = await evidence(process)
        assert removed["identity"] == initial["identity"]
        assert removed["skills"] == []
    finally:
        if process.returncode is None:
            process.stdin.write(b"quit\n")
            await process.stdin.drain()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await process.wait()
        stderr = await process.stderr.read()
        engine.dispose()
        assert process.returncode == 0, stderr.decode()
