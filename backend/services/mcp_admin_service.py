"""Admin pipeline for Jarvis self-managed MCP servers.

Two flows are supported:

  Path A — config-only (existing MCP):
    User/Jarvis already has a working MCP server (npm/uv package, http url).
    Just smoke-test + persist via mcp_catalog. Handled by the existing
    catalog/attachments services; this module does not duplicate that.

  Path B — Jarvis-authored:
    Agent scaffolds a server inside `.fast-agent/mcp_workspace/generated/{name}/`,
    iterates code via static + functional checks, then promotes it to the
    DB-backed catalog. This module owns the entire pipeline:

        check_environment           — probe binaries / versions
        recommended_packages        — curated allowlist (Python + Node)
        check_package_safety        — flag suspicious deps before install
        scaffold                    — write boilerplate + manifest.json
        static_check                — AST + lint + forbidden patterns
        install_dependencies        — uv pip install into per-server .venv
        run_smoke_test              — initialize + list_tools (no call)
        run_tool_test               — call one tool, check assertions
        run_test_suite              — pytest in workspace
        verify                      — aggregate gate before promote
        promote                     — generated → catalog
        patch_tool                  — re-run pipeline after edit
        clean_workspace             — drop test_runs / single name / all

All mutations emit `audit(action="...")` so the dashboard activity stream
sees Jarvis self-management in realtime.

Forbidden patterns are warned (not hard-blocked) per user policy: regex
hits emit an `audit(action="warn")` row + activity broadcast which the UI
surfaces as a persistent toast.
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.exc import OperationalError

from services.mcp_runtime import audit

logger = logging.getLogger("mcp")

# ── Paths ─────────────────────────────────────────────────────────────

_ROOT = Path(__file__).parent.parent / ".fast-agent" / "mcp_workspace"
GENERATED_DIR = _ROOT / "generated"
TEST_RUNS_DIR = _ROOT / "test_runs"

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}[a-z0-9]$")

# Hard timeout caps so a runaway tool can't pin the event loop.
SMOKE_TIMEOUT_S = 20.0
TOOL_CALL_TIMEOUT_S = 30.0
TEST_SUITE_TIMEOUT_S = 120.0
INSTALL_TIMEOUT_S = 180.0

# ── Recommended package allowlists ────────────────────────────────────

RECOMMENDED_PYTHON = {
    "mcp": "Official MCP Python SDK (server + client primitives).",
    "fastmcp": "High-level FastMCP server framework — preferred for new servers.",
    "httpx": "Async HTTP client; preferred over requests for async tools.",
    "requests": "Sync HTTP client; OK for simple tools without async.",
    "aiohttp": "Async HTTP client/server; alternative to httpx.",
    "pydantic": "Schema validation; required for typed tool args.",
    "anyio": "Async compatibility layer — ships with mcp.",
    "pyyaml": "YAML parser; common in config-driven tools.",
    "tomli-w": "TOML writer (Python <3.13 stdlib lacks writer).",
    "python-dateutil": "Robust datetime parsing.",
    "arrow": "Ergonomic datetime alternative to dateutil.",
    "lxml": "Fast HTML/XML parser.",
    "beautifulsoup4": "HTML scraping utility.",
    "more-itertools": "Iterator helpers.",
    "cachetools": "TTL/LRU caches.",
    "tenacity": "Retry decorator.",
    "structlog": "Structured logging.",
}

RECOMMENDED_NODE = {
    "@modelcontextprotocol/sdk": "Official MCP TypeScript SDK.",
    "zod": "Schema validation; common with MCP TS SDK.",
    "axios": "HTTP client.",
    "undici": "Native fetch implementation; preferred for new code.",
    "node-fetch": "Older fetch polyfill; OK for compat.",
    "yaml": "YAML parser.",
    "date-fns": "Datetime utilities.",
    "lodash": "General utilities (use targeted imports).",
}

# Heuristics: warn if a package looks suspicious. NOT a hard block — agent
# decides after seeing the warning. (Per user policy.)
PYPI_API = "https://pypi.org/pypi/{name}/json"
NPM_API = "https://registry.npmjs.org/{name}"

FORBIDDEN_PATTERNS = [
    (r"\beval\s*\(", "uses eval()"),
    (r"\bexec\s*\(", "uses exec()"),
    (r"\bos\.system\s*\(", "uses os.system()"),
    (r"\b__import__\s*\(\s*[\"']os[\"']", "dynamic os import"),
    (r"subprocess\.\w+\([^)]*shell\s*=\s*True", "subprocess with shell=True"),
    (r"\bpickle\.loads?\s*\(", "deserializes pickle (RCE-prone)"),
    (r"\brm\s+-rf\s+/", "destructive shell pattern"),
]


# ── Path helpers ──────────────────────────────────────────────────────


def _server_dir(name: str) -> Path:
    if not NAME_RE.match(name):
        raise ValueError(
            f"invalid server name {name!r}: must match {NAME_RE.pattern}"
        )
    return GENERATED_DIR / name


def _manifest_path(name: str) -> Path:
    return _server_dir(name) / "manifest.json"


def _venv_python(name: str) -> Path:
    return _server_dir(name) / ".venv" / "bin" / "python"


def _ensure_root() -> None:
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    TEST_RUNS_DIR.mkdir(parents=True, exist_ok=True)


# ── Manifest I/O ──────────────────────────────────────────────────────


def _read_manifest(name: str) -> dict[str, Any]:
    p = _manifest_path(name)
    if not p.exists():
        raise LookupError(f"server {name!r} not scaffolded")
    return json.loads(p.read_text())


def _write_manifest(name: str, manifest: dict[str, Any]) -> None:
    manifest["updated_at"] = time.time()
    _manifest_path(name).write_text(json.dumps(manifest, indent=2))


def _bump_status(name: str, stage: str, ok: bool, detail: dict[str, Any] | None = None) -> None:
    m = _read_manifest(name)
    m.setdefault("history", []).append(
        {"stage": stage, "ok": ok, "ts": time.time(), "detail": detail or {}}
    )
    m["last_stage"] = stage
    m["last_stage_ok"] = ok
    _write_manifest(name, m)


# ── Stage 0: Environment probe ────────────────────────────────────────


def _binary_version(cmd: list[str]) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
        out = (r.stdout or r.stderr or "").strip().splitlines()
        return out[0] if out else None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def check_environment() -> dict[str, Any]:
    """Snapshot the runtime so the agent knows what tools/binaries are
    available before deciding which language/framework to use."""
    return {
        "python": {
            "version": sys.version.split()[0],
            "executable": sys.executable,
            "implementation": sys.implementation.name,
        },
        "uv": _binary_version(["uv", "--version"]),
        "node": _binary_version(["node", "--version"]),
        "npm": _binary_version(["npm", "--version"]),
        "npx": _binary_version(["npx", "--version"]),
        "deno": _binary_version(["deno", "--version"]),
        "bun": _binary_version(["bun", "--version"]),
        "git": _binary_version(["git", "--version"]),
        "docker": _binary_version(["docker", "--version"]),
        "platform": sys.platform,
        "workspace_root": str(_ROOT),
        "ts": time.time(),
    }


def recommended_packages() -> dict[str, Any]:
    """Return curated allowlists. Agent uses these without extra safety check."""
    return {
        "python": [{"name": k, "purpose": v} for k, v in RECOMMENDED_PYTHON.items()],
        "node": [{"name": k, "purpose": v} for k, v in RECOMMENDED_NODE.items()],
    }


# ── Stage 0b: Package safety check ────────────────────────────────────


async def check_package_safety(
    package_name: str, ecosystem: str = "python"
) -> dict[str, Any]:
    """Lightweight risk score for an unknown package. Returns:

        {"name", "ecosystem", "in_recommended": bool, "exists": bool,
         "version_count": int, "first_release_ts": float|None,
         "downloads_last_month": int|None, "warnings": [str], "info_url": str}

    We do NOT block install — the agent reads the warnings and decides.
    """
    name_lower = package_name.lower()
    if ecosystem == "python":
        if name_lower in RECOMMENDED_PYTHON:
            return {
                "name": package_name,
                "ecosystem": "python",
                "in_recommended": True,
                "warnings": [],
                "info_url": f"https://pypi.org/project/{package_name}/",
            }
        return await _pypi_safety(package_name)

    if ecosystem == "node":
        if name_lower in RECOMMENDED_NODE:
            return {
                "name": package_name,
                "ecosystem": "node",
                "in_recommended": True,
                "warnings": [],
                "info_url": f"https://www.npmjs.com/package/{package_name}",
            }
        return await _npm_safety(package_name)

    return {"name": package_name, "ecosystem": ecosystem,
            "warnings": [f"unknown ecosystem {ecosystem!r}"]}


async def _pypi_safety(name: str) -> dict[str, Any]:
    import httpx

    warnings: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(PYPI_API.format(name=name))
        if r.status_code == 404:
            return {"name": name, "ecosystem": "python", "exists": False,
                    "warnings": ["package not on PyPI"], "in_recommended": False,
                    "info_url": f"https://pypi.org/project/{name}/"}
        if r.status_code != 200:
            return {"name": name, "ecosystem": "python", "exists": None,
                    "warnings": [f"PyPI lookup HTTP {r.status_code}"],
                    "in_recommended": False, "info_url": f"https://pypi.org/project/{name}/"}
        data = r.json()
    except Exception as exc:
        return {"name": name, "ecosystem": "python", "exists": None,
                "warnings": [f"PyPI lookup failed: {type(exc).__name__}"],
                "in_recommended": False, "info_url": f"https://pypi.org/project/{name}/"}

    info = data.get("info", {}) or {}
    releases = data.get("releases", {}) or {}
    first_release_ts = None
    earliest = None
    for ver, files in releases.items():
        for f in files or []:
            try:
                ts = datetime.fromisoformat(f["upload_time_iso_8601"].rstrip("Z")).timestamp()
            except Exception:
                continue
            if earliest is None or ts < earliest:
                earliest = ts
    first_release_ts = earliest

    age_days = ((time.time() - first_release_ts) / 86400) if first_release_ts else None
    if age_days is not None and age_days < 30:
        warnings.append(f"package is very new ({int(age_days)} days old)")
    if not info.get("home_page") and not info.get("project_urls"):
        warnings.append("no homepage/project URL declared")
    if not info.get("license"):
        warnings.append("no license declared")
    name_lower = name.lower()
    for known in RECOMMENDED_PYTHON:
        if name_lower != known and _looks_like_typosquat(name_lower, known):
            warnings.append(f"name resembles {known!r} (possible typosquat)")

    return {
        "name": name,
        "ecosystem": "python",
        "in_recommended": False,
        "exists": True,
        "version_count": len(releases),
        "first_release_ts": first_release_ts,
        "age_days": age_days,
        "summary": info.get("summary"),
        "license": info.get("license"),
        "homepage": info.get("home_page") or "",
        "warnings": warnings,
        "info_url": f"https://pypi.org/project/{name}/",
    }


async def _npm_safety(name: str) -> dict[str, Any]:
    import httpx

    warnings: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(NPM_API.format(name=name))
        if r.status_code == 404:
            return {"name": name, "ecosystem": "node", "exists": False,
                    "warnings": ["package not on npm"], "in_recommended": False,
                    "info_url": f"https://www.npmjs.com/package/{name}"}
        if r.status_code != 200:
            return {"name": name, "ecosystem": "node", "exists": None,
                    "warnings": [f"npm lookup HTTP {r.status_code}"],
                    "in_recommended": False, "info_url": f"https://www.npmjs.com/package/{name}"}
        data = r.json()
    except Exception as exc:
        return {"name": name, "ecosystem": "node", "exists": None,
                "warnings": [f"npm lookup failed: {type(exc).__name__}"],
                "in_recommended": False, "info_url": f"https://www.npmjs.com/package/{name}"}

    times = data.get("time", {}) or {}
    created = times.get("created")
    first_release_ts = None
    if created:
        try:
            first_release_ts = datetime.fromisoformat(created.rstrip("Z")).timestamp()
        except Exception:
            pass
    age_days = ((time.time() - first_release_ts) / 86400) if first_release_ts else None
    if age_days is not None and age_days < 30:
        warnings.append(f"package is very new ({int(age_days)} days old)")
    name_lower = name.lower()
    for known in RECOMMENDED_NODE:
        if name_lower != known and _looks_like_typosquat(name_lower, known.lower()):
            warnings.append(f"name resembles {known!r} (possible typosquat)")

    return {
        "name": name,
        "ecosystem": "node",
        "in_recommended": False,
        "exists": True,
        "version_count": len(data.get("versions", {}) or {}),
        "first_release_ts": first_release_ts,
        "age_days": age_days,
        "description": (data.get("description") or "")[:200],
        "license": data.get("license"),
        "warnings": warnings,
        "info_url": f"https://www.npmjs.com/package/{name}",
    }


def _looks_like_typosquat(candidate: str, known: str) -> bool:
    """Levenshtein distance 1 OR character-substitution heuristic."""
    if abs(len(candidate) - len(known)) > 1:
        return False
    if candidate == known:
        return False
    # Trivial: 1 edit distance
    return _edit_distance(candidate, known) == 1


def _edit_distance(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur.append(min(cur[-1] + 1, prev[j] + 1, prev[j - 1] + cost))
        prev = cur
    return prev[-1]


# ── Stage 1: Scaffold ─────────────────────────────────────────────────


_SERVER_TEMPLATE = '''"""Auto-scaffolded MCP server: {name}

{description}

DO NOT edit unless you understand the FastMCP @mcp.tool() contract.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("{name}")


{tool_stubs}


if __name__ == "__main__":
    mcp.run()
'''

_TOOL_STUB_TEMPLATE = '''@mcp.tool()
def {fn}({args_sig}) -> str:
    """{description}

    TODO(jarvis): implement {fn}.
    """
    raise NotImplementedError("tool {fn} not implemented yet")
'''

_TEST_TEMPLATE = '''"""Smoke test for {name}. Replace placeholders with real assertions."""
import json


def test_manifest_present():
    """Manifest must list every tool the server exposes."""
    from pathlib import Path
    manifest = json.loads((Path(__file__).resolve().parent.parent / "manifest.json").read_text())
    assert manifest["name"] == "{name}"
    assert isinstance(manifest.get("planned_tools", []), list)
'''


def scaffold(
    name: str,
    description: str,
    planned_tools: list[dict[str, Any]],
    *,
    actor: str = "jarvis",
) -> dict[str, Any]:
    """Create the directory + boilerplate. `planned_tools` items must be
    `{"name": str, "description": str, "args": [{"name", "type"}]}`."""
    _ensure_root()
    if not NAME_RE.match(name):
        raise ValueError(
            f"invalid server name {name!r}: must match {NAME_RE.pattern}"
        )
    if _server_dir(name).exists():
        raise FileExistsError(f"server {name!r} already scaffolded")
    # Catalog collision: a generated server promoted later would clash.
    # Check at scaffold time so the agent fails fast and renames.
    try:
        from core.database import McpServerModel, SessionLocal
        with SessionLocal() as db:
            if db.get(McpServerModel, name):
                raise FileExistsError(
                    f"server {name!r} already exists in catalog (promoted/built-in)"
                )
    except FileExistsError:
        raise
    except (OperationalError, ImportError) as exc:
        # Narrow catch: tables missing (unit-test isolation) or core.database
        # import failed. Real DB errors (programming, integrity, connection)
        # fall through to the caller — silent swallow used to mask them.
        logger.debug("[mcp.admin] catalog collision check skipped: %s", exc)

    tools_meta: list[dict[str, Any]] = []
    stubs: list[str] = []
    for t in planned_tools or []:
        tname = t.get("name")
        if not tname or not re.match(r"^[a-z][a-z0-9_]*$", tname):
            raise ValueError(f"invalid tool name {tname!r}")
        desc = (t.get("description") or "").strip()
        if len(desc) < 10:
            raise ValueError(
                f"tool {tname!r}: description must be >= 10 chars (LLM needs context)"
            )
        args = t.get("args") or []
        args_sig = ", ".join(
            f"{a['name']}: {a.get('type', 'str')}"
            for a in args
        ) or ""
        stubs.append(_TOOL_STUB_TEMPLATE.format(
            fn=tname, description=desc.replace('"""', '').replace('\\', ''),
            args_sig=args_sig,
        ))
        tools_meta.append({"name": tname, "description": desc, "args": args})

    server_code = _SERVER_TEMPLATE.format(
        name=name,
        description=description.replace('"""', '').replace('\\', ''),
        tool_stubs="\n\n".join(stubs) if stubs else "# (no tools planned yet)",
    )

    sdir = _server_dir(name)
    sdir.mkdir(parents=True)
    (sdir / "tests").mkdir()
    (sdir / "logs").mkdir()
    (sdir / "server.py").write_text(server_code)
    (sdir / "tests" / "__init__.py").write_text("")
    (sdir / "tests" / "test_smoke.py").write_text(_TEST_TEMPLATE.format(name=name))
    (sdir / "requirements.txt").write_text("mcp\n")

    manifest = {
        "name": name,
        "description": description,
        "planned_tools": tools_meta,
        "spec_hash": _spec_hash(tools_meta),
        "language": "python",
        "version": "0.1.0",
        "status": "scaffolded",
        "created_by": actor,
        "created_at": time.time(),
        "updated_at": time.time(),
        "history": [],
    }
    _write_manifest(name, manifest)
    return {"name": name, "path": str(sdir), "tools": [t["name"] for t in tools_meta]}


def _spec_hash(planned_tools: list[dict[str, Any]]) -> str:
    """Stable hash of the spec — detects drift between scaffold and verify."""
    import hashlib
    canonical = json.dumps(
        [{"name": t["name"], "args": t.get("args", [])} for t in planned_tools],
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


# ── Stage 2: Static check ─────────────────────────────────────────────


def _scan_forbidden(text: str) -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    for pattern, why in FORBIDDEN_PATTERNS:
        for m in re.finditer(pattern, text):
            hits.append({"pattern": pattern, "why": why, "snippet": text[max(0, m.start() - 20):m.end() + 20]})
    return hits


def _extract_decorated_tools(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                # @mcp.tool()  or  @mcp.tool
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr == "tool":
                    found.add(node.name)
                elif isinstance(dec, ast.Attribute) and dec.attr == "tool":
                    found.add(node.name)
    return found


async def static_check(name: str) -> dict[str, Any]:
    """AST parse + tool-name match + lint + forbidden-pattern scan.

    Forbidden hits are recorded as `audit(action="warn")` so the dashboard
    surfaces a toast — they do NOT fail the check.
    """
    sdir = _server_dir(name)
    server_py = sdir / "server.py"
    if not server_py.exists():
        raise LookupError(f"server.py not found for {name!r}")
    text = server_py.read_text()

    issues: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    # Parse
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        issues.append({"kind": "syntax", "message": str(exc)})
        return {"ok": False, "issues": issues, "warnings": warnings}

    # Decorator-vs-manifest match
    decorated = _extract_decorated_tools(tree)
    manifest = _read_manifest(name)
    planned = {t["name"] for t in manifest.get("planned_tools", [])}
    missing = sorted(planned - decorated)
    extra = sorted(decorated - planned)
    if missing:
        issues.append({"kind": "missing_tool", "message": f"manifest declares but server.py lacks: {missing}"})
    if extra:
        issues.append({"kind": "rogue_tool", "message": f"server.py defines tool not in manifest: {extra}"})

    # Forbidden patterns → warnings (broadcast)
    hits = _scan_forbidden(text)
    if hits:
        warnings.extend([{"kind": "forbidden_pattern", **h} for h in hits])
        async with audit(
            "warn", server=name, actor="jarvis",
            detail={"category": "forbidden_pattern", "hits": hits},
        ):
            pass

    # Optional: ruff if installed in main env
    ruff = shutil.which("ruff")
    if ruff:
        try:
            r = subprocess.run(
                [ruff, "check", "--output-format=concise", str(server_py)],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode != 0:
                for line in (r.stdout or "").splitlines():
                    line = line.strip()
                    if line:
                        warnings.append({"kind": "lint", "message": line})
        except Exception:
            logger.debug("[mcp.admin] ruff unavailable or failed; skipping")

    ok = not issues
    _bump_status(name, "static_check", ok, {"issues": issues, "warnings": warnings})
    return {"ok": ok, "issues": issues, "warnings": warnings}


# ── Stage 3: Install dependencies into per-server venv ─────────────────


async def install_dependencies(name: str) -> dict[str, Any]:
    """Create `.venv` (uv preferred, fall back to stdlib venv) and install
    requirements.txt into it. Per-server isolation per user policy."""
    sdir = _server_dir(name)
    req = sdir / "requirements.txt"
    if not req.exists():
        raise LookupError(f"requirements.txt missing for {name!r}")
    venv = sdir / ".venv"

    async with audit("install_deps", server=name, actor="jarvis") as a:
        uv = shutil.which("uv")
        try:
            if uv:
                if not venv.exists():
                    proc = await asyncio.create_subprocess_exec(
                        uv, "venv", str(venv),
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    )
                    out, err = await asyncio.wait_for(proc.communicate(), timeout=INSTALL_TIMEOUT_S)
                    if proc.returncode != 0:
                        a.set(ok=False, stderr=(err or b"").decode("utf-8", "replace")[:2000])
                        return {"ok": False, "error": "uv venv failed",
                                "stderr": (err or b"").decode("utf-8", "replace")[:2000]}

                proc = await asyncio.create_subprocess_exec(
                    uv, "pip", "install", "--python", str(_venv_python(name)),
                    "-r", str(req),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                out, err = await asyncio.wait_for(proc.communicate(), timeout=INSTALL_TIMEOUT_S)
            else:
                # Fallback: stdlib venv + pip
                if not venv.exists():
                    proc = await asyncio.create_subprocess_exec(
                        sys.executable, "-m", "venv", str(venv),
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    )
                    await asyncio.wait_for(proc.communicate(), timeout=INSTALL_TIMEOUT_S)
                proc = await asyncio.create_subprocess_exec(
                    str(_venv_python(name)), "-m", "pip", "install", "-r", str(req),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                out, err = await asyncio.wait_for(proc.communicate(), timeout=INSTALL_TIMEOUT_S)

            ok = proc.returncode == 0
            stderr_tail = (err or b"").decode("utf-8", "replace")[-2000:]
            stdout_tail = (out or b"").decode("utf-8", "replace")[-2000:]
            a.set(ok=ok, returncode=proc.returncode)
            _bump_status(name, "install_deps", ok, {
                "stderr_tail": stderr_tail if not ok else "",
                "returncode": proc.returncode,
            })
            return {
                "ok": ok,
                "returncode": proc.returncode,
                "stdout_tail": stdout_tail if ok else "",
                "stderr_tail": stderr_tail,
            }
        except asyncio.TimeoutError:
            a.set(ok=False, error=f"install timed out after {INSTALL_TIMEOUT_S}s")
            return {"ok": False, "error": f"install timed out after {INSTALL_TIMEOUT_S}s"}
        except Exception as exc:
            a.set(ok=False, error=f"{type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


# ── Stage 4: Smoke test (protocol-level) ───────────────────────────────


def _generated_payload(name: str) -> dict[str, Any]:
    """Build the MCPServerSettings payload for spawning a generated server.

    The server is run via the per-server venv's python so deps resolve.
    Working dir = the server directory so manifest.json is relative.
    """
    venv_py = _venv_python(name)
    if not venv_py.exists():
        raise LookupError(f".venv missing for {name!r} — run install_dependencies first")
    return {
        "transport": "stdio",
        "command": str(venv_py),
        "args": ["server.py"],
        "env": {
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": str(_server_dir(name)),
        },
    }


async def run_smoke_test(name: str) -> dict[str, Any]:
    """Spawn the generated server, MCP-initialize, list_tools, disconnect.

    Verifies that:
      * subprocess starts and speaks MCP
      * declared tools all show up

    Reuses mcp_catalog.smoke_test which knows the connection-manager dance.
    """
    from services import mcp_catalog

    # cwd needs to be the server dir so any relative manifest reads work.
    payload = _generated_payload(name)
    # mcp_catalog.smoke_test reads cwd from MCPServerSettings.cwd if set
    payload["cwd"] = str(_server_dir(name))

    async with audit("smoke_test", server=name, actor="jarvis") as a:
        result = await mcp_catalog.smoke_test(
            payload, timeout=SMOKE_TIMEOUT_S, return_tool_details=True,
        )
        if result.get("ok"):
            manifest = _read_manifest(name)
            planned = {t["name"] for t in manifest.get("planned_tools", [])}
            actual = set(result.get("tools") or [])
            missing = sorted(planned - actual)
            extra = sorted(actual - planned)
            mismatch = bool(missing or extra)
            a.set(ok=not mismatch, tools=list(actual), missing=missing, extra=extra)
            _bump_status(name, "smoke_test", not mismatch, {
                "tools": list(actual), "missing": missing, "extra": extra,
            })
            if mismatch:
                return {"ok": False, "tools": list(actual),
                        "missing": missing, "extra": extra,
                        "error": "manifest/tool mismatch"}
            return result
        a.set(ok=False, error=result.get("error"))
        _bump_status(name, "smoke_test", False, {"error": result.get("error")})
        return result


# ── Stage 5: Functional tool test ──────────────────────────────────────


async def run_tool_test(
    name: str,
    tool_name: str,
    args: dict[str, Any] | None = None,
    assertions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Spawn the server, call one tool with `args`, evaluate `assertions`.

    Assertion DSL — each item is a dict:
      {"type": "no_error"}                        — result.isError must be False
      {"type": "field_present", "path": "a.b.c"}  — JSON pointer present
      {"type": "regex_match", "path": "text", "pattern": "..."}
      {"type": "type_check",  "path": "x.y", "expected": "str|int|list|dict|bool|float"}
      {"type": "duration_under_ms", "ms": 5000}
    """
    from mcp.client.session import ClientSession

    from services import mcp_catalog, shared_state

    agent_app = shared_state.agent_app
    if agent_app is None:
        return {"ok": False, "error": "agent_app not initialized", "passed": [], "failed": []}

    context = None
    for ag in (getattr(agent_app, "_agents", {}) or {}).values():
        ctx = getattr(ag, "context", None)
        if ctx is not None:
            context = ctx
            break
    if context is None:
        return {"ok": False, "error": "fast-agent context unavailable", "passed": [], "failed": []}

    server_registry = getattr(context, "server_registry", None)
    if server_registry is None:
        return {"ok": False, "error": "server_registry missing", "passed": [], "failed": []}

    smoke_name = f"__functest_{int(time.time() * 1000)}__"
    payload = _generated_payload(name)
    payload["cwd"] = str(_server_dir(name))
    settings = mcp_catalog._payload_to_mcp_settings(smoke_name, payload)
    server_registry.registry[smoke_name] = settings

    from fast_agent.mcp.mcp_connection_manager import MCPConnectionManager

    started = time.time()
    async with audit("tool_test", server=name, actor="jarvis", detail={"tool": tool_name}) as a:
        try:
            async def _run() -> dict[str, Any]:
                def _factory(read, write, read_timeout, **_kw):
                    return ClientSession(read, write, read_timeout)

                async with MCPConnectionManager(server_registry, context=context) as cm:
                    conn = await cm.launch_server(
                        smoke_name, client_session_factory=_factory,
                        startup_timeout_seconds=SMOKE_TIMEOUT_S,
                        trigger_oauth=False,
                    )
                    await conn.wait_for_initialized()
                    if conn.session is None or getattr(conn, "_error_occurred", False):
                        return {"ok": False, "error": getattr(conn, "_error_message", None) or "session not initialized",
                                "passed": [], "failed": []}
                    call_started = time.time()
                    try:
                        call_result = await conn.session.call_tool(tool_name, args or {})
                    finally:
                        duration_ms = int((time.time() - call_started) * 1000)
                    await cm.disconnect_server(smoke_name)
                    return {"ok": True, "duration_ms": duration_ms, "result": _serialize_call_result(call_result)}

            outcome = await asyncio.wait_for(_run(), timeout=TOOL_CALL_TIMEOUT_S + 5)
        except asyncio.TimeoutError:
            outcome = {"ok": False, "error": f"timed out after {TOOL_CALL_TIMEOUT_S}s"}
        except Exception as exc:
            outcome = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            server_registry.registry.pop(smoke_name, None)

        if not outcome.get("ok"):
            a.set(ok=False, error=outcome.get("error"))
            _bump_status(name, "tool_test", False, {"tool": tool_name, "error": outcome.get("error")})
            return {"ok": False, "tool": tool_name, "error": outcome.get("error"),
                    "passed": [], "failed": []}

        passed: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        for spec in (assertions or []):
            ok, reason = _evaluate_assertion(spec, outcome["result"], outcome["duration_ms"])
            (passed if ok else failed).append({"assertion": spec, "reason": reason})
        gate_ok = not failed
        a.set(ok=gate_ok, passed=len(passed), failed=len(failed),
              duration_ms=int((time.time() - started) * 1000))
        _bump_status(name, "tool_test", gate_ok, {"tool": tool_name, "passed": passed, "failed": failed})
        return {
            "ok": gate_ok, "tool": tool_name,
            "duration_ms": outcome["duration_ms"],
            "result_preview": outcome["result"],
            "passed": passed, "failed": failed,
        }


def _serialize_call_result(call_result: Any) -> dict[str, Any]:
    """Reduce MCP CallToolResult to JSON-friendly shape."""
    items: list[dict[str, Any]] = []
    for c in getattr(call_result, "content", None) or []:
        item: dict[str, Any] = {"type": getattr(c, "type", "unknown")}
        text = getattr(c, "text", None)
        if text is not None:
            item["text"] = text
            try:
                item["json"] = json.loads(text)
            except Exception:
                pass
        items.append(item)
    return {
        "isError": bool(getattr(call_result, "isError", False)),
        "content": items,
    }


def _resolve_path(obj: Any, path: str) -> tuple[bool, Any]:
    cur: Any = obj
    if path == "":
        return True, cur
    parts = path.split(".")
    for p in parts:
        if isinstance(cur, list):
            try:
                cur = cur[int(p)]
                continue
            except (ValueError, IndexError, TypeError):
                return False, None
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return False, None
    return True, cur


def _evaluate_assertion(
    spec: dict[str, Any], result: dict[str, Any], duration_ms: int,
) -> tuple[bool, str]:
    kind = spec.get("type")
    if kind == "no_error":
        return (not result.get("isError"), "isError was True" if result.get("isError") else "ok")
    if kind == "duration_under_ms":
        ms = int(spec.get("ms", 0))
        return (duration_ms <= ms, f"{duration_ms}ms vs cap {ms}ms")
    if kind == "field_present":
        ok, _ = _resolve_path(result, spec.get("path", ""))
        return (ok, "found" if ok else f"path {spec.get('path')!r} missing")
    if kind == "regex_match":
        ok, val = _resolve_path(result, spec.get("path", ""))
        if not ok:
            return False, f"path {spec.get('path')!r} missing"
        if not isinstance(val, str):
            return False, f"value at {spec.get('path')!r} not str ({type(val).__name__})"
        return (bool(re.search(spec.get("pattern", ""), val)), "regex match" if re.search(spec.get("pattern", ""), val) else "regex no match")
    if kind == "type_check":
        ok, val = _resolve_path(result, spec.get("path", ""))
        if not ok:
            return False, f"path {spec.get('path')!r} missing"
        type_map = {"str": str, "int": int, "float": float, "list": list, "dict": dict, "bool": bool}
        expected = type_map.get(spec.get("expected", ""))
        if expected is None:
            return False, f"unknown expected type {spec.get('expected')!r}"
        return (isinstance(val, expected), f"got {type(val).__name__}")
    return False, f"unknown assertion type {kind!r}"


# ── Stage 6: Test suite (pytest in venv) ──────────────────────────────


async def run_test_suite(name: str) -> dict[str, Any]:
    """Run the server's `tests/` directory via pytest from the per-server venv.
    Suite results land in `mcp_workspace/test_runs/{name}-{ts}.json`."""
    sdir = _server_dir(name)
    tests_dir = sdir / "tests"
    if not tests_dir.exists():
        return {"ok": True, "skipped": True, "reason": "no tests/ dir"}
    venv_py = _venv_python(name)
    if not venv_py.exists():
        return {"ok": False, "error": ".venv missing — run install_dependencies first"}

    _ensure_root()
    run_log = TEST_RUNS_DIR / f"{name}-{int(time.time())}.log"

    async with audit("test_suite", server=name, actor="jarvis") as a:
        try:
            proc = await asyncio.create_subprocess_exec(
                str(venv_py), "-m", "pytest", "-q", str(tests_dir),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                cwd=str(sdir),
            )
            try:
                out, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=TEST_SUITE_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                proc.kill()
                a.set(ok=False, error="suite timed out")
                _bump_status(name, "test_suite", False, {"error": "timeout"})
                return {"ok": False, "error": f"timed out after {TEST_SUITE_TIMEOUT_S}s"}
            output = (out or b"").decode("utf-8", "replace")
            run_log.write_text(output)
            ok = proc.returncode == 0
            a.set(ok=ok, returncode=proc.returncode, log=str(run_log))
            _bump_status(name, "test_suite", ok, {"returncode": proc.returncode, "log": str(run_log)})
            return {
                "ok": ok, "returncode": proc.returncode,
                "log": str(run_log),
                "tail": output.splitlines()[-30:],
            }
        except FileNotFoundError as exc:
            # pytest not installed — agent should add to requirements.txt
            a.set(ok=False, error=f"pytest missing: {exc}")
            return {"ok": False, "error": "pytest not installed in venv; add to requirements.txt"}


# ── Stage 7: Verify gate ──────────────────────────────────────────────


def verify(name: str) -> dict[str, Any]:
    """Aggregate the manifest history into a ready/blockers verdict."""
    manifest = _read_manifest(name)
    history = manifest.get("history", [])

    by_stage: dict[str, dict[str, Any]] = {}
    for h in history:
        by_stage[h["stage"]] = h  # last write wins → most recent run

    blockers: list[str] = []
    warnings: list[str] = []

    static = by_stage.get("static_check")
    if not static or not static.get("ok"):
        blockers.append("static_check has not passed")

    install = by_stage.get("install_deps")
    if not install or not install.get("ok"):
        blockers.append("install_deps has not passed")

    smoke = by_stage.get("smoke_test")
    if not smoke or not smoke.get("ok"):
        blockers.append("smoke_test has not passed")

    planned = [t["name"] for t in manifest.get("planned_tools", [])]
    tested_tools: set[str] = set()
    for h in history:
        if h["stage"] == "tool_test" and h.get("ok"):
            t = (h.get("detail") or {}).get("tool")
            if t:
                tested_tools.add(t)
    missing_tested = [t for t in planned if t not in tested_tools]
    if missing_tested:
        blockers.append(f"these planned tools have no passing functional test: {missing_tested}")

    suite = by_stage.get("test_suite")
    if suite and not suite.get("ok"):
        blockers.append("test_suite is failing")

    # Spec drift: if planned_tools changed since scaffold, warn.
    current_hash = _spec_hash(manifest.get("planned_tools", []))
    original_hash = manifest.get("spec_hash")
    if original_hash and current_hash != original_hash:
        warnings.append(
            f"planned_tools spec changed since scaffold "
            f"({original_hash[:8]} → {current_hash[:8]}) — "
            f"all tests should be re-evaluated against the new spec"
        )

    if static and static.get("detail", {}).get("warnings"):
        for w in static["detail"]["warnings"]:
            if w.get("kind") == "forbidden_pattern":
                warnings.append(f"forbidden pattern: {w.get('why')}")

    ready = not blockers
    return {
        "ready": ready,
        "blockers": blockers,
        "warnings": warnings,
        "stages_seen": sorted(by_stage.keys()),
        "tested_tools": sorted(tested_tools),
        "planned_tools": planned,
    }


# ── Stage 8: Promote → catalog ─────────────────────────────────────────


async def promote(
    name: str,
    *,
    attach_to: list[str] | None = None,
    actor: str = "jarvis",
) -> dict[str, Any]:
    """After verify().ready==True, persist into mcp_servers DB and (optionally)
    attach to one or more agents. Skips smoke-test in mcp_catalog.create()
    by going straight through the same DB path with `is_builtin=False`.

    Refuses to promote if verify() is not ready.
    """
    gate = verify(name)
    if not gate["ready"]:
        raise RuntimeError(f"verify gate not ready: {gate['blockers']}")

    from services import mcp_attachments, mcp_catalog
    from core.database import McpServerModel, SessionLocal

    payload = _generated_payload(name)
    payload["cwd"] = str(_server_dir(name))

    async with audit("promote", server=name, actor=actor) as a:
        # Direct DB insert (skip the catalog smoke_test — we already smoke-tested
        # extensively as part of the pipeline). Still validate the shape.
        mcp_catalog.validate_payload(name, payload)
        async with mcp_catalog.server_lock(name):
            now = time.time()
            with SessionLocal() as db:
                if db.get(McpServerModel, name):
                    raise ValueError(f"server {name!r} already in catalog")
                row = McpServerModel(
                    name=name,
                    transport=payload["transport"],
                    command=payload.get("command"),
                    args_json=json.dumps(payload.get("args") or []),
                    env_json=json.dumps(payload.get("env") or {}),
                    url=payload.get("url"),
                    cwd=payload.get("cwd"),
                    is_builtin=False,
                    created_at=now,
                    updated_at=now,
                )
                db.add(row)
                db.commit()

        manifest = _read_manifest(name)
        manifest["status"] = "promoted"
        manifest["promoted_at"] = time.time()
        _write_manifest(name, manifest)

        attached: list[dict[str, Any]] = []
        for agent_name in (attach_to or []):
            try:
                attached.append(await mcp_attachments.attach(agent_name, name, actor=actor))
            except Exception as exc:
                attached.append({"agent": agent_name, "error": f"{type(exc).__name__}: {exc}"})

        a.set(attached=attached)
        return {"name": name, "promoted": True, "attached": attached}


async def patch_tool(
    name: str,
    tool_name: str,
    new_code: str,
    *,
    actor: str = "jarvis",
) -> dict[str, Any]:
    """Replace one tool function's body in server.py and bump manifest version.

    The agent provides full new code for a single `@mcp.tool()` function.
    AST-substitute the matching function definition; preserve everything else.
    Re-runs the pipeline up through static_check; the agent decides which
    further stages to re-run.
    """
    sdir = _server_dir(name)
    server_py = sdir / "server.py"
    if not server_py.exists():
        raise LookupError(f"server.py missing for {name!r}")

    text = server_py.read_text()
    tree = ast.parse(text)

    # Parse the new code; expect exactly one top-level function/async def
    new_tree = ast.parse(new_code)
    new_funcs = [n for n in new_tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if len(new_funcs) != 1 or new_funcs[0].name != tool_name:
        raise ValueError(
            f"new_code must define exactly one top-level function named {tool_name!r}"
        )

    # Find existing function and splice
    target_idx = None
    for idx, node in enumerate(tree.body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == tool_name:
            target_idx = idx
            break
    if target_idx is None:
        raise LookupError(f"function {tool_name!r} not found in server.py")

    # Preserve the existing decorator list (e.g. @mcp.tool()) by carrying it forward
    new_funcs[0].decorator_list = tree.body[target_idx].decorator_list
    tree.body[target_idx] = new_funcs[0]

    # ast.unparse round-trips: keeps overall structure but loses comments inside
    # the replaced function. Acceptable for hot-fix.
    rewritten = ast.unparse(tree)
    # Quick sanity reparse
    ast.parse(rewritten)
    server_py.write_text(rewritten + "\n")

    manifest = _read_manifest(name)
    parts = manifest.get("version", "0.1.0").split(".")
    parts[-1] = str(int(parts[-1]) + 1) if parts[-1].isdigit() else parts[-1] + "+1"
    manifest["version"] = ".".join(parts)
    manifest["status"] = "patched"
    # Invalidate any prior tool_test entry for THIS tool so verify() no longer
    # treats the patched body as previously-passing. verify() reads history
    # last-write-wins per (stage, tool), so we drop matching entries instead
    # of appending an "ok=False" placeholder. Untouched tools keep their
    # passes; the agent must re-run run_tool_test for the patched one.
    history = manifest.get("history") or []
    manifest["history"] = [
        h for h in history
        if not (h.get("stage") == "tool_test"
                and (h.get("detail") or {}).get("tool") == tool_name)
    ]
    _write_manifest(name, manifest)

    async with audit("patch_tool", server=name, actor=actor, detail={"tool": tool_name}):
        pass

    # Re-run static_check immediately so the agent gets fast feedback.
    return {"name": name, "tool": tool_name, "version": manifest["version"],
            "static_check": await static_check(name)}


# ── Stage 9: Workspace cleanup ─────────────────────────────────────────


def list_generated() -> list[dict[str, Any]]:
    """Snapshot every server scaffolded under generated/."""
    _ensure_root()
    out: list[dict[str, Any]] = []
    for sdir in sorted(GENERATED_DIR.iterdir()):
        if not sdir.is_dir():
            continue
        try:
            m = _read_manifest(sdir.name)
        except Exception as exc:
            out.append({"name": sdir.name, "error": str(exc)})
            continue
        out.append({
            "name": m.get("name"), "description": m.get("description"),
            "status": m.get("status"), "version": m.get("version"),
            "language": m.get("language"), "planned_tools": m.get("planned_tools", []),
            "last_stage": m.get("last_stage"), "last_stage_ok": m.get("last_stage_ok"),
            "created_at": m.get("created_at"), "updated_at": m.get("updated_at"),
        })
    return out


def get_generated(name: str) -> dict[str, Any]:
    """Full manifest for one generated server (incl. history)."""
    return _read_manifest(name)


def clean_workspace(scope: str = "test_runs") -> dict[str, Any]:
    """Drop runtime artifacts.

      scope='test_runs' → wipe TEST_RUNS_DIR/* (default; safest)
      scope='all'       → wipe entire mcp_workspace/ (DANGEROUS — also kills generated/)
      scope='<name>'    → drop one server's directory + .venv

    Returns counts so the agent can report back.
    """
    _ensure_root()
    if scope == "test_runs":
        deleted = 0
        for p in TEST_RUNS_DIR.glob("*"):
            try:
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink()
                deleted += 1
            except Exception:
                pass
        return {"scope": "test_runs", "deleted_entries": deleted}

    if scope == "all":
        # Refuse without explicit nuke=True at the route layer; protect the agent
        # from accidentally vaporizing its in-progress generated servers.
        raise PermissionError(
            "clean_workspace(scope='all') is destructive — must be triggered "
            "from the dashboard with explicit confirmation"
        )

    # name-scoped
    if not NAME_RE.match(scope):
        raise ValueError(f"invalid scope/name {scope!r}")
    sdir = _server_dir(scope)
    if not sdir.exists():
        raise LookupError(f"server {scope!r} not in workspace")
    shutil.rmtree(sdir)
    return {"scope": scope, "removed": str(sdir)}
