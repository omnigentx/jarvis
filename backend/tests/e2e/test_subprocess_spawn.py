"""E2E POC (subprocess variant): spawn a real Python subprocess running
isolated_runner with ScriptedLLM replacing the "playback" model class.

Proves the pattern that's the source of most production regressions (per the
existing `test_subprocess_env_vars.py` docstring): env var propagation + child
boot + LLM wiring across process boundaries.

Adding a new subprocess test case is 3 lines — see `run_scripted_subprocess`
in harness.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.e2e.harness import run_scripted_subprocess


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.slow
def test_subprocess_boots_with_scripted_llm(tmp_path: Path):
    result = run_scripted_subprocess(
        fixture_path=FIXTURES / "subprocess_simple_reply.yaml",
        task="say something",
        tmp_path=tmp_path,
    )

    assert result.returncode == 0, (
        f"subprocess exited with {result.returncode}\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    # Pin the single expected value — accepting {"completed", "success"} would
    # silently mask isolated_runner contract drift (it writes "completed").
    assert result.result["status"] == "completed", (
        f"Unexpected status: {result.result!r}"
    )
    assert "ack from scripted llm" in result.result.get("result", "")


@pytest.mark.slow
def test_subprocess_fails_loud_on_missing_fixture_env(tmp_path: Path):
    """subprocess_entry registers ScriptedLLM from ``PLAYBACK_FIXTURE_PATH``;
    an empty or missing value must produce a non-zero exit with the specific
    RuntimeError message — not a silent boot.
    """
    result = run_scripted_subprocess(
        fixture_path=FIXTURES / "subprocess_simple_reply.yaml",
        task="say something",
        tmp_path=tmp_path,
        extra_env={"PLAYBACK_FIXTURE_PATH": ""},
    )

    assert result.returncode != 0, (
        "Subprocess must exit non-zero when PLAYBACK_FIXTURE_PATH is empty.\n"
        f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    )
    assert "PLAYBACK_FIXTURE_PATH" in result.stderr, (
        "Stderr must name the missing env var so the failure is actionable.\n"
        f"STDERR:\n{result.stderr}"
    )
