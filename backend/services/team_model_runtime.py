"""Per-call model selection for isolated team agents.

Overrides are keyed by team session and logical agent name. A resumed agent
gets a new run ID, so run-scoped storage would silently lose its selection.
Audit rows are kept separately. The local RPC socket shares the trusted-shell
boundary; MCP callers come from fast-agent transport metadata, not LLM args.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import yaml

logger = logging.getLogger(__name__)
_CONFIG = Path(__file__).resolve().parent.parent / "fastagent.config.yaml"


@lru_cache(maxsize=1)
def _default_model() -> str:
    try:
        return str((yaml.safe_load(_CONFIG.read_text(encoding="utf-8")) or {}).get("default_model") or "")
    except (OSError, yaml.YAMLError):
        logger.exception("[MODEL] Could not load configured default model")
        return ""


class ModelChangeError(ValueError):
    pass


class ModelChangeConflict(ModelChangeError):
    pass


class ModelChangeForbidden(ModelChangeError):
    pass


class ModelUnavailable(ModelChangeError):
    pass


def _connect() -> sqlite3.Connection:
    path = os.environ.get("SPAWN_REGISTRY_DB", "data/jarvis.db")
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS team_model_configs ("
        "session_id TEXT NOT NULL, agent_name TEXT NOT NULL, model TEXT NOT NULL, "
        "revision INTEGER NOT NULL, updated_at REAL NOT NULL, "
        "PRIMARY KEY (session_id, agent_name))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS team_model_active_calls ("
        "run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, model TEXT NOT NULL, revision INTEGER NOT NULL, "
        "pid INTEGER NOT NULL, started_at REAL NOT NULL)"
    )
    return conn


def _record(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT data_json FROM spawn_registry WHERE run_id = ?", (run_id,)
    ).fetchone()
    return json.loads(row["data_json"]) if row else None


def _configured(conn: sqlite3.Connection, record: dict[str, Any]) -> tuple[str | None, int]:
    row = conn.execute(
        "SELECT model, revision FROM team_model_configs WHERE session_id = ? AND agent_name = ?",
        (record.get("session_id"), record.get("agent_name")),
    ).fetchone()
    return (row["model"], row["revision"]) if row else (None, 0)


def _audit(
    conn: sqlite3.Connection, run_id: str, actor: str, requested_model: str,
    outcome: str, correlation_id: str, *, previous_model: str = "",
    revision: int = 0, agent_name: str = "",
) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS team_model_change_audit ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, correlation_id TEXT NOT NULL, "
        "run_id TEXT NOT NULL, agent_name TEXT NOT NULL, actor TEXT NOT NULL, "
        "previous_model TEXT, requested_model TEXT NOT NULL, revision INTEGER NOT NULL, "
        "outcome TEXT NOT NULL, created_at REAL NOT NULL)"
    )
    conn.execute(
        "INSERT INTO team_model_change_audit "
        "(correlation_id, run_id, agent_name, actor, previous_model, requested_model, "
        "revision, outcome, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (correlation_id, run_id, agent_name, actor, previous_model,
         requested_model, revision, outcome, time.time()),
    )


def _validate_model(model_id: str) -> str:
    # A running OpenAI-compatible LLM cannot be swapped to another provider
    # without rebuilding its client. Keep the supported boundary explicit.
    if not isinstance(model_id, str) or not model_id.startswith("openai."):
        raise ModelChangeError("running agents support openai.<model> only")
    provider_model = model_id.removeprefix("openai.")
    if not provider_model or len(provider_model) > 128 or any(c.isspace() for c in provider_model):
        raise ModelChangeError("invalid OpenAI-compatible model ID")
    return provider_model


def _authorize(conn: sqlite3.Connection, requester: str, target: dict[str, Any], target_run_id: str) -> None:
    session_id = target.get("session_id")
    if not session_id:
        raise ModelChangeForbidden("target is not a team agent")
    rows = conn.execute("SELECT run_id, data_json FROM spawn_registry").fetchall()
    records = [(row["run_id"], json.loads(row["data_json"])) for row in rows]
    matches = [
        (run_id, candidate) for run_id, candidate in records
        if candidate.get("agent_name") == requester
        and candidate.get("session_id") == session_id
    ]
    # A resumed agent can have several run records in one session. Its name
    # remains the logical identity; all those records must agree on role.
    if not matches or len({candidate.get("role") for _, candidate in matches}) != 1:
        raise ModelChangeForbidden("caller identity is absent or ambiguous in this team")
    caller = matches[0][1]
    # Capability and hierarchy come from the persisted session template,
    # never from the caller's tool arguments or instruction text.
    row = conn.execute(
        "SELECT data_json FROM team_sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    team = json.loads(row["data_json"]) if row else {}
    template = team.get("template") or {}
    orchestrator_role = template.get("orchestrator")
    roles = template.get("roles") or {}
    caller_role = caller.get("role")
    target_role = target.get("role")
    if not orchestrator_role or caller_role not in roles or target_role not in roles:
        raise ModelChangeForbidden("team has no recorded orchestrator")
    if "model-management" not in (roles[caller_role].get("capabilities") or []):
        raise ModelChangeForbidden("caller lacks model-management capability")
    if requester == target.get("agent_name"):
        return
    if caller_role != orchestrator_role or target_role == orchestrator_role:
        raise ModelChangeForbidden("only the team orchestrator may change another agent")


def snapshot(run_id: str, *, caller_agent: str | None = None) -> dict[str, Any]:
    with _connect() as conn:
        record = _record(conn, run_id)
        if record is not None and caller_agent is not None:
            _authorize(conn, caller_agent, record, run_id)
        configured = _configured(conn, record) if record is not None else (None, 0)
        active = conn.execute(
            "SELECT model, revision, pid, started_at FROM team_model_active_calls WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    if record is None:
        raise ModelChangeError("agent run not found")
    override, revision = configured
    base = (record.get("original_config") or {}).get("model") or _default_model()
    active_model = None
    active_revision = None
    if active and time.time() - active["started_at"] < 1800:
        try:
            os.kill(active["pid"], 0)
            active_model = active["model"]
            active_revision = active["revision"]
        except (ProcessLookupError, PermissionError):
            pass
    return {
        "run_id": run_id,
        "session_id": record.get("session_id") or "",
        "model": override or base,
        "model_revision": revision,
        "overridden": bool(override),
        "configured_model": override or base,
        "active_model": active_model,
        "active_revision": active_revision,
    }


def _change_model_inner(
    run_id: str, model_id: str, expected_revision: int, *, actor: str,
    caller_agent: str | None, correlation_id: str,
) -> dict[str, Any]:
    _validate_model(model_id)
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 0:
        raise ModelChangeError("expected_revision must be a non-negative integer")
    if not run_id or not actor:
        raise ModelChangeError("run_id and actor are required")
    # Check authorization before querying the model catalog, so an agent
    # cannot use model errors to probe another team. Do network I/O before
    # taking the SQLite write lock; recheck authorization in the transaction.
    with _connect() as read_conn:
        initial = _record(read_conn, run_id)
        if initial is None:
            raise ModelChangeError("agent run not found")
        if not initial.get("session_id") or not initial.get("agent_name"):
            raise ModelChangeError("target is not a team agent")
        if caller_agent is not None:
            _authorize(read_conn, caller_agent, initial, run_id)
    from services.model_catalog import CatalogUnavailable, available_models
    try:
        if model_id.removeprefix("openai.") not in available_models():
            raise ModelUnavailable(f"model {model_id!r} is unavailable in the gateway catalog")
    except CatalogUnavailable as exc:
        raise ModelUnavailable(str(exc)) from exc
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        record = _record(conn, run_id)
        if record is None:
            raise ModelChangeError("agent run not found")
        if not record.get("session_id") or not record.get("agent_name"):
            raise ModelChangeError("target is not a team agent")
        if caller_agent is not None:
            _authorize(conn, caller_agent, record, run_id)
        override, current = _configured(conn, record)
        if current != expected_revision:
            raise ModelChangeConflict(f"expected revision {expected_revision}, current {current}")
        previous = override or (record.get("original_config") or {}).get("model") or _default_model()
        if model_id == previous:
            _audit(conn, run_id, actor, model_id, "unchanged", correlation_id,
                   previous_model=previous, revision=current,
                   agent_name=record.get("agent_name", ""))
            return {"run_id": run_id, "target_agent": record.get("agent_name", ""),
                    "actor": actor, "previous_model": previous, "model": model_id,
                    "model_revision": current, "changed": False,
                    "correlation_id": correlation_id, "timestamp": time.time()}
        conn.execute(
            "INSERT INTO team_model_configs (session_id, agent_name, model, revision, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(session_id, agent_name) DO UPDATE SET "
            "model = excluded.model, revision = excluded.revision, updated_at = excluded.updated_at",
            (record.get("session_id"), record.get("agent_name"), model_id, current + 1, time.time()),
        )
        _audit(conn, run_id, actor, model_id, "success", correlation_id,
               previous_model=previous, revision=current + 1,
               agent_name=record.get("agent_name", ""))
    return {
        "run_id": run_id, "target_agent": record.get("agent_name", ""),
        "actor": actor, "previous_model": previous, "model": model_id,
        "model_revision": current + 1, "changed": True,
        "correlation_id": correlation_id, "timestamp": time.time(),
    }


def change_model(
    run_id: str, model_id: str, expected_revision: int, *, actor: str,
    caller_agent: str | None = None,
) -> dict[str, Any]:
    """Validate, authorize, commit and audit each attempted model change."""
    correlation_id = uuid.uuid4().hex
    try:
        return _change_model_inner(
            run_id, model_id, expected_revision, actor=actor,
            caller_agent=caller_agent, correlation_id=correlation_id,
        )
    except ModelChangeError as exc:
        outcome = (
            "forbidden" if isinstance(exc, ModelChangeForbidden) else
            "conflict" if isinstance(exc, ModelChangeConflict) else
            "unavailable" if isinstance(exc, ModelUnavailable) else "invalid"
        )
        try:
            with _connect() as conn:
                record = _record(conn, run_id)
                override, revision = _configured(conn, record) if record else (None, 0)
                previous = override or ((record or {}).get("original_config") or {}).get("model") or _default_model()
                _audit(conn, run_id, actor, str(model_id), outcome, correlation_id,
                       previous_model=previous, revision=revision,
                       agent_name=(record or {}).get("agent_name", ""))
        except sqlite3.Error:
            logger.exception("[MODEL] Could not persist failed attempt audit")
        raise


def delete_session_overrides(session_id: str) -> int:
    """Remove live configuration when the owning team session is deleted."""
    with _connect() as conn:
        conn.execute("DELETE FROM team_model_active_calls WHERE session_id = ?", (session_id,))
        cur = conn.execute("DELETE FROM team_model_configs WHERE session_id = ?", (session_id,))
        return cur.rowcount


def _set_active(run_id: str, session_id: str, model: str, revision: int) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO team_model_active_calls (run_id, session_id, model, revision, pid, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET "
            "model = excluded.model, revision = excluded.revision, "
            "pid = excluded.pid, started_at = excluded.started_at",
            (run_id, session_id, model, revision, os.getpid(), time.time()),
        )


def _clear_active(run_id: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM team_model_active_calls WHERE run_id = ?", (run_id,))


def create_model_hook(
    run_id: str, agent_name: str = "",
    emit_event: Callable[..., None] | None = None,
):
    """Read once at each LLM boundary; a change affects the next call."""
    from fast_agent.agents.tool_runner import ToolRunnerHooks
    from fast_agent.llm.request_params import RequestParams

    async def before_llm_call(runner, _messages) -> None:
        state = snapshot(run_id)
        if state["overridden"]:
            provider_model = _validate_model(state["model"])
            current = runner.request_params
            params = current.model_copy(update={"model": provider_model}) if current else RequestParams(model=provider_model)
            runner.set_request_params(params)
        _set_active(run_id, state["session_id"], state["model"], state["model_revision"])
        if emit_event:
            emit_event("model_call_started", run_id, agent_name,
                       model=state["model"], revision=state["model_revision"])
        logger.info("[MODEL] run=%s revision=%s model=%s", run_id, state["model_revision"], state["model"])

    async def after_llm_call(_runner, _message) -> None:
        _clear_active(run_id)
        if emit_event:
            emit_event("model_call_finished", run_id, agent_name)

    async def after_turn_complete(_runner, _message) -> None:
        _clear_active(run_id)

    return ToolRunnerHooks(
        before_llm_call=before_llm_call,
        after_llm_call=after_llm_call,
        after_turn_complete=after_turn_complete,
    )


def attach_model_hook(
    agent_app: Any, run_id: str, agent_name: str,
    emit_event: Callable[..., None] | None = None,
) -> bool:
    from services.sse_progress import merge_hooks

    # Generic fast-agent subprocesses and lightweight test harnesses have no
    # Jarvis registry. They have no live team model configuration to read.
    try:
        with _connect() as conn:
            if _record(conn, run_id) is None:
                return False
    except sqlite3.OperationalError as exc:
        if "no such table: spawn_registry" in str(exc):
            return False
        raise

    agent = (getattr(agent_app, "_agents", {}) or {}).get(agent_name)
    if agent is None or getattr(agent, "_jarvis_model_hook", False):
        return False
    hook = create_model_hook(run_id, agent_name, emit_event)
    existing = agent.tool_runner_hooks
    agent.tool_runner_hooks = merge_hooks(existing, hook) if existing else hook
    agent._jarvis_model_hook = True
    return True
