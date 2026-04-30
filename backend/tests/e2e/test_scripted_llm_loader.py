"""Loader validation for ScriptedLLM fixtures.

Rejecting shape errors at load time (not at the first ``.items()`` call)
makes typos in fixtures produce a named, actionable error instead of an
opaque ``AttributeError`` deep in the playback loop.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tests.e2e.scripted_llm import ScriptedLLM


def _write_fixture(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "fixture.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def test_tool_calls_list_rejected(tmp_path):
    """Common typo: ``tool_calls: [...]`` instead of ``tool_calls: {c1: ...}``."""
    fx = _write_fixture(tmp_path, {
        "turns": [
            {"tool_calls": [{"name": "foo", "arguments": {}}]},
        ],
    })
    with pytest.raises(ValueError, match="tool_calls must be a mapping"):
        ScriptedLLM.load_turns_from_yaml(fx)


def test_tool_call_entry_not_mapping_rejected(tmp_path):
    """Each ``tool_calls[cid]`` must itself be a mapping, not a bare string."""
    fx = _write_fixture(tmp_path, {
        "turns": [{"tool_calls": {"c1": "foo"}}],
    })
    with pytest.raises(ValueError, match="must be a mapping"):
        ScriptedLLM.load_turns_from_yaml(fx)


def test_turn_not_mapping_rejected(tmp_path):
    """A stray scalar in the turns list (e.g. copy-paste error) must fail loud."""
    fx = _write_fixture(tmp_path, {"turns": ["oops just a string"]})
    with pytest.raises(ValueError, match="turn\\[0\\] must be a mapping"):
        ScriptedLLM.load_turns_from_yaml(fx)


def test_utf8_fixture_loads(tmp_path):
    """Vietnamese content must survive a non-UTF-8 default locale."""
    fx = _write_fixture(tmp_path, {
        "turns": [{"content": "Đang phát Tây Du Ký chương 3."}],
    })
    turns = ScriptedLLM.load_turns_from_yaml(fx)
    assert turns[0]["content"] == "Đang phát Tây Du Ký chương 3."
