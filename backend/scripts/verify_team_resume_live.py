"""Opt-in live LLM acceptance for team resume; isolated DB/workspace, no production writes."""

from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid


async def main() -> None:
    import yaml

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--secrets", type=Path, required=True)
    parser.add_argument("--venv", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", default="openai.coding-agent")
    args = parser.parse_args()
    backend = Path(__file__).resolve().parents[1]
    root = Path(tempfile.mkdtemp(prefix="jarvis-live-resume-"))
    args.output.mkdir(parents=True, exist_ok=True)
    # Reuse dependencies without syncing or changing the existing environment.
    for name in ("core", "services", "tools", "models", "helpers"):
        if (backend / name).exists():
            (root / name).symlink_to(backend / name, target_is_directory=True)
    (root / "fast_agent").symlink_to(
        backend / "fast-agent/src/fast_agent", target_is_directory=True
    )
    (root / "pyproject.toml").write_text(
        '[project]\nname="resume-acceptance"\nversion="0.0.0"\nrequires-python=">=3.11"\n'
    )
    secrets = yaml.safe_load(args.secrets.read_text())
    (root / "fastagent.secrets.yaml").write_text(
        yaml.safe_dump({"openai": secrets["openai"]})
    )
    (root / "fastagent.secrets.yaml").chmod(0o600)
    servers = {
        name: {
            "command": "python",
            "args": ["-m", f"fast_agent.spawn.servers.{name}_server"],
        }
        for name in ("email", "meeting_room")
    }
    (root / "fastagent.config.yaml").write_text(
        yaml.safe_dump({"default_model": args.model, "mcp": {"servers": servers}})
    )
    db_path = root / "data/jarvis.db"
    db_path.parent.mkdir()
    os.environ.update(
        SPAWN_PROJECT_DIR=str(root),
        SPAWN_REGISTRY_DB=str(db_path),
        JARVIS_DB_PATH=str(db_path),
        UV_PROJECT_ENVIRONMENT=str(args.venv),
        UV_NO_SYNC="1",
        SPAWN_EVENT_SOCKET=str(root / "events.sock"),
        ENVIRONMENT_DIR=str(root / ".fast-agent"),
    )
    sys.path[:0] = [str(backend / "fast-agent/src"), str(backend)]
    from core.database import init_db

    init_db()
    from core.agent_registry_db import AgentRegistryDB
    from services.sse_progress import progress_manager
    from services.spawn_progress_bridge import SpawnProgressBridge
    from services.spawn_event_socket import SpawnEventSocketServer
    from fast_agent.spawn.servers import agent_spawner_server as srv
    from fast_agent.spawn import isolated_spawner as spawner
    from fast_agent.spawn.agent_channel import AgentChannel
    from fast_agent.spawn.team_spawner import get_team_session

    events = []
    changed = asyncio.Event()

    class CaptureBridge(SpawnProgressBridge):
        def process_event(self, raw_line: str) -> None:
            super().process_event(raw_line)
            event = json.loads(raw_line)
            events.append(event)
            changed.set()
            if event.get("event_type") in ("idle", "error", "response"):
                print(
                    json.dumps(
                        {
                            k: event.get(k)
                            for k in ("event_type", "agent_name", "run_id", "data")
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

    bridge = CaptureBridge(progress_manager, registry_db=AgentRegistryDB())
    server = SpawnEventSocketServer(str(root / "events.sock"), bridge)
    await server.start()

    async def wait_stage(names: list[str], marker: str, cursor: int) -> dict[str, str]:
        async with asyncio.timeout(180):
            while True:
                changed.clear()
                stage = events[cursor:]
                responses = {
                    e["agent_name"]: e["data"].get("text", "")
                    for e in stage
                    if e.get("event_type") == "response"
                    and marker in e.get("data", {}).get("text", "")
                }
                idle = {e["agent_name"] for e in stage if e.get("event_type") == "idle"}
                if set(names) <= responses.keys() and set(names) <= idle:
                    # Let completion-triggered orchestrator work register before
                    # deciding the entire team is idle. Wait on the next event if busy.
                    await asyncio.sleep(0)
                    records = [srv._registry.find_by_name(n) for n in names]
                    pm = next(n for n in names if "[PM]" in n)
                    worker_idle = max(
                        (
                            i
                            for i, e in enumerate(stage)
                            if e.get("event_type") == "idle"
                            and e.get("agent_name") != pm
                        ),
                        default=-1,
                    )
                    pm_reply = max(
                        (
                            i
                            for i, e in enumerate(stage)
                            if e.get("event_type") == "response"
                            and e.get("agent_name") == pm
                            and marker in e.get("data", {}).get("text", "")
                        ),
                        default=-1,
                    )
                    if (
                        all(r and r.status == "idle" for r in records)
                        and pm_reply > worker_idle
                    ):
                        return {n: responses[n] for n in names}
                errors = [e for e in stage if e.get("event_type") == "error"]
                if errors:
                    raise RuntimeError(str(errors[-1])[:1000])
                await changed.wait()

    started = time.time()
    report = {
        "model": args.model,
        "runtime_dir": str(root),
        "started_at": started,
        "stages": [],
    }
    seed = "MEMORY-" + uuid.uuid4().hex[:10]
    instruction = (
        "You are {agent_name}, a member of an isolated runtime acceptance team. "
        "Do not use tools, spawn others, or write files. Follow each new task literally. "
        "Remember the secret marker provided in the initial brief across future turns. "
        "On each task output ONLY the requested stage word and the original secret marker. "
        "Ignore generic workflow and roster advice; this is a memory continuity exercise."
    )
    templates = root / "team_templates"
    templates.mkdir()
    template = {
        "name": "resume-acceptance",
        "orchestrator": "pm",
        "roles": {
            role: {
                "role_display": role.upper(),
                "instruction": instruction,
                "servers": [],
                "skills": [],
                "model": args.model,
            }
            for role in ("pm", "dev")
        },
    }
    (templates / "resume-acceptance.yaml").write_text(yaml.safe_dump(template))
    try:
        cursor = len(events)
        created = json.loads(
            await srv.spawn_team_tool(
                "resume-acceptance",
                f"Remember {seed}. Output INITIAL and that marker.",
                team_name="live-resume-acceptance",
            )
        )
        assert "session_id" in created, created
        sid = created["session_id"]
        members = json.loads(
            await srv.spawn_team_members(
                "dev", sid, f"Remember {seed}. Output INITIAL and that marker."
            )
        )
        print("SPAWN", json.dumps({"team": created, "members": members}), flush=True)
        session = get_team_session(sid)
        names = list(session.agents)
        initial = await wait_stage(names, "INITIAL", cursor)
        assert all(seed in value for value in initial.values()), initial
        before = {
            n: srv._registry.get_latest(i["run_id"]).to_dict()
            for n, i in session.agents.items()
        }
        report.update(session_id=sid, agents=names)
        report["stages"].append(
            {
                "stage": "initial",
                "responses": initial,
                "run_ids": {n: r["run_id"] for n, r in before.items()},
            }
        )

        cursor = len(events)
        live = json.loads(
            await srv.resume_team_tool(
                sid, "Output LIVE and the original secret marker from the first task."
            )
        )
        assert live["queued_agents"] == 2 and live["resumed_agents"] == 0, live
        response = await wait_stage(names, "LIVE", cursor)
        assert all(seed in value for value in response.values()), response
        for name, record in before.items():
            assert srv._registry.get_latest(record["run_id"]).run_id == record["run_id"]
            assert AgentChannel.is_alive(name, session_id=sid, run_id=record["run_id"])
        report["stages"].append(
            {"stage": "live_resume", "tool_result": live, "responses": response}
        )

        # Stop only this harness's child processes, then await their exit.
        import psutil

        runs = list(spawner._background_processes)
        for rid in runs:
            proc = psutil.Process(spawner._background_processes[rid].pid)
            for child in proc.children(recursive=True):
                child.terminate()
            proc.terminate()
        await asyncio.gather(
            *(
                spawner._background_tasks[r]
                for r in runs
                if r in spawner._background_tasks
            ),
            return_exceptions=True,
        )
        assert all(
            not AgentChannel.is_alive(n, session_id=sid, run_id=before[n]["run_id"])
            for n in names
        )
        cursor = len(events)
        restored = json.loads(
            await srv.resume_team_tool(
                sid,
                "Output RESTORED and the original secret marker from the first task.",
            )
        )
        assert restored["resumed_agents"] == 2, restored
        response = await wait_stage(names, "RESTORED", cursor)
        assert all(seed in value for value in response.values()), response
        current = get_team_session(sid)
        assert set(current.agents) == set(names)
        assert current.workspace == session.workspace
        for name in names:
            assert current.agents[name]["run_id"] != before[name]["run_id"]
        report["stages"].append(
            {"stage": "dead_resume", "tool_result": restored, "responses": response}
        )
        report["status"] = "passed"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = repr(exc)
        raise
    finally:
        report["duration_seconds"] = round(time.time() - started, 2)
        acceptance_status = report.get("status", "failed")
        report["status"] = "cleanup_pending"
        (args.output / "live-result.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2)
        )
        (args.output / "live-events.json").write_text(
            json.dumps(events, ensure_ascii=False, indent=2)
        )
        for process in list(spawner._background_processes.values()):
            if process.returncode is None:
                process.terminate()
        tasks = list(spawner._background_tasks.values())
        if tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), 15
                )
            except asyncio.TimeoutError:
                for process in list(spawner._background_processes.values()):
                    if process.returncode is None:
                        process.kill()
        # Terminate any runner orphaned by a uv launcher, scoped to this test root.
        import psutil

        for proc in psutil.process_iter():
            try:
                if (
                    proc.pid != os.getpid()
                    and Path(proc.environ().get("SPAWN_PROJECT_DIR", "/")).resolve()
                    == root.resolve()
                ):
                    proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        from fast_agent.spawn import spawn_events

        for connection in (srv._event_socket, spawn_events._direct_socket):
            if connection is not None:
                connection.close()
        for credential_file in root.rglob("fastagent.secrets.yaml"):
            credential_file.unlink(missing_ok=True)
        await asyncio.wait_for(server.stop(), 10)
        report["cleanup_passed"] = True
        report["status"] = acceptance_status
        (args.output / "live-result.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2)
        )
        print("RESULT", json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
