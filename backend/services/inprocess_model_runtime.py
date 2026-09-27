"""Live model overrides for in-process built-in and DB-defined agents.

Shares the persisted override and audit tables with isolated team agents.
"""
from __future__ import annotations

import os
import time
import uuid
from contextvars import ContextVar
from typing import Any

from services.team_model_runtime import (
    ModelChangeConflict, ModelChangeError, ModelUnavailable,
    _audit, _configured, _connect, _default_model, _validate_model,
)

_INPROCESS_SESSION = "@inprocess"


def inprocess_snapshot(agent_name: str, base_model: str) -> dict[str, Any]:
    """Effective model for a built-in or DB-defined agent, including active calls."""
    with _connect() as conn:
        override, revision = _configured(conn, {
            "session_id": _INPROCESS_SESSION, "agent_name": agent_name,
        })
        rows = conn.execute(
            "SELECT model, revision, pid, started_at FROM inprocess_model_active_calls "
            "WHERE agent_name = ? ORDER BY started_at DESC", (agent_name,),
        ).fetchall()
    live = []
    for row in rows:
        if time.time() - row["started_at"] >= 1800:
            continue
        try:
            os.kill(row["pid"], 0)
        except (ProcessLookupError, PermissionError):
            continue
        live.append(row)
    return {
        "base_model": base_model,
        "model": override or base_model,
        "configured_model": override or base_model,
        "model_revision": revision,
        "overridden": bool(override),
        "active_model": live[0]["model"] if live else None,
        "active_revision": live[0]["revision"] if live else None,
        "active_call_count": len(live),
    }


def inprocess_overrides() -> dict[str, str]:
    """One read for the Agent List, avoiding a DB open per agent row."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT agent_name, model FROM team_model_configs "
            "WHERE session_id = ? AND model <> ''", (_INPROCESS_SESSION,),
        ).fetchall()
    return {row["agent_name"]: row["model"] for row in rows}


def change_inprocess_model(
    agent_name: str, base_model: str, model_id: str, expected_revision: int,
    *, actor: str,
) -> dict[str, Any]:
    """CAS update in the same SQLite override store used by team members."""
    correlation_id = uuid.uuid4().hex
    try:
        if model_id:
            _validate_model(model_id)
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
            raise ModelChangeError("expected_revision must be a non-negative integer")
        if model_id:
            from services.model_catalog import CatalogUnavailable, available_models, probe_model
            try:
                if model_id.removeprefix("openai.") not in available_models():
                    raise ModelUnavailable(f"model {model_id!r} is unavailable in the gateway catalog")
                probe_model(model_id.removeprefix("openai."))
            except CatalogUnavailable as exc:
                raise ModelUnavailable(str(exc)) from exc
        with _connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            key = {"session_id": _INPROCESS_SESSION, "agent_name": agent_name}
            override, revision = _configured(conn, key)
            previous = override or base_model
            if expected_revision != revision:
                raise ModelChangeConflict(f"expected revision {expected_revision}, current {revision}")
            changed = (override or "") != model_id
            if changed:
                conn.execute(
                    "INSERT INTO team_model_configs (session_id, agent_name, model, revision, updated_at) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT(session_id, agent_name) DO UPDATE SET "
                    "model = excluded.model, revision = excluded.revision, updated_at = excluded.updated_at",
                    (_INPROCESS_SESSION, agent_name, model_id, revision + 1, time.time()),
                )
            _audit(conn, f"inprocess:{agent_name}", actor, model_id,
                   "success" if changed else "unchanged", correlation_id,
                   previous_model=previous, revision=revision + int(changed), agent_name=agent_name)
        return {"target_agent": agent_name, "actor": actor, "previous_model": previous,
                "model": model_id or base_model, "model_revision": revision + int(changed),
                "changed": changed, "correlation_id": correlation_id, "timestamp": time.time()}
    except ModelChangeError as exc:
        outcome = (
            "conflict" if isinstance(exc, ModelChangeConflict) else
            "unavailable" if isinstance(exc, ModelUnavailable) else "invalid"
        )
        with _connect() as conn:
            override, revision = _configured(conn, {
                "session_id": _INPROCESS_SESSION, "agent_name": agent_name,
            })
            _audit(conn, f"inprocess:{agent_name}", actor, str(model_id),
                   outcome, correlation_id, previous_model=override or base_model,
                   revision=revision, agent_name=agent_name)
        raise


def attach_inprocess_hooks(agent_app: Any) -> int:
    """Attach once per live agent; re-run after dynamic-definition reloads."""
    from fast_agent.agents.tool_runner import ToolRunnerHooks
    from services.sse_progress import merge_hooks
    from agent import fast

    count = 0
    for name, runner in (getattr(agent_app, "_agents", {}) or {}).items():
        if getattr(runner, "_jarvis_inprocess_model_hook", False):
            continue
        call_id: ContextVar[str | None] = ContextVar(f"model_call_{name}", default=None)

        async def before_llm_call(llm, _messages, *, agent_name=name, context=call_id):
            config = (fast.agents.get(agent_name) or {}).get("config")
            base = getattr(config, "model", None) or _default_model()
            model_state = inprocess_snapshot(agent_name, base)
            if model_state["model"].startswith("openai."):
                from fast_agent.llm.request_params import RequestParams
                provider_model = _validate_model(model_state["model"])
                current = llm.request_params
                params = current.model_copy(update={"model": provider_model}) if current else RequestParams(model=provider_model)
                llm.set_request_params(params)
            identity = uuid.uuid4().hex
            context.set(identity)
            with _connect() as conn:
                conn.execute(
                    "INSERT INTO inprocess_model_active_calls "
                    "(call_id, agent_name, model, revision, pid, started_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (identity, agent_name, model_state["model"], model_state["model_revision"], os.getpid(), time.time()),
                )

        async def clear_call(_llm, _message, *, context=call_id):
            identity = context.get()
            if identity:
                with _connect() as conn:
                    conn.execute("DELETE FROM inprocess_model_active_calls WHERE call_id = ?", (identity,))
                context.set(None)

        hook = ToolRunnerHooks(before_llm_call=before_llm_call,
                               after_llm_call=clear_call, after_turn_complete=clear_call)
        existing = runner.tool_runner_hooks
        runner.tool_runner_hooks = merge_hooks(existing, hook) if existing else hook
        runner._jarvis_inprocess_model_hook = True
        count += 1
    return count
