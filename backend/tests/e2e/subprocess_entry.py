"""Subprocess entrypoint for E2E tests.

Launched instead of `python -m fast_agent.spawn.isolated_runner` so we can
(a) register ScriptedLLM as the "playback" model class before fast-agent boots,
(b) then hand control to the real isolated_runner.main().

Env vars read here:
  PLAYBACK_FIXTURE_PATH — absolute path to a YAML fixture that ScriptedLLM loads.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Ensure backend/ is importable before touching fast_agent
_backend_dir = str(Path(__file__).resolve().parent.parent.parent)
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from fast_agent.llm.model_factory import ModelFactory  # noqa: E402
from tests.e2e.scripted_llm import ScriptedLLM  # noqa: E402


def _make_fixture_loaded_scripted_llm_cls(fixture_path: str) -> type:
    """Create a ScriptedLLM subclass that loads `fixture_path` on __init__.

    Needed because ModelFactory instantiates the LLM class with no fixture args.
    Validation is done once at class-creation time (now) via the strict loader
    so a malformed fixture crashes the subprocess immediately rather than
    silently running with zero turns.
    """
    validated_turns = ScriptedLLM.load_turns_from_yaml(fixture_path)

    class _FixtureLoadedScriptedLLM(ScriptedLLM):
        def __init__(self, **kwargs):
            super().__init__(turns=validated_turns, **kwargs)

    _FixtureLoadedScriptedLLM.__name__ = "FixtureLoadedScriptedLLM"
    return _FixtureLoadedScriptedLLM


def register_scripted_playback() -> None:
    fixture_path = os.environ.get("PLAYBACK_FIXTURE_PATH")
    if not fixture_path:
        raise RuntimeError(
            "subprocess_entry requires PLAYBACK_FIXTURE_PATH env var"
        )
    cls = _make_fixture_loaded_scripted_llm_cls(fixture_path)
    ModelFactory.MODEL_SPECIFIC_CLASSES["playback"] = cls


if __name__ == "__main__":
    register_scripted_playback()

    from fast_agent.spawn.isolated_runner import main as _runner_main

    asyncio.run(_runner_main())
