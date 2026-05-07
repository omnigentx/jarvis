"""MCP server catalog — DB-backed CRUD over McpServerModel.

Single source of truth for MCP server definitions. Built-in servers are seeded
from fastagent.config.yaml on first boot (idempotent upsert: insert if missing,
never overwrite). User-created servers are persisted with is_builtin=False.

After seed, the live ServerRegistry (fast-agent context.server_registry) is
overridden via apply_to_registry() so that aggregator attach/detach uses the
DB-resolved settings rather than the YAML snapshot.

Concurrency: a per-server-name asyncio.Lock serializes mutations so concurrent
update + attach + delete on the same server cannot interleave.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select

from core.database import McpServerModel, SessionLocal
from services.mcp_runtime import audit, resolve_env

logger = logging.getLogger("mcp")

NAME_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9_\-]{0,62}[a-zA-Z0-9])?$")
VALID_TRANSPORTS = {"stdio", "http", "sse"}

_FASTAGENT_CONFIG = Path(__file__).parent.parent / "fastagent.config.yaml"
_FASTAGENT_SECRETS = Path(__file__).parent.parent / "fastagent.secrets.yaml"

_SECRET_KEY_RE = re.compile(r"(TOKEN|KEY|SECRET|PASSWORD|CREDENTIAL)", re.IGNORECASE)

_locks: dict[str, asyncio.Lock] = {}


def server_lock(name: str) -> asyncio.Lock:
    """Per-server lock; lazily created. Same instance across calls."""
    lock = _locks.get(name)
    if lock is None:
        lock = asyncio.Lock()
        _locks[name] = lock
    return lock


# ── (de)serialization ──────────────────────────────────────────────────


def _row_to_dict(row: McpServerModel, *, mask_secrets: bool = True) -> dict[str, Any]:
    env = json.loads(row.env_json) if row.env_json else {}
    if mask_secrets:
        env = {k: ("••••" if _is_secret_key(k) else v) for k, v in env.items()}
    return {
        "name": row.name,
        "transport": row.transport,
        "command": row.command,
        "args": json.loads(row.args_json) if row.args_json else [],
        "env": env,
        "url": row.url,
        "cwd": row.cwd,
        "is_builtin": bool(row.is_builtin),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _is_secret_key(key: str) -> bool:
    return bool(_SECRET_KEY_RE.search(key))


def _row_to_mcp_settings(row: McpServerModel) -> "Any":
    """Convert a DB row to fast-agent's MCPServerSettings (env placeholders resolved)."""
    from fast_agent.config import MCPServerSettings

    env = json.loads(row.env_json) if row.env_json else None
    args = json.loads(row.args_json) if row.args_json else None

    return MCPServerSettings(
        name=row.name,
        transport=row.transport,
        command=row.command,
        args=args,
        env=resolve_env(env) if env else None,
        url=row.url,
        cwd=row.cwd,
    )


# ── Validation ────────────────────────────────────────────────────────


def validate_payload(name: str, payload: dict[str, Any]) -> None:
    if not NAME_RE.match(name):
        raise ValueError(
            f"invalid server name {name!r}: must match {NAME_RE.pattern}"
        )
    transport = payload.get("transport")
    if transport not in VALID_TRANSPORTS:
        raise ValueError(f"transport must be one of {sorted(VALID_TRANSPORTS)}")
    if transport == "stdio":
        if not payload.get("command"):
            raise ValueError("stdio transport requires 'command'")
    else:
        if not payload.get("url"):
            raise ValueError(f"{transport} transport requires 'url'")
    args = payload.get("args")
    if args is not None and not isinstance(args, list):
        raise ValueError("'args' must be a list of strings")
    env = payload.get("env")
    if env is not None and not isinstance(env, dict):
        raise ValueError("'env' must be a dict[str, str]")


# ── Read ──────────────────────────────────────────────────────────────


def list_all(*, mask_secrets: bool = True) -> list[dict[str, Any]]:
    with SessionLocal() as db:
        rows = db.execute(select(McpServerModel).order_by(McpServerModel.name)).scalars().all()
        return [_row_to_dict(r, mask_secrets=mask_secrets) for r in rows]


def get(name: str, *, mask_secrets: bool = True) -> dict[str, Any] | None:
    with SessionLocal() as db:
        row = db.get(McpServerModel, name)
        return _row_to_dict(row, mask_secrets=mask_secrets) if row else None


def get_secret_value(name: str, env_key: str) -> str | None:
    """Reveal a single env value (for the UI eye icon). Returns None if missing."""
    with SessionLocal() as db:
        row = db.get(McpServerModel, name)
        if not row or not row.env_json:
            return None
        env = json.loads(row.env_json)
        return env.get(env_key)


# ── Mutate ────────────────────────────────────────────────────────────


async def create(name: str, payload: dict[str, Any], *, actor: str = "user") -> dict[str, Any]:
    """Create a user server. Runs smoke_test first; only persists on success."""
    validate_payload(name, payload)
    async with server_lock(name):
        with SessionLocal() as db:
            if db.get(McpServerModel, name):
                raise ValueError(f"server {name!r} already exists")

        async with audit(
            "create", server=name, actor=actor,
            detail={"transport": payload.get("transport")},
        ) as a:
            test_result = await smoke_test(payload)
            a.set(smoke_ok=test_result["ok"], tools=test_result.get("tools"))
            if not test_result["ok"]:
                raise RuntimeError(f"smoke test failed: {test_result.get('error')}")
            now = time.time()
            with SessionLocal() as db:
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
                return _row_to_dict(row)


async def update(name: str, patch: dict[str, Any], *, actor: str = "user") -> dict[str, Any]:
    """Update an existing server (built-in editable too). No reconnect here —
    callers (routes) decide whether to fan out to attached agents."""
    async with server_lock(name):
        with SessionLocal() as db:
            row = db.get(McpServerModel, name)
            if not row:
                raise LookupError(f"server {name!r} not found")
            # Build merged config for re-validation
            merged = _row_to_dict(row, mask_secrets=False)
            merged.update({k: v for k, v in patch.items() if k in {"transport", "command", "args", "env", "url", "cwd"}})
            validate_payload(name, merged)

        async with audit("update", server=name, actor=actor) as a:
            test_result = await smoke_test(merged)
            a.set(smoke_ok=test_result["ok"])
            if not test_result["ok"]:
                raise RuntimeError(f"smoke test failed: {test_result.get('error')}")

            with SessionLocal() as db:
                row = db.get(McpServerModel, name)
                if not row:
                    raise LookupError(f"server {name!r} disappeared")
                row.transport = merged["transport"]
                row.command = merged.get("command")
                row.args_json = json.dumps(merged.get("args") or [])
                row.env_json = json.dumps(merged.get("env") or {})
                row.url = merged.get("url")
                row.cwd = merged.get("cwd")
                row.updated_at = time.time()
                db.commit()
                return _row_to_dict(row)


async def delete(name: str, *, actor: str = "user") -> dict[str, Any]:
    """Delete a server. Built-ins are protected (raises PermissionError).

    Cascade detaches via FK ON DELETE CASCADE on agent_mcp_attachments,
    but the live aggregators must be detached separately by the caller
    (routes/mcp.py) BEFORE calling this.
    """
    async with server_lock(name):
        async with audit("delete", server=name, actor=actor) as a:
            with SessionLocal() as db:
                row = db.get(McpServerModel, name)
                if not row:
                    raise LookupError(f"server {name!r} not found")
                if row.is_builtin:
                    raise PermissionError(f"server {name!r} is built-in; cannot delete")
                snapshot = _row_to_dict(row)
                db.delete(row)
                db.commit()
                a.set(deleted=snapshot)
                return {"deleted": True, "name": name}


# ── Smoke test ────────────────────────────────────────────────────────


async def smoke_test(
    payload: dict[str, Any],
    *,
    timeout: float = 15.0,
    return_tool_details: bool = False,
) -> dict[str, Any]:
    """Briefly launch a server, list its tools, then tear down.

    Returns {"ok": bool, "tools": list[str], "error": str|None}. When
    `return_tool_details=True` also includes "tool_details": list of
    {"name", "description"} dicts — used by routes/mcp.py to refresh the
    mcp_server_tools cache after a manual Test trigger.

    Used by:
      - POST /api/mcp/servers (before persisting)
      - PUT  /api/mcp/servers/{name} (before persisting changes)
      - POST /api/mcp/servers/{name}/test (manual user trigger; refreshes cache)

    Does NOT touch the DB directly. Does NOT mutate the live ServerRegistry
    permanently (it adds a temp entry, runs, then removes it).
    """
    from mcp.client.session import ClientSession

    from services import shared_state

    agent_app = shared_state.agent_app
    if agent_app is None:
        return {"ok": False, "error": "agent_app not initialized", "tools": []}

    # AgentApp.__getattr__ resolves agent-name lookups; it has no .context or
    # .app attribute. Each individual agent carries the live fast-agent
    # context, so grab it from any one.
    context = None
    agents_dict = getattr(agent_app, "_agents", {}) or {}
    for ag in agents_dict.values():
        ctx = getattr(ag, "context", None)
        if ctx is not None:
            context = ctx
            break
    if context is None:
        return {"ok": False, "error": "fast-agent context not available", "tools": []}

    server_registry = getattr(context, "server_registry", None)
    if server_registry is None:
        return {"ok": False, "error": "server_registry missing on context", "tools": []}

    smoke_name = f"__smoke_{int(time.time() * 1000)}__"
    settings = _payload_to_mcp_settings(smoke_name, payload)
    server_registry.registry[smoke_name] = settings

    # Lazy import to avoid module-load coupling
    from fast_agent.mcp.mcp_connection_manager import MCPConnectionManager

    try:
        async def _run() -> dict[str, Any]:
            # ServerConnection.create_session calls factory with extra kwargs
            # (server_config=, transport_metrics=); accept and ignore them.
            def _factory(read, write, read_timeout, **_kwargs):
                return ClientSession(read, write, read_timeout)

            async with MCPConnectionManager(server_registry, context=context) as cm:
                conn = await cm.launch_server(
                    smoke_name,
                    client_session_factory=_factory,
                    startup_timeout_seconds=timeout,
                    trigger_oauth=False,
                )
                await conn.wait_for_initialized()
                if getattr(conn, "_error_occurred", False) or conn.session is None:
                    err = getattr(conn, "_error_message", None) or "session not initialized"
                    return {"ok": False, "error": err, "tools": []}
                tools_result = await conn.session.list_tools()
                raw_tools = list(tools_result.tools or [])
                tool_names = [t.name for t in raw_tools]
                await cm.disconnect_server(smoke_name)
                out: dict[str, Any] = {"ok": True, "tools": tool_names, "error": None}
                if return_tool_details:
                    out["tool_details"] = [
                        {"name": t.name, "description": getattr(t, "description", "") or ""}
                        for t in raw_tools
                    ]
                return out

        return await asyncio.wait_for(_run(), timeout=timeout + 5.0)
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"timed out after {timeout}s", "tools": []}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "tools": []}
    finally:
        server_registry.registry.pop(smoke_name, None)


def _payload_to_mcp_settings(name: str, payload: dict[str, Any]) -> "Any":
    from fast_agent.config import MCPServerSettings

    env = payload.get("env") or None
    return MCPServerSettings(
        name=name,
        transport=payload["transport"],
        command=payload.get("command"),
        args=payload.get("args") or None,
        env=resolve_env(env) if env else None,
        url=payload.get("url"),
        cwd=payload.get("cwd"),
    )


# ── Boot-time seed ────────────────────────────────────────────────────


def seed_from_yaml(
    config_path: Path = _FASTAGENT_CONFIG,
    secrets_path: Path = _FASTAGENT_SECRETS,
) -> dict[str, int]:
    """Idempotent insert: every server in fastagent.config.yaml's mcp.servers
    not yet in DB is inserted with is_builtin=True. Existing rows are LEFT
    UNTOUCHED so user-edited values survive a yaml change.

    Called once during server.py lifespan, BEFORE apply_to_registry.
    Returns {"inserted": n, "skipped": n}.
    """
    if not config_path.exists():
        logger.warning("[mcp.seed] config not found: %s", config_path)
        return {"inserted": 0, "skipped": 0}

    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}
    declared = raw.get("mcp", {}).get("servers", {}) or {}

    # Merge secrets overrides (same approach as server.py:_discover_uncached_servers)
    if secrets_path.exists():
        try:
            with open(secrets_path) as f:
                sec_raw = yaml.safe_load(f) or {}
            sec_servers = sec_raw.get("mcp", {}).get("servers", {}) or {}
            for sname, scfg in sec_servers.items():
                base = declared.get(sname, {})
                merged = {**base, **scfg}
                if "env" in base or "env" in scfg:
                    merged["env"] = {**(base.get("env") or {}), **(scfg.get("env") or {})}
                declared[sname] = merged
        except Exception:
            logger.exception("[mcp.seed] failed to merge secrets overlay")

    inserted = 0
    skipped = 0
    now = time.time()
    with SessionLocal() as db:
        existing = {r.name for r in db.execute(select(McpServerModel.name)).all()}
        for sname, cfg in declared.items():
            if sname in existing:
                skipped += 1
                continue
            transport = cfg.get("transport") or ("http" if cfg.get("url") else "stdio")
            row = McpServerModel(
                name=sname,
                transport=transport,
                command=cfg.get("command"),
                args_json=json.dumps(cfg.get("args") or []),
                env_json=json.dumps(cfg.get("env") or {}),
                url=cfg.get("url"),
                cwd=cfg.get("cwd"),
                is_builtin=True,
                created_at=now,
                updated_at=now,
            )
            db.add(row)
            inserted += 1
        db.commit()
    logger.info("[mcp.seed] inserted=%d skipped=%d", inserted, skipped)
    return {"inserted": inserted, "skipped": skipped}


def apply_to_registry(context: "Any") -> int:
    """Replace context.server_registry.registry contents with DB-resolved
    MCPServerSettings. Called once on boot AFTER seed_from_yaml.

    Returns the count of servers loaded into the registry.
    """
    server_registry = getattr(context, "server_registry", None)
    if server_registry is None:
        logger.warning("[mcp.apply] context has no server_registry")
        return 0

    with SessionLocal() as db:
        rows = db.execute(select(McpServerModel)).scalars().all()
        new_registry = {row.name: _row_to_mcp_settings(row) for row in rows}
        server_registry.registry = new_registry

    logger.info("[mcp.apply] loaded %d servers into ServerRegistry", len(new_registry))
    return len(new_registry)
