"""The replay must use complete results and never emit source content."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts.measure_atlassian_multistep import _bounded_hits, measure


def _tool_result(call_id: str, tool: str, payload: object) -> dict[str, object]:
    return {
        "role": "user",
        "tool_results": {
            call_id: {
                "tool_name": tool,
                "content": [{"type": "text", "text": json.dumps(payload)}],
            }
        },
    }


def _snapshot_db(path: Path) -> None:
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE agent_context_snapshots (id INTEGER PRIMARY KEY, "
        "context_json TEXT NOT NULL)"
    )
    jira = {
        "total": 2,
        "start_at": 0,
        "max_results": 2,
        "issues": [
            {
                "id": "1", "key": "TEST-1", "summary": "First task",
                "description": "SECRET_ACCEPTANCE_CRITERION: preserve audit history",
            },
            {
                "id": "2", "key": "TEST-2", "summary": "Second task",
                "description": "Second task requirements",
            },
        ],
    }
    page = {
        "metadata": {
            "id": "42", "title": "Plan",
            "content": {"value": "Overview\nThe risk is stale data.\nDecision: refresh.\n"},
        }
    }
    messages = [
        _tool_result("jira", "mcp-atlassian__jira_search", jira),
        _tool_result("search", "mcp-atlassian__confluence_search", []),
        _tool_result("page", "mcp-atlassian__confluence_get_page", page),
    ]
    complete = json.dumps({"messages": messages})
    compacted_messages = json.loads(complete)["messages"]
    compacted_messages[-1]["tool_results"]["page"]["content"][0]["text"] = (
        '{"metadata": {"id": "42", "content": '
        "…[truncated by context compaction]"
    )
    truncated = json.dumps({"messages": compacted_messages})
    db.execute(
        "INSERT INTO agent_context_snapshots VALUES (?, ?)", (1, complete)
    )
    db.execute(
        "INSERT INTO agent_context_snapshots VALUES (?, ?)", (2, truncated)
    )
    db.commit()
    db.close()


def test_replay_uses_complete_snapshot_and_reports_no_source_text(tmp_path: Path) -> None:
    db_path = tmp_path / "history.db"
    _snapshot_db(db_path)

    report = measure(db_path)
    serialized = json.dumps(report)

    assert report["snapshot_id"] == 1
    assert [step["tool"] for step in report["observed_steps"]] == [
        "mcp-atlassian__jira_search",
        "mcp-atlassian__confluence_search",
        "mcp-atlassian__confluence_get_page",
    ]
    assert report["observed_steps"][-1]["cumulative_tool_output_tokens"] == sum(
        step["output_tokens"] for step in report["observed_steps"]
    )
    assert report["jira"]["descriptions_removed_by_brief"] == 2
    assert report["confluence"]["observed_page_reads"] == 1
    assert report["file_search_probe"]["source_sha256_matches"] is True
    assert report["file_search_probe"]["temporary_file_removed_on_exit"] is True
    assert "SECRET_ACCEPTANCE_CRITERION" not in serialized
    assert "stale data" not in serialized


def test_snapshot_id_rejects_truncated_result(tmp_path: Path) -> None:
    db_path = tmp_path / "history.db"
    _snapshot_db(db_path)

    with pytest.raises(ValueError, match="No complete"):
        measure(db_path, snapshot_id=2)


def test_bounded_search_limits_hits_and_retains_line_numbers() -> None:
    text = "\n".join(
        ["intro", "risk one", "detail", "middle", "risk two", "end", "risk three"]
    )
    snippet = _bounded_hits(text, "RISK", limit=2)

    assert "2: risk one" in snippet
    assert "5: risk two" in snippet
    assert "7: risk three" not in snippet
