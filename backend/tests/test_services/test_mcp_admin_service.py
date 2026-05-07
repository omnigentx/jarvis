"""Unit tests for services.mcp_admin_service.

Covers env probe, package safety heuristics, scaffold round-trip,
static_check decorator/manifest matching + forbidden patterns,
verify gate aggregation, and clean_workspace scopes.

The heavy stages (install_dependencies, run_smoke_test, run_tool_test,
run_test_suite, promote) require a live fast-agent context + real
subprocess pipes; they're exercised via end-to-end manual smoke tests
and the existing test_mcp_catalog integration patch (smoke_test path
is shared). Here we test the deterministic logic only.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from services import mcp_admin_service as svc


@pytest.fixture(autouse=True)
def _isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "_ROOT", tmp_path)
    monkeypatch.setattr(svc, "GENERATED_DIR", tmp_path / "generated")
    monkeypatch.setattr(svc, "TEST_RUNS_DIR", tmp_path / "test_runs")
    yield


# ── Environment + recommended packages ────────────────────────────────


def test_check_environment_returns_python_block():
    info = svc.check_environment()
    assert "python" in info
    assert info["python"]["version"]
    assert "executable" in info["python"]
    # All known binaries are at least probed (None if absent is fine).
    for k in ("uv", "node", "npm", "git"):
        assert k in info


def test_recommended_packages_split_by_ecosystem():
    out = svc.recommended_packages()
    assert "python" in out and "node" in out
    py_names = {p["name"] for p in out["python"]}
    assert "mcp" in py_names
    assert "fastmcp" in py_names
    node_names = {p["name"] for p in out["node"]}
    assert "@modelcontextprotocol/sdk" in node_names


# ── Package safety: recommended → no-warning short-circuit ────────────


@pytest.mark.asyncio
async def test_check_package_safety_recommended_python_no_warnings():
    res = await svc.check_package_safety("httpx", "python")
    assert res["in_recommended"] is True
    assert res["warnings"] == []


@pytest.mark.asyncio
async def test_check_package_safety_recommended_node_no_warnings():
    res = await svc.check_package_safety("zod", "node")
    assert res["in_recommended"] is True
    assert res["warnings"] == []


@pytest.mark.asyncio
async def test_check_package_safety_unknown_ecosystem_returns_warning():
    res = await svc.check_package_safety("foo", "ruby")
    assert res["warnings"]
    assert "unknown ecosystem" in res["warnings"][0]


# ── Typo-squat heuristic ──────────────────────────────────────────────


def test_edit_distance_basics():
    assert svc._edit_distance("abc", "abc") == 0
    assert svc._edit_distance("abc", "abd") == 1
    assert svc._edit_distance("abc", "abcd") == 1
    assert svc._edit_distance("abc", "xyz") == 3


def test_looks_like_typosquat_flags_one_edit_away():
    assert svc._looks_like_typosquat("htpx", "httpx")    # missing 't'
    assert svc._looks_like_typosquat("httpz", "httpx")   # substitution
    assert not svc._looks_like_typosquat("requests", "httpx")
    assert not svc._looks_like_typosquat("httpx", "httpx")  # same


# ── Scaffold ──────────────────────────────────────────────────────────


def test_scaffold_creates_expected_layout():
    res = svc.scaffold("my_tool", "Does the thing.", [
        {"name": "add", "description": "Add two numbers together.", "args": [{"name": "a", "type": "int"}, {"name": "b", "type": "int"}]},
        {"name": "ping", "description": "Health-check the service."},
    ])
    sdir = svc._server_dir("my_tool")
    assert (sdir / "server.py").exists()
    assert (sdir / "tests" / "test_smoke.py").exists()
    assert (sdir / "manifest.json").exists()
    assert (sdir / "requirements.txt").exists()

    manifest = json.loads((sdir / "manifest.json").read_text())
    assert manifest["name"] == "my_tool"
    assert manifest["status"] == "scaffolded"
    assert len(manifest["planned_tools"]) == 2
    body = (sdir / "server.py").read_text()
    assert "@mcp.tool()" in body
    assert "def add(" in body
    assert "def ping(" in body


def test_scaffold_rejects_invalid_name():
    with pytest.raises(ValueError, match="invalid server name"):
        svc.scaffold("Bad Name!", "x", [])


def test_scaffold_rejects_short_tool_description():
    with pytest.raises(ValueError, match=">= 10 chars"):
        svc.scaffold("ok", "desc", [{"name": "t", "description": "short"}])


def test_scaffold_rejects_duplicate_name():
    svc.scaffold("dup", "desc", [{"name": "t", "description": "good description here"}])
    with pytest.raises(FileExistsError):
        svc.scaffold("dup", "desc", [])


# ── static_check ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_static_check_passes_clean_scaffold():
    svc.scaffold("clean", "Clean server.", [
        {"name": "ping", "description": "Health-check the service."},
    ])
    res = await svc.static_check("clean")
    assert res["ok"] is True
    assert res["issues"] == []


@pytest.mark.asyncio
async def test_static_check_flags_missing_tool_definition():
    svc.scaffold("missing", "x", [
        {"name": "ping", "description": "Health-check the service."},
    ])
    # Strip the tool body
    server_py = svc._server_dir("missing") / "server.py"
    text = server_py.read_text()
    text = text.replace("@mcp.tool()\ndef ping(", "def __replaced_ping(")
    server_py.write_text(text)

    res = await svc.static_check("missing")
    assert res["ok"] is False
    assert any(i["kind"] == "missing_tool" for i in res["issues"])


@pytest.mark.asyncio
async def test_static_check_warns_forbidden_patterns():
    svc.scaffold("danger", "x", [
        {"name": "ping", "description": "Health-check the service."},
    ])
    server_py = svc._server_dir("danger") / "server.py"
    bad = server_py.read_text() + '\n\n_ = eval("1+1")\nimport subprocess; subprocess.run("ls", shell=True)\n'
    server_py.write_text(bad)

    res = await svc.static_check("danger")
    # Warnings only — `ok` reflects issues, not warnings.
    kinds = {w["kind"] for w in res["warnings"]}
    assert "forbidden_pattern" in kinds
    bodies = {w["why"] for w in res["warnings"] if w["kind"] == "forbidden_pattern"}
    assert "uses eval()" in bodies
    assert any("shell=True" in b for b in bodies)


@pytest.mark.asyncio
async def test_static_check_syntax_error_blocks():
    svc.scaffold("syntax", "x", [
        {"name": "ping", "description": "Health-check the service."},
    ])
    server_py = svc._server_dir("syntax") / "server.py"
    server_py.write_text("def broken(:\n    pass\n")
    res = await svc.static_check("syntax")
    assert res["ok"] is False
    assert res["issues"][0]["kind"] == "syntax"


# ── verify gate ───────────────────────────────────────────────────────


def _set_history(name: str, entries: list[dict]):
    m = svc._read_manifest(name)
    m["history"] = entries
    svc._write_manifest(name, m)


def test_verify_blocks_when_no_stages_run():
    svc.scaffold("vempty", "x", [
        {"name": "ping", "description": "Health-check the service."},
    ])
    res = svc.verify("vempty")
    assert res["ready"] is False
    assert "static_check has not passed" in res["blockers"]
    assert "install_deps has not passed" in res["blockers"]
    assert "smoke_test has not passed" in res["blockers"]


def test_verify_passes_when_all_stages_ok():
    svc.scaffold("vok", "x", [
        {"name": "ping", "description": "Health-check the service."},
    ])
    _set_history("vok", [
        {"stage": "static_check", "ok": True, "ts": 0, "detail": {}},
        {"stage": "install_deps", "ok": True, "ts": 1, "detail": {}},
        {"stage": "smoke_test", "ok": True, "ts": 2, "detail": {}},
        {"stage": "tool_test", "ok": True, "ts": 3, "detail": {"tool": "ping"}},
    ])
    res = svc.verify("vok")
    assert res["ready"] is True
    assert res["blockers"] == []
    assert res["tested_tools"] == ["ping"]


def test_verify_blocks_if_planned_tool_has_no_passing_test():
    svc.scaffold("vmix", "x", [
        {"name": "ping", "description": "Health-check the service."},
        {"name": "send", "description": "Send something somewhere safe."},
    ])
    _set_history("vmix", [
        {"stage": "static_check", "ok": True, "ts": 0, "detail": {}},
        {"stage": "install_deps", "ok": True, "ts": 1, "detail": {}},
        {"stage": "smoke_test", "ok": True, "ts": 2, "detail": {}},
        {"stage": "tool_test", "ok": True, "ts": 3, "detail": {"tool": "ping"}},
        # send has only failures
        {"stage": "tool_test", "ok": False, "ts": 4, "detail": {"tool": "send"}},
    ])
    res = svc.verify("vmix")
    assert res["ready"] is False
    assert any("send" in b for b in res["blockers"])


# ── list_generated / get_generated ────────────────────────────────────


def test_list_generated_reports_status():
    svc.scaffold("lone", "lonely", [
        {"name": "ping", "description": "Health-check the service."},
    ])
    rows = svc.list_generated()
    assert len(rows) == 1
    assert rows[0]["name"] == "lone"
    assert rows[0]["status"] == "scaffolded"


def test_get_generated_returns_full_manifest():
    svc.scaffold("gone", "x", [
        {"name": "ping", "description": "Health-check the service."},
    ])
    m = svc.get_generated("gone")
    assert m["name"] == "gone"
    assert "history" in m


# ── clean_workspace ───────────────────────────────────────────────────


def test_clean_workspace_test_runs_only_clears_test_runs():
    svc._ensure_root()
    (svc.TEST_RUNS_DIR / "old.log").write_text("garbage")
    svc.scaffold("preserved", "stays", [
        {"name": "ping", "description": "Health-check the service."},
    ])
    res = svc.clean_workspace("test_runs")
    assert res["scope"] == "test_runs"
    assert res["deleted_entries"] == 1
    assert not (svc.TEST_RUNS_DIR / "old.log").exists()
    assert svc._server_dir("preserved").exists()


def test_clean_workspace_named_drops_only_that_server():
    svc.scaffold("alpha", "x", [{"name": "ping", "description": "Health-check the service."}])
    svc.scaffold("beta", "y", [{"name": "ping", "description": "Health-check the service."}])
    res = svc.clean_workspace("alpha")
    assert res["scope"] == "alpha"
    assert not svc._server_dir("alpha").exists()
    assert svc._server_dir("beta").exists()


def test_clean_workspace_all_is_blocked_from_agent():
    with pytest.raises(PermissionError):
        svc.clean_workspace("all")


def test_clean_workspace_unknown_name_raises():
    with pytest.raises(LookupError):
        svc.clean_workspace("nonexistent_server")


# ── _scan_forbidden ──────────────────────────────────────────────────


def test_scan_forbidden_catches_common_patterns():
    src = """
import os
import pickle
os.system("ls")
pickle.loads(b"")
eval("1+1")
"""
    hits = svc._scan_forbidden(src)
    whys = {h["why"] for h in hits}
    assert "uses os.system()" in whys
    assert "deserializes pickle (RCE-prone)" in whys
    assert "uses eval()" in whys


# ── Patch tool ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_tool_replaces_function_body_preserving_decorator():
    svc.scaffold("patchable", "x", [
        {"name": "ping", "description": "Health-check the service."},
    ])
    new_code = '''def ping() -> str:
    """Pong with new behaviour."""
    return "pong"
'''
    res = await svc.patch_tool("patchable", "ping", new_code)
    assert res["tool"] == "ping"
    server_py = (svc._server_dir("patchable") / "server.py").read_text()
    assert "return 'pong'" in server_py or 'return "pong"' in server_py
    assert "@mcp.tool()" in server_py  # decorator preserved


@pytest.mark.asyncio
async def test_patch_tool_rejects_mismatched_function_name():
    svc.scaffold("mismatch", "x", [
        {"name": "ping", "description": "Health-check the service."},
    ])
    new_code = '''def pong() -> str:
    return "x"
'''
    with pytest.raises(ValueError, match="exactly one top-level function named"):
        await svc.patch_tool("mismatch", "ping", new_code)


# ── _generated_payload + promote (cwd persistence regression) ─────────


def test_generated_payload_includes_server_dir_via_cwd_caller_contract():
    """Whoever calls _generated_payload must inject cwd=server_dir afterwards
    (run_smoke_test / run_tool_test / promote all do this). The bare payload
    must not silently provide cwd, so the contract stays explicit."""
    svc.scaffold("payloadcheck", "x", [
        {"name": "ping", "description": "Health-check the service."},
    ])
    sdir = svc._server_dir("payloadcheck")
    # Need a stub .venv/bin/python so _generated_payload's existence check passes.
    venv_py = sdir / ".venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True, exist_ok=True)
    venv_py.write_text("")
    payload = svc._generated_payload("payloadcheck")
    assert payload["transport"] == "stdio"
    assert payload["command"] == str(venv_py)
    assert payload["args"] == ["server.py"]
    assert "cwd" not in payload  # explicit contract — caller injects


@pytest.mark.asyncio
async def test_promote_persists_cwd_to_catalog(monkeypatch):
    """Regression for 'McpError: Connection closed' on attach.

    Before this fix, promote() built the payload with cwd=server_dir but the
    DB insert dropped cwd, so apply_to_registry rebuilt MCPServerSettings
    without cwd. The aggregator then spawned `python server.py` from the
    backend cwd → file-not-found → subprocess died → 'Connection closed'.
    """
    from core.database import (
        AgentMcpAttachmentModel,
        Base,
        McpEventLogModel,
        McpServerModel,
        SessionLocal,
        engine,
    )

    # CI runs against a fresh DB and pytest may pick this file alphabetically
    # before test_mcp_catalog.py whose fixture creates the tables. Ensure the
    # MCP tables exist so the cleanup query below doesn't blow up.
    Base.metadata.create_all(bind=engine)

    svc.scaffold("withcwd", "x", [
        {"name": "ping", "description": "Health-check the service."},
    ])
    sdir = svc._server_dir("withcwd")
    venv_py = sdir / ".venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True, exist_ok=True)
    venv_py.write_text("")

    # Verify gate must pass; fake the history.
    _set_history("withcwd", [
        {"stage": "static_check", "ok": True, "ts": 0, "detail": {}},
        {"stage": "install_deps", "ok": True, "ts": 1, "detail": {}},
        {"stage": "smoke_test", "ok": True, "ts": 2, "detail": {}},
        {"stage": "tool_test", "ok": True, "ts": 3, "detail": {"tool": "ping"}},
    ])

    # Clean any prior run + attachment listener noise.
    with SessionLocal() as db:
        db.query(AgentMcpAttachmentModel).delete()
        db.query(McpEventLogModel).delete()
        db.query(McpServerModel).filter_by(name="withcwd").delete()
        db.commit()

    try:
        res = await svc.promote("withcwd", attach_to=[])
        assert res["promoted"] is True

        with SessionLocal() as db:
            row = db.get(McpServerModel, "withcwd")
            assert row is not None
            assert row.cwd == str(sdir), (
                f"promote() must persist cwd; got {row.cwd!r}, expected {str(sdir)!r}"
            )
    finally:
        # This test shares the prod DB; clean up so cross-file ordering
        # (test_mcp_catalog) doesn't see leftover rows.
        with SessionLocal() as db:
            db.query(AgentMcpAttachmentModel).filter_by(server_name="withcwd").delete()
            db.query(McpServerModel).filter_by(name="withcwd").delete()
            db.query(McpEventLogModel).filter_by(server_name="withcwd").delete()
            db.commit()
