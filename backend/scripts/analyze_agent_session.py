#!/usr/bin/env python3
"""
Analyze PM agent session history to debug spawn failures.

Usage:
    # From server (docker cp first):
    docker cp jarvis_backend:/app/.runtime/data/workspaces/<team_workspace>/.fast-agent/sessions/<session_id>/history_child.json /tmp/pm_history.json
    python3 scripts/analyze_agent_session.py /tmp/pm_history.json

    # From local (scp first):
    scp server:/tmp/pm_history.json /tmp/pm_history.json
    python3 scripts/analyze_agent_session.py /tmp/pm_history.json
"""
import json
import sys


def analyze(path: str):
    with open(path) as f:
        data = json.load(f)

    msgs = data["messages"] if isinstance(data, dict) and "messages" in data else data
    print(f"Total messages: {len(msgs)}\n")

    for i, m in enumerate(msgs):
        role = m.get("role", "?")

        # Extract reasoning from channels (Codex Responses API format)
        channels = m.get("channels", {})
        reasoning = channels.get("reasoning", [])
        for r in reasoning:
            if isinstance(r, dict) and r.get("text"):
                print(f"\n{'='*60}")
                print(f"[{i}] {role} REASONING:")
                print(f"{'='*60}")
                print(r["text"][:1500])

        # Extract tool calls
        tool_calls = m.get("tool_calls", {})
        if isinstance(tool_calls, dict):
            for call_id, tc in tool_calls.items():
                params = tc.get("params", {})
                name = params.get("name", "?")
                args = params.get("arguments", {})
                print(f"\n[{i}] >>> {name}")
                print(f"      args: {json.dumps(args, ensure_ascii=False)[:300]}")
        elif isinstance(tool_calls, list):
            for tc in tool_calls:
                if isinstance(tc, str):
                    print(f"[{i}] >>> TOOL_CALL id: {tc}")

        # Extract tool results
        tool_results = m.get("tool_results", {})
        if isinstance(tool_results, dict):
            for call_id, tr in tool_results.items():
                result = tr.get("result", tr)
                is_err = tr.get("isError", False)
                content = result.get("content", []) if isinstance(result, dict) else result
                if isinstance(content, list):
                    texts = [c.get("text", str(c))[:300] for c in content if isinstance(c, dict)]
                    content_str = " ".join(texts)
                else:
                    content_str = str(content)
                tag = "ERROR" if is_err else "OK"
                if len(content_str) > 500:
                    content_str = content_str[:500] + "..."
                print(f"[{i}] <<< RESULT[{tag}]: {content_str}")
        elif isinstance(tool_results, list):
            for tr in tool_results:
                if isinstance(tr, str):
                    print(f"[{i}] <<< RESULT id: {tr}")

        # Content text (final assistant response)
        content = m.get("content", [])
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    txt = part["text"]
                    if len(txt) > 20:
                        print(f"\n[{i}] {role.upper()}: {txt[:800]}")
                        print("---")
        elif isinstance(content, str) and len(content) > 20:
            print(f"\n[{i}] {role.upper()}: {content[:800]}")
            print("---")


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/pm_history.json"
    analyze(path)
