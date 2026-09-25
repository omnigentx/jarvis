"""Reproducible context-cost sample for Atlassian MCP output projections.

Run with ``uv run --frozen python scripts/measure_atlassian_output.py``.
This uses synthetic content; live tenant results must be measured separately.
"""
from __future__ import annotations

import json
import importlib.util
from pathlib import Path

import tiktoken

_PROJECTION = (
    Path(__file__).resolve().parents[1] / "mcp-atlassian" / "src" /
    "mcp_atlassian" / "utils" / "response_projection.py"
)
_SPEC = importlib.util.spec_from_file_location("response_projection", _PROJECTION)
assert _SPEC and _SPEC.loader
_module = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_module)
brief_jira_search = _module.brief_jira_search
confluence_page_metadata = _module.confluence_page_metadata


def _tokens(value: object) -> int:
    payload = json.dumps(value, indent=2, ensure_ascii=False)
    return len(tiktoken.get_encoding("cl100k_base").encode(payload))


def main() -> None:
    issue_body = (
        "Acceptance criteria: export must preserve Unicode, ownership and "
        "audit history. Add tests for retries, conflicts and cancellation. "
    ) * 12
    search = {
        "total": 10,
        "start_at": 0,
        "max_results": 10,
        "issues": [
            {"id": str(i), "key": f"TEST-{i}", "summary": f"Task {i}",
             "status": {"name": "In Progress"}, "assignee": {"display_name": "PM"},
             "priority": {"name": "Medium"}, "updated": "2026-09-25",
             "issue_type": {"name": "Task"}, "labels": ["team"],
             "description": issue_body, "reporter": {"display_name": "Owner"},
             "created": "2026-09-01"}
            for i in range(1, 11)
        ],
    }
    page = {
        "id": "123", "title": "Design", "url": "https://example.atlassian.net/wiki/x/123",
        "version": 7, "updated": "2026-09-25", "attachments": [],
        "content": {"value": (issue_body + "\n") * 5, "format": "markdown"},
    }
    for name, original, compact in (
        ("jira_search_10", search, brief_jira_search(
            search, "https://example.atlassian.net")),
        ("confluence_get_page", page, confluence_page_metadata(page)),
    ):
        before, after = _tokens(original), _tokens(compact)
        print(f"{name}: {before} -> {after} tokens ({(before-after)/before:.1%} saved)")


if __name__ == "__main__":
    main()
