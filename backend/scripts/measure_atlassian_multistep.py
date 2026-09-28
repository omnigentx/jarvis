"""Replay complete historical Atlassian tool results without exporting content.

Example::

    uv run --frozen python scripts/measure_atlassian_multistep.py \
        --db /path/to/jarvis.db

The report counts tool-output tokens. Follow-up Jira issue reads and local
file searches are explicitly modeled; it does not claim LLM billing savings
or answer-quality equivalence. The database is opened read-only and no source
content is printed or copied into the repository.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

import tiktoken


def _projection_functions() -> tuple[Any, Any]:
    path = (
        Path(__file__).resolve().parents[1]
        / "mcp-atlassian/src/mcp_atlassian/utils/response_projection.py"
    )
    spec = importlib.util.spec_from_file_location("atlassian_projection", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load Atlassian projection: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.brief_jira_search, module.confluence_page_metadata


def _tokens(value: Any, encoder: Any) -> int:
    return len(encoder.encode(json.dumps(value, indent=2, ensure_ascii=False)))


def _results(
    messages: list[dict[str, Any]],
) -> tuple[dict[str, list[Any]], list[tuple[str, Any]]]:
    by_tool: dict[str, list[Any]] = {}
    ordered: list[tuple[str, Any]] = []
    seen_ids: set[str] = set()
    for message in messages:
        for call_id, result in (message.get("tool_results") or {}).items():
            if call_id in seen_ids or not isinstance(result, dict):
                continue
            tool = result.get("tool_name", "")
            if tool not in {
                "mcp-atlassian__jira_search",
                "mcp-atlassian__confluence_search",
                "mcp-atlassian__confluence_get_page",
            }:
                continue
            blocks = result.get("content") or []
            if not blocks or not isinstance(blocks[0], dict):
                continue
            try:
                parsed = json.loads(blocks[0]["text"])
            except (KeyError, TypeError, json.JSONDecodeError):
                continue  # Later context-compacted snapshots can truncate JSON.
            seen_ids.add(call_id)
            by_tool.setdefault(tool, []).append(parsed)
            ordered.append((tool, parsed))
    return by_tool, ordered


def _snapshot(
    db_path: Path, snapshot_id: int | None,
) -> tuple[int, dict[str, list[Any]], list[tuple[str, Any]]]:
    connection = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    try:
        if snapshot_id is not None:
            rows = connection.execute(
                "SELECT id, context_json FROM agent_context_snapshots WHERE id = ?",
                (snapshot_id,),
            )
        else:
            rows = connection.execute(
                "SELECT id, context_json FROM agent_context_snapshots "
                "WHERE context_json LIKE '%mcp-atlassian__jira_search%' "
                "AND context_json LIKE '%mcp-atlassian__confluence_get_page%' "
                "ORDER BY id DESC"
            )
        for row_id, raw in rows:
            try:
                found, ordered = _results(json.loads(raw)["messages"])
            except (TypeError, KeyError, json.JSONDecodeError):
                continue
            if found.get("mcp-atlassian__jira_search") and found.get(
                "mcp-atlassian__confluence_get_page"
            ):
                return row_id, found, ordered
    finally:
        connection.close()
    raise ValueError("No complete Jira and Confluence tool results in snapshot DB")


def _bounded_hits(body: str, query: str, *, limit: int = 3) -> str:
    """Model a bounded file-content search, preserving matched source lines."""
    lines = body.splitlines()
    hits = [i for i, line in enumerate(lines) if query.casefold() in line.casefold()]
    picked: set[int] = set()
    for hit in hits[:limit]:
        picked.update(range(max(0, hit - 1), min(len(lines), hit + 2)))
    return "\n".join(f"{i + 1}: {lines[i]}" for i in sorted(picked))


def _cache_probe(body: str, query: str, encoder: Any) -> dict[str, Any]:
    """Measure exact file retention and bounded local search; delete on exit."""
    with tempfile.TemporaryDirectory(prefix="atlassian-replay-") as directory:
        path = Path(directory) / "page.txt"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(body)
        read_back = path.read_text(encoding="utf-8")
        snippet = _bounded_hits(read_back, query)
        result = {
            "query": query,
            "source_sha256_matches": hashlib.sha256(body.encode()).digest()
            == hashlib.sha256(read_back.encode()).digest(),
            "query_found": query.casefold() in read_back.casefold(),
            "snippet_contains_query": query.casefold() in snippet.casefold(),
            "snippet_tokens": len(encoder.encode(snippet)),
            "body_tokens": len(encoder.encode(body)),
        }
    result["temporary_file_removed_on_exit"] = not path.exists()
    return result


def measure(db_path: Path, snapshot_id: int | None = None) -> dict[str, Any]:
    row_id, found, ordered = _snapshot(db_path, snapshot_id)
    brief_jira_search, confluence_page_metadata = _projection_functions()
    encoder = tiktoken.get_encoding("cl100k_base")
    cumulative = 0
    observed_steps = []
    for tool, payload in ordered:
        count = _tokens(payload, encoder)
        cumulative += count
        observed_steps.append(
            {
                "step": len(observed_steps) + 1,
                "tool": tool,
                "output_tokens": count,
                "cumulative_tool_output_tokens": cumulative,
            }
        )

    jira = found["mcp-atlassian__jira_search"][0]
    issues = jira.get("issues", [])
    brief = brief_jira_search(jira, "https://example.atlassian.net")
    jira_full = _tokens(jira, encoder)
    jira_brief = _tokens(brief, encoder)
    jira_steps = []
    for selected in sorted({1, 2, 3, 5, len(issues)}):
        if not selected or selected > len(issues):
            continue
        # This is an optimistic proxy: real jira_get_issue may return more.
        followup = sum(_tokens({"issue": issue}, encoder) for issue in issues[:selected])
        jira_steps.append(
            {
                "selected_issues": selected,
                "full_search_tokens": jira_full,
                "brief_plus_issue_proxy_tokens": jira_brief + followup,
                "proxy_delta_vs_full": jira_brief + followup - jira_full,
                "brief_tool_calls": 1 + selected,
                "full_search_tool_calls": 1,
            }
        )

    pages = [
        result["metadata"]
        for result in found["mcp-atlassian__confluence_get_page"]
        if isinstance(result, dict) and isinstance(result.get("metadata"), dict)
    ]
    page_full = [_tokens({"metadata": page}, encoder) for page in pages]
    page_meta = [
        _tokens({"metadata": confluence_page_metadata(page)}, encoder)
        for page in pages
    ]
    search_results = found.get("mcp-atlassian__confluence_search", [])
    search_tokens = _tokens(search_results[0], encoder) if search_results else None
    confluence_steps = []
    for selected in sorted({1, 2, 3, len(pages)}):
        if not selected or selected > len(pages):
            continue
        # Compare the same selected pages. Metadata is overhead if bodies
        # are required, even if it can help in an earlier discovery phase.
        confluence_steps.append(
            {
                "selected_pages": selected,
                "search_plus_full_tokens": (search_tokens or 0) + sum(page_full[:selected]),
                "search_plus_metadata_then_full_tokens": (
                    (search_tokens or 0)
                    + sum(page_meta[:selected])
                    + sum(page_full[:selected])
                ),
                "metadata_extra_tool_calls": selected,
                "metadata_extra_upstream_full_page_fetches": selected,
            }
        )

    cache = None
    if pages:
        body = (pages[0].get("content") or {}).get("value", "")
        if isinstance(body, str):
            cache = _cache_probe(body, "risk", encoder)

    return {
        "snapshot_id": row_id,
        "basis": "historical real tool results, deterministic replay",
        "observed_steps": observed_steps,
        "limits": [
            "Only tool-output tokens; no LLM usage, latency or answer quality measured.",
            "Jira selected-issue follow-up uses search result as optimistic proxy, not a recorded jira_get_issue call.",
            "No compact-mode or repeated Confluence reads were observed in this snapshot.",
            "Current Confluence metadata mode still fetches the body upstream before projecting.",
        ],
        "jira": {
            "issues": len(issues),
            "descriptions_removed_by_brief": sum(bool(i.get("description")) for i in issues),
            "full_search_tokens": jira_full,
            "brief_search_tokens": jira_brief,
            "steps": jira_steps,
        },
        "confluence": {
            "search_result_tokens": search_tokens,
            "observed_page_reads": len(pages),
            "observed_repeat_reads": len(pages) - len(
                {str(page.get("id")) for page in pages}
            ),
            "full_page_tokens": page_full,
            "metadata_page_tokens": page_meta,
            "steps": confluence_steps,
        },
        "file_search_probe": cache,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="Read-only Jarvis SQLite DB")
    parser.add_argument("--snapshot-id", type=int, help="Use one context snapshot")
    args = parser.parse_args()
    print(json.dumps(measure(args.db, args.snapshot_id), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
