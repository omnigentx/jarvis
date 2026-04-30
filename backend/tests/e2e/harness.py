"""Helpers to run an E2E test: in-process ToolAgent and real subprocess spawn.

Usage — in-process:

    recorder = ToolCallRecorder()
    agent = await build_scripted_agent(
        fixture_path="fixtures/foo.yaml",
        tools=[my_tool_fn],
        recorder=recorder,
    )
    final = await agent.generate("user input")
    recorder.assert_matches([("my_tool_fn", {"arg": "value"})])

Usage — subprocess:

    result = run_scripted_subprocess(
        fixture_path="fixtures/bar.yaml",
        task="user input",
        tmp_path=tmp_path,
    )
    assert result["status"] == "completed"
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from fast_agent.agents.agent_types import AgentConfig
from fast_agent.agents.tool_agent import ToolAgent
from fast_agent.agents.tool_runner import ToolRunner, ToolRunnerHooks
from fast_agent.mcp.prompt_message_extended import PromptMessageExtended

from tests.e2e.scripted_llm import ScriptedLLM


BACKEND_DIR = Path(__file__).resolve().parent.parent.parent


# ────────────────────────────────────────────────────────────────────
# Text extraction helpers — shared across tests to avoid copy-paste drift.
# ────────────────────────────────────────────────────────────────────


def first_tool_result_text(msg) -> str:
    """Return the first text block inside ``msg.tool_results``.

    Used by tests that assert on a specific tool's JSON/plain-text payload.
    Raises if the message has no text — an empty tool result would otherwise
    masquerade as a passing assertion.
    """
    for result in msg.tool_results.values():
        for block in result.content:
            text = getattr(block, "text", None)
            if text is not None:
                return text
    raise AssertionError(f"No text block in tool_results of {msg}")


def flatten_text(messages) -> str:
    """Concatenate every text block across a list of messages.

    Walks both ``msg.content`` (assistant/user text) and ``msg.tool_results``
    so hook-injected reminders and tool payloads both appear. Join with
    newlines so substring assertions stay readable.
    """
    chunks: list[str] = []
    for msg in messages:
        for block in msg.content:
            t = getattr(block, "text", None)
            if t:
                chunks.append(t)
        if msg.tool_results:
            for res in msg.tool_results.values():
                for block in res.content:
                    t = getattr(block, "text", None)
                    if t:
                        chunks.append(t)
    return "\n".join(chunks)


@dataclass
class RecordedCall:
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolCallRecorder:
    """Captures every tool call the agent issues (via before_tool_call hook)."""

    calls: list[RecordedCall] = field(default_factory=list)

    async def hook(self, runner: ToolRunner, request: PromptMessageExtended) -> None:
        for _cid, tc in (request.tool_calls or {}).items():
            self.calls.append(
                RecordedCall(
                    name=tc.params.name,
                    arguments=dict(tc.params.arguments or {}),
                )
            )

    def assert_matches(self, expected: Sequence[tuple[str, dict[str, Any]]]) -> None:
        """Strict match: (name, arguments) tuples in order. Fails loud on any diff."""
        actual = [(c.name, c.arguments) for c in self.calls]
        assert actual == list(expected), (
            f"Tool call sequence mismatch.\n"
            f"  expected: {list(expected)}\n"
            f"  actual:   {actual}"
        )


async def build_scripted_agent(
    *,
    fixture_path: str | Path,
    tools: Sequence[Callable],
    agent_name: str = "e2e_agent",
    instruction: str = "You are a test agent.",
    recorder: ToolCallRecorder | None = None,
) -> ToolAgent:
    """Build a ToolAgent whose LLM replays the given fixture.

    The agent is wired for in-process execution (no MCP server spawn).
    """
    agent = ToolAgent(
        config=AgentConfig(
            name=agent_name,
            instruction=instruction,
            servers=[],
            human_input=False,
        ),
        tools=list(tools),
        context=None,
    )

    agent._llm = ScriptedLLM.from_yaml(fixture_path)

    if recorder is not None:
        agent.tool_runner_hooks = ToolRunnerHooks(before_tool_call=recorder.hook)

    return agent


# ────────────────────────────────────────────────────────────────────
# Subprocess E2E helper
# ────────────────────────────────────────────────────────────────────


def _init_spawn_registry_db(db_path: Path) -> None:
    """Create the bare minimum tables a spawned agent needs to save its context."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS agent_context_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                session_id TEXT,
                team_name TEXT,
                context_json TEXT NOT NULL,
                message_count INTEGER DEFAULT 0,
                total_input_tokens INTEGER DEFAULT 0,
                total_output_tokens INTEGER DEFAULT 0,
                trigger TEXT DEFAULT 'manual',
                created_at REAL NOT NULL
            )"""
        )
        conn.commit()
    finally:
        conn.close()


@dataclass
class SubprocessResult:
    returncode: int
    stdout: str
    stderr: str
    result: dict[str, Any]
    db_path: Path

    def context_snapshots(self) -> list[dict[str, Any]]:
        """Read every agent_context_snapshot row written during the run."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT agent_name, session_id, team_name, message_count, "
                "trigger, context_json FROM agent_context_snapshots "
                "ORDER BY created_at"
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]


def run_scripted_subprocess(
    *,
    fixture_path: str | Path,
    task: str,
    tmp_path: Path,
    instruction: str = "You are a test agent.",
    servers: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
    timeout: int = 60,
) -> SubprocessResult:
    """Launch a real isolated_runner subprocess with ScriptedLLM replacing 'playback'.

    Returns a SubprocessResult with stdout/stderr, the parsed result JSON, and
    a helper to read context snapshots out of the temp SQLite DB. Any missing
    env var that the spawned subprocess relies on will surface here as a
    non-zero exit + stderr — catching the silent-failure mode the real
    subprocess pipeline is prone to.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    db_path = tmp_path / "jarvis.db"
    _init_spawn_registry_db(db_path)

    (workspace / "fastagent.config.yaml").write_text("default_model: playback\n")

    config = {
        "task": task,
        "instruction": instruction,
        "context": "",
        "servers": servers or [],
        "model": "playback",
        "depth": 1,
        "max_depth": 1,
        "workspace_dir": str(workspace),
        "role": "test_role",
        "lifecycle": "oneshot",
        "result_file": str(tmp_path / "result.json"),
    }
    config_path = tmp_path / "handoff.json"
    config_path.write_text(json.dumps(config))

    env = {
        **os.environ,
        "SPAWN_REGISTRY_DB": str(db_path),
        "SPAWN_PROJECT_DIR": str(workspace),
        "PLAYBACK_FIXTURE_PATH": str(fixture_path),
        "PYTHONPATH": str(BACKEND_DIR),
    }
    if extra_env:
        env.update(extra_env)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.e2e.subprocess_entry",
            "--config",
            str(config_path),
            "--project-dir",
            str(workspace),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=str(BACKEND_DIR),
    )

    # If the subprocess exited cleanly, isolated_runner must have written
    # result.json — absence would be a silent-success bug. If it crashed
    # (returncode != 0), the caller inspects stderr/returncode directly; we
    # don't double-assert here because some tests deliberately exercise the
    # crash path.
    result_path = tmp_path / "result.json"
    if proc.returncode == 0:
        assert result_path.exists(), (
            f"Subprocess exited 0 but did not write {result_path.name} — "
            f"isolated_runner contract broken.\n"
            f"stderr:\n{proc.stderr}\nstdout:\n{proc.stdout}"
        )
    result: dict[str, Any] = {}
    if result_path.exists():
        result = json.loads(result_path.read_text(encoding="utf-8"))

    return SubprocessResult(
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        result=result,
        db_path=db_path,
    )
