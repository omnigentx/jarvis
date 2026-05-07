"""Per-agent MCP server allowlist — DB-backed CRUD over AgentMcpAttachmentModel.

Pattern mirrors skills/agent_cards: the @fast.agent(servers=[...]) decorator
in agent.py is the **first-boot seed**, after which DB rows become the source
of truth. apply_to_runtime() mutates AgentConfig.servers on every static agent
so subsequent MCPAggregator factory calls see the DB list.

Runtime attach/detach:
  * attach(): insert (agent, server) row + call aggregator.attach_server(...)
  * detach(): call aggregator.detach_server(...) + delete row
  * Per-server lock from mcp_catalog.server_lock(name) serializes against
    catalog mutations on the same server.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import select

from core.database import (
    AgentMcpAttachmentModel,
    McpServerModel,
    SessionLocal,
)
from services.mcp_catalog import _row_to_mcp_settings, server_lock
from services.mcp_runtime import audit

logger = logging.getLogger("mcp")


# ── Read ──────────────────────────────────────────────────────────────


def list_for_agent(agent_name: str) -> list[str]:
    with SessionLocal() as db:
        rows = db.execute(
            select(AgentMcpAttachmentModel.server_name).where(
                AgentMcpAttachmentModel.agent_name == agent_name
            )
        ).all()
        return sorted(r[0] for r in rows)


def list_for_server(server_name: str) -> list[str]:
    with SessionLocal() as db:
        rows = db.execute(
            select(AgentMcpAttachmentModel.agent_name).where(
                AgentMcpAttachmentModel.server_name == server_name
            )
        ).all()
        return sorted(r[0] for r in rows)


def list_all() -> dict[str, list[str]]:
    """Return {agent_name: [server_name, ...]}."""
    out: dict[str, list[str]] = {}
    with SessionLocal() as db:
        rows = db.execute(select(AgentMcpAttachmentModel)).scalars().all()
        for r in rows:
            out.setdefault(r.agent_name, []).append(r.server_name)
    for k in out:
        out[k].sort()
    return out


# ── Mutate ────────────────────────────────────────────────────────────


async def attach(agent_name: str, server_name: str, *, actor: str = "user") -> dict[str, Any]:
    """Persist + live-attach. Returns aggregator result (tools_added etc.)."""
    async with server_lock(server_name):
        async with audit("attach", server=server_name, agent=agent_name, actor=actor) as a:
            # 1) DB row (idempotent — primary key dedupes)
            with SessionLocal() as db:
                if not db.get(McpServerModel, server_name):
                    raise LookupError(f"server {server_name!r} not in catalog")
                existing = db.get(
                    AgentMcpAttachmentModel, (agent_name, server_name)
                )
                if existing is None:
                    db.add(
                        AgentMcpAttachmentModel(
                            agent_name=agent_name,
                            server_name=server_name,
                            created_at=time.time(),
                        )
                    )
                    db.commit()
                    a.set(persisted=True)
                else:
                    a.set(persisted=False, already_attached=True)
                row = db.get(McpServerModel, server_name)
                settings = _row_to_mcp_settings(row)

            # 2) Live attach to aggregator (best-effort — log fail but DB persists)
            agg, err = _get_aggregator(agent_name)
            if agg is None:
                a.set(live_attached=False, runtime_warning=err)
                return {
                    "agent": agent_name,
                    "server": server_name,
                    "persisted": True,
                    "live_attached": False,
                    "warning": err,
                    "tools_added": [],
                }

            from fast_agent.mcp.mcp_aggregator import MCPAttachOptions

            result = await agg.attach_server(
                server_name=server_name,
                server_config=settings,
                options=MCPAttachOptions(),
            )
            a.set(
                live_attached=True,
                tools_added=list(result.tools_added),
                tools_total=result.tools_total,
                already_attached=result.already_attached,
            )
            return {
                "agent": agent_name,
                "server": server_name,
                "persisted": True,
                "live_attached": True,
                "tools_added": list(result.tools_added),
                "tools_total": result.tools_total,
                "warnings": list(result.warnings),
            }


async def detach(agent_name: str, server_name: str, *, actor: str = "user") -> dict[str, Any]:
    """Live-detach + remove DB row. In-flight tool calls on this server will EOF."""
    async with server_lock(server_name):
        async with audit("detach", server=server_name, agent=agent_name, actor=actor) as a:
            agg, err = _get_aggregator(agent_name)
            live_detached = False
            tools_removed: list[str] = []
            if agg is not None:
                try:
                    result = await agg.detach_server(server_name)
                    live_detached = result.detached
                    tools_removed = list(result.tools_removed)
                    a.set(live_detached=live_detached, tools_removed=tools_removed)
                except Exception as exc:
                    a.set(live_detach_error=str(exc))
                    # Continue to DB cleanup even if live detach failed
            else:
                a.set(live_detached=False, runtime_warning=err)

            with SessionLocal() as db:
                row = db.get(AgentMcpAttachmentModel, (agent_name, server_name))
                if row:
                    db.delete(row)
                    db.commit()
                    a.set(persisted_delete=True)
                else:
                    a.set(persisted_delete=False)

            return {
                "agent": agent_name,
                "server": server_name,
                "live_detached": live_detached,
                "tools_removed": tools_removed,
                "warning": err,
            }


async def reconnect_all_for_server(server_name: str, *, actor: str = "system") -> dict[str, Any]:
    """After mcp_catalog.update(server) succeeded, fan-out: detach+attach on
    every aggregator that has server_name. Per-agent try/except so one failure
    doesn't block the rest. Returns per-agent status list."""
    affected = list_for_server(server_name)
    results: list[dict[str, Any]] = []
    if not affected:
        return {"server": server_name, "agents": [], "all_ok": True}

    with SessionLocal() as db:
        row = db.get(McpServerModel, server_name)
        if not row:
            raise LookupError(f"server {server_name!r} not found")
        settings = _row_to_mcp_settings(row)

    from fast_agent.mcp.mcp_aggregator import MCPAttachOptions

    for agent_name in affected:
        async with audit("reconnect", server=server_name, agent=agent_name, actor=actor) as a:
            agg, err = _get_aggregator(agent_name)
            if agg is None:
                a.set(ok=False, error=err)
                results.append({"agent": agent_name, "ok": False, "error": err})
                continue
            try:
                await agg.detach_server(server_name)
                attach_result = await agg.attach_server(
                    server_name=server_name,
                    server_config=settings,
                    options=MCPAttachOptions(force_reconnect=True),
                )
                a.set(ok=True, tools_total=attach_result.tools_total)
                results.append(
                    {"agent": agent_name, "ok": True, "tools_total": attach_result.tools_total}
                )
            except Exception as exc:
                a.set(ok=False, error=f"{type(exc).__name__}: {exc}")
                results.append(
                    {"agent": agent_name, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
                )

    return {
        "server": server_name,
        "agents": results,
        "all_ok": all(r["ok"] for r in results),
    }


async def detach_from_all(server_name: str, *, actor: str = "system") -> list[dict[str, Any]]:
    """Live-detach server from every agent that has it; delete all attachment
    rows. Used by routes/mcp.py before mcp_catalog.delete()."""
    affected = list_for_server(server_name)
    results: list[dict[str, Any]] = []
    for agent_name in affected:
        try:
            results.append(await detach(agent_name, server_name, actor=actor))
        except Exception as exc:
            logger.exception(
                "[mcp.detach_from_all] failed for agent=%s server=%s",
                agent_name,
                server_name,
            )
            results.append(
                {"agent": agent_name, "server": server_name, "error": str(exc)}
            )
    return results


# ── Boot wiring ───────────────────────────────────────────────────────


def seed_from_decorator(fast: "Any") -> dict[str, int]:
    """First-boot seed: import @fast.agent(servers=[...]) into the DB.

    Idempotent: existing (agent, server) rows are skipped. New decorator
    additions in agent.py will appear automatically.
    """
    inserted = 0
    skipped = 0
    now = time.time()
    with SessionLocal() as db:
        existing = {
            (r.agent_name, r.server_name)
            for r in db.execute(select(AgentMcpAttachmentModel)).scalars().all()
        }
        catalog_names = {
            r[0] for r in db.execute(select(McpServerModel.name)).all()
        }
        for agent_name, card_data in fast.agents.items():
            cfg = card_data.get("config")
            if cfg is None:
                continue
            for server_name in getattr(cfg, "servers", []) or []:
                if (agent_name, server_name) in existing:
                    skipped += 1
                    continue
                if server_name not in catalog_names:
                    logger.warning(
                        "[mcp.seed_attach] agent=%s declares unknown server=%s; skipping",
                        agent_name,
                        server_name,
                    )
                    continue
                db.add(
                    AgentMcpAttachmentModel(
                        agent_name=agent_name,
                        server_name=server_name,
                        created_at=now,
                    )
                )
                inserted += 1
        db.commit()
    logger.info("[mcp.seed_attach] inserted=%d skipped=%d", inserted, skipped)
    return {"inserted": inserted, "skipped": skipped}


def apply_to_runtime(fast: "Any") -> int:
    """Override AgentConfig.servers on every static agent with DB content.
    MUST be called BEFORE `async with fast.run()` so MCPAggregator factory
    sees the right server list."""
    snapshot = list_all()
    overridden = 0
    for agent_name, card_data in fast.agents.items():
        cfg = card_data.get("config")
        if cfg is None:
            continue
        cfg.servers = snapshot.get(agent_name, [])
        overridden += 1
    logger.info("[mcp.apply_attach] overrode %d agent server lists", overridden)
    return overridden


# ── Helpers ───────────────────────────────────────────────────────────


def _get_aggregator(agent_name: str) -> tuple["Any | None", str | None]:
    """Returns (aggregator, error_message). aggregator=None means runtime not ready."""
    from services import shared_state

    agent_app = shared_state.agent_app
    if agent_app is None:
        return None, "agent_app not initialized"
    ag = agent_app.get_agent(agent_name) if hasattr(agent_app, "get_agent") else None
    if ag is None:
        return None, f"agent {agent_name!r} not found in runtime"
    agg = getattr(ag, "_aggregator", None)
    if agg is None:
        return None, f"agent {agent_name!r} has no MCP aggregator"
    return agg, None
