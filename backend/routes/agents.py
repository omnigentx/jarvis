"""
Agent Management API Routes.
CRUD operations for dynamic agent cards + listing all agents (static + dynamic).
Skills are read from FastAgent runtime AgentConfig (single source of truth).
Activities are persisted in SQLite and streamed via SSE.
"""
import os
import re
import logging
import sqlite3
from pathlib import Path
from typing import Any

import json as _json
import yaml
from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.auth import verify_api_key
from agent import fast
import time as _time


def _fetch_latest_snapshots_batch(
    agent_names: list[str], snapshots_db_path: str | None,
) -> dict[str, tuple[str | None, float | None]]:
    """Batch-fetch the latest snapshot ``(trigger, created_at)`` for every
    agent name in one SQLite query.

    Replaces per-agent ``SELECT ... WHERE agent_name = ?`` with a single
    ``WHERE agent_name IN (...)`` + ``GROUP BY``. For a 30-agent roster
    this drops 30 round-trips to 1. Returns a dict keyed by agent_name;
    missing entries means the agent has no snapshot yet.

    Empty list short-circuits to ``{}`` — caller doesn't have to check.
    """
    out: dict[str, tuple[str | None, float | None]] = {}
    if not agent_names or not snapshots_db_path:
        return out
    # Deduplicate while preserving names — the SQL parameter list grows
    # quadratically if the caller passes the same name twice.
    unique_names = list(dict.fromkeys(agent_names))
    placeholders = ",".join("?" for _ in unique_names)
    try:
        with sqlite3.connect(snapshots_db_path, timeout=0.5) as conn:
            # ``ROWID`` (an alias for the auto-increment id) breaks ties
            # in ``created_at`` and is always strictly increasing per
            # insert, so taking MAX(id) per agent gives us the freshest
            # snapshot deterministically without needing a window
            # function (which isn't available in older SQLite builds).
            rows = conn.execute(
                f"SELECT s.agent_name, s.trigger, s.created_at "
                f"FROM agent_context_snapshots s "
                f"JOIN (SELECT agent_name, MAX(id) AS max_id "
                f"      FROM agent_context_snapshots "
                f"      WHERE agent_name IN ({placeholders}) "
                f"      GROUP BY agent_name) latest "
                f"ON s.agent_name = latest.agent_name "
                f"AND s.id = latest.max_id",
                unique_names,
            ).fetchall()
            for agent_name, trigger, created_at in rows:
                try:
                    ts = float(created_at) if created_at is not None else None
                except (TypeError, ValueError):
                    ts = None
                out[agent_name] = (trigger, ts)
    except sqlite3.Error:
        # Probe failure is non-fatal — caller falls back to raw status.
        # Single batch failure is preferable to N per-agent failures
        # logged separately.
        pass
    return out


def _compute_effective_status(
    record: dict, *,
    snapshots_db_path: str | None = None,
    snapshot_cache: dict[str, tuple[str | None, float | None]] | None = None,
) -> str:
    """Derive effective status from multi-signal evidence.

    Background (the 2026-05-11 ``running``-stuck-forever UI bug):
    ``spawn_registry.status`` is updated only via the spawn-event bridge
    socket. If that socket dies (R1/R2 race, subprocess SIGKILL'd before
    emitting final event), the DB value can be frozen at ``running``
    indefinitely while the agent has actually exited or gone idle.

    Instead of trusting the single bridge-fed field, cross-check with:

    * **Channel sock probe** — file present + connect-test succeeds ⇒
      subprocess is alive (sitting in keep-alive ``listen()``).
    * **Latest agent snapshot** — the subprocess writes its
      ``trigger`` (``task_complete`` / ``idle`` / ``error``) and
      ``created_at`` DIRECTLY to SQLite, bypassing the bridge entirely.
      That is our most trustworthy "what is the agent actually doing"
      signal.
    * **``record.last_active_at``** — the bridge updates this on every
      thinking/tool_call/tool_result/response event. Compared against
      the snapshot's ``created_at`` it tells us whether the latest idle
      snapshot is fresh or stale-from-a-prior-turn.

    All values returned are members of the canonical ``SpawnStatus``
    set used by the bridge and startup cleanup
    (``mark_stale_running``) so consumers do not see novel values.
    Decision tree (only overrides the mutable states ``running`` /
    ``starting`` / ``resumed`` / ``idle`` / ``pending`` / ``unknown`` —
    terminal states like ``completed`` / ``error`` / ``paused`` are
    trusted as-is):

    Channel alive (subprocess in keep-alive):
      * snapshot trigger ∈ {task_complete, idle} AND
        snapshot.created_at >= record.last_active_at  → ``idle``
        (subprocess wrote the idle snapshot AFTER its last bridge-
        relayed activity event ⇒ genuinely idle now, not mid-turn).
      * Otherwise → keep raw (likely mid-LLM call: prior idle snapshot
        is stale relative to current activity).

    Channel dead (subprocess exited):
      * trigger = ``error`` → ``error``.
      * trigger ∈ {task_complete, idle}:
          - lifecycle ``oneshot`` → ``completed`` (one-and-done done).
          - lifecycle ``resumable`` / missing → ``idle``. The agent is
            hibernating: ``auto_wake_if_idle`` respawns it from snapshot
            on the next inbound message. This matches the canonical
            rule already enforced by ``spawn_progress_bridge`` (live
            ``result`` event) and ``mark_stale_running`` (startup
            cleanup) — see those for the same ``"idle" if lifecycle ==
            "resumable" else "completed"`` pattern.
      * No snapshot trigger → keep raw. Could be the spawn race window
        (channel not yet bound, no snapshot yet) or a true crash with
        no terminal event. Without snapshot evidence we cannot
        distinguish; ``mark_stale_running`` will flip to idle/completed
        on the next backend restart.

    Probe exception → keep raw (don't invent a state from a failed
    probe).
    """
    raw = (record.get("status") or "unknown")
    lifecycle_raw = (record.get("lifecycle") or "").lower()
    # ``completed`` is only authoritative for oneshot agents — they really
    # are done forever. For resumable agents (post-2026-05-20 merge: every
    # non-oneshot agent), ``completed`` is a stale label written by the old
    # spawner; the canonical post-task state is ``idle`` (the agent is
    # waiting for a follow-up via resume_spawn / auto-resume on inbox).
    # Mirror the same rule used by ``spawn_progress_bridge`` (live result
    # event) and ``mark_stale_running`` (startup cleanup).
    if raw == "completed" and lifecycle_raw and lifecycle_raw != "oneshot":
        return "idle"
    # Terminal / overlay states are authoritative — never second-guess.
    if raw in {"completed", "error", "killed", "failed", "cancelled",
               "paused", "timeout"}:
        return raw
    # Anything outside the mutable set is unknown to the helper — defer
    # to whatever the writer chose.
    if raw not in {"running", "starting", "resumed", "idle", "pending",
                   "unknown"}:
        return raw

    agent_name = record.get("agent_name") or record.get("role") or ""
    if not agent_name:
        return raw

    # Probe channel liveness via connect (not just file-stat — orphan
    # sock files from SIGKILL'd processes would otherwise lie).
    channel_alive: bool | None
    try:
        from fast_agent.spawn.agent_channel import AgentChannel
        channel_alive = AgentChannel.is_alive(agent_name)
    except Exception:
        channel_alive = None

    # Latest snapshot — trigger + created_at, written directly by the
    # subprocess to SQLite (independent of bridge health).
    #
    # Prefer the pre-fetched batch dict (one query for the whole roster
    # via ``_fetch_latest_snapshots_batch``) over a per-agent SELECT.
    # Per-agent fallback is kept for direct callers that don't go
    # through ``list_agents`` (tests, debug endpoints).
    latest_trigger: str | None = None
    snapshot_ts: float | None = None
    if snapshot_cache is not None:
        cached = snapshot_cache.get(agent_name)
        if cached is not None:
            latest_trigger, snapshot_ts = cached
    elif snapshots_db_path:
        try:
            with sqlite3.connect(snapshots_db_path, timeout=0.5) as conn:
                row = conn.execute(
                    "SELECT trigger, created_at FROM agent_context_snapshots "
                    "WHERE agent_name = ? ORDER BY created_at DESC "
                    "LIMIT 1",
                    (agent_name,),
                ).fetchone()
                if row:
                    latest_trigger = row[0]
                    try:
                        snapshot_ts = float(row[1]) if row[1] is not None else None
                    except (TypeError, ValueError):
                        snapshot_ts = None
        except sqlite3.Error:
            pass

    lifecycle = (record.get("lifecycle") or "").lower()

    if channel_alive is True:
        if latest_trigger in {"task_complete", "idle"}:
            # Disambiguate "fresh idle" from "stale snapshot during
            # active turn". The bridge bumps ``last_active_at`` on every
            # LLM-side event; the subprocess writes the idle snapshot
            # AT THE END of a turn. If the snapshot is newer than the
            # last activity event we know the subprocess has settled.
            last_active = record.get("last_active_at") or 0.0
            try:
                last_active = float(last_active)
            except (TypeError, ValueError):
                last_active = 0.0
            if snapshot_ts is None or snapshot_ts >= last_active:
                return "idle"
        # No fresh-idle evidence → trust raw (likely mid-LLM-call).
        return raw

    if channel_alive is False:
        if latest_trigger == "error":
            return "error"
        if latest_trigger in {"task_complete", "idle"}:
            # Canonical rule (matches spawn_progress_bridge + mark_stale_running):
            # resumable agents go idle (respawnable); oneshot complete for good.
            return "completed" if lifecycle == "oneshot" else "idle"
        # Channel dead AND no snapshot evidence. Two indistinguishable
        # scenarios — fresh-spawn race (channel about to bind) vs.
        # silent crash. Trust raw; backend-restart cleanup will reconcile.
        return raw

    # Probe inconclusive (exception).
    return raw


def _icon_for_role(record: dict, *, default: str = "smart_toy") -> str:
    """Pick a Material Symbol icon based on whether this agent is the
    team's orchestrator.

    Why orchestrator-vs-worker and not role-name lookup? Templates are
    user-editable and may define arbitrary role names (PM, lead,
    scrum_master, BA, dev, designer, …). Hard-coding per-role icons
    (the prior implementation: ``if "PM" in role: icon = ...``) only
    worked for one specific template and silently fell back to the
    generic icon for any other naming. The orchestrator concept is the
    only system-wide invariant — template.orchestrator names whichever
    role coordinates the team.

    Resolution order:
      1. ``record["is_orchestrator"]`` if the registry row was tagged
         (forward-compatible — registry doesn't yet set this).
      2. Lookup ``team_sessions.template.orchestrator`` for this team
         and compare to the record's role.
      3. Fall through to ``default`` for workers.
    """
    if record.get("is_orchestrator"):
        return "assignment_ind"

    team_name = record.get("team_name") or ""
    role = (record.get("role") or "").strip()
    if not team_name or not role:
        return default

    try:
        # Cheap lookup — one DB read per agent card. The dashboard list
        # already issues N reads per refresh; this adds 1 SQL query per
        # team-managed row, so the cost stays in the same order.
        import json

        from core.agent_registry_db import _connect

        with _connect() as conn:
            row = conn.execute(
                """SELECT data_json FROM team_sessions
                   WHERE json_extract(data_json, '$.team_name') = ?
                   LIMIT 1""",
                (team_name,),
            ).fetchone()
        if row is None:
            return default
        data = json.loads(row["data_json"])
        orch_role = ((data.get("template") or {}).get("orchestrator") or "").strip()
        if orch_role and orch_role.lower() == role.lower():
            return "assignment_ind"
    except Exception:
        # Best-effort cosmetic — never block the response on an icon.
        pass
    return default


def _snapshots_db_path() -> str | None:
    """Resolve the path to the SQLite DB that holds ``agent_context_snapshots``.

    Uses the same env var the spawn registry uses so both sides agree.
    """
    p = os.environ.get("SPAWN_REGISTRY_DB", "data/jarvis.db")
    full = Path(p).resolve()
    return str(full) if full.exists() else None


_default_model_cache: str | None = None


def _get_default_model() -> str:
    """Read default_model from fastagent.config.yaml (cached)."""
    global _default_model_cache
    if _default_model_cache is not None:
        return _default_model_cache
    config_path = Path(__file__).parent.parent / "fastagent.config.yaml"
    if config_path.exists():
        try:
            with open(config_path) as f:
                cfg = yaml.safe_load(f)
            _default_model_cache = cfg.get("default_model", "openai.gpt-4o-mini")
        except Exception:
            _default_model_cache = "openai.gpt-4o-mini"
    else:
        _default_model_cache = "openai.gpt-4o-mini"
    return _default_model_cache
import services.shared_state as state
from services.activity_stream import activity_stream_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agents", tags=["agents"])

SKILLS_DIR = Path(__file__).parent.parent / ".fast-agent" / "skills"


def _get_agent_skills(agent_name: str) -> list[dict]:
    """Return per-skill load status for the agent details UI.

    Each entry carries ``status``:
      * ``"loaded"`` — manifest was resolved + injected into the live agent.
      * ``"failed"`` — requested by name in config but missing from the live
        agent's resolved manifests (file missing, parse error, etc).

    The live agent's ``skill_manifests`` is the authoritative source of what
    the agent actually sees; the raw ``config.skills`` field (preserved by
    ``_apply_skills_to_agent_configs``) is the request log used to compute
    the failed delta.
    """
    try:
        agent_data = fast.agents.get(agent_name)
        if not agent_data:
            return []
        config = agent_data.get("config")
        if not config:
            return []

        # Loaded manifests — prefer the live agent instance (post-init), fall
        # back to the resolved config list if the runtime isn't ready yet.
        loaded: list = []
        if state.agent_app is not None:
            instance = state.agent_app.get_agent(agent_name)
            if instance is not None:
                loaded = list(getattr(instance, "skill_manifests", None) or [])
        if not loaded:
            loaded = list(getattr(config, "skill_manifests", None) or [])

        result = [
            {
                "name": m.name,
                "description": m.description or "",
                "content": m.body or "",
                "status": "loaded",
            }
            for m in loaded
        ]
        loaded_names = {m.name for m in loaded}

        # Requested-but-not-loaded — string entries in raw config.skills
        # may be either bare skill names ("user-context") or directory paths
        # (".fast-agent/skills/user-context"). Fast-agent resolves both via
        # SkillRegistry to a SkillManifest whose ``name`` is the directory
        # basename, so we compare by basename to avoid false-positive
        # "failed" entries for path-style refs. Non-string entries
        # (Path/SkillRegistry/SkillManifest) reference directories rather
        # than a specific skill, so we can't diff them.
        requested = getattr(config, "skills", None)
        if isinstance(requested, (list, tuple)):
            for item in requested:
                if not isinstance(item, str) or not item:
                    continue
                basename = item.rstrip("/").split("/")[-1]
                if basename in loaded_names:
                    continue
                result.append({
                    "name": basename,
                    "description": "",
                    "content": "",
                    "status": "failed",
                })
        return result
    except Exception as e:
        logger.debug("[AGENTS API] Failed to get skills for %s: %s", agent_name, e)
        return []




def _get_runtime_instruction(agent_name: str) -> str:
    """Get the actual resolved instruction from the running agent instance.
    
    This returns the real prompt with skills injected (via set_instruction()),
    not the static template from the agent card.
    """
    try:
        if state.agent_app is None:
            return ""
        agent_instance = state.agent_app.get_agent(agent_name)
        if agent_instance is None:
            return ""
        return getattr(agent_instance, "instruction", "")
    except Exception as e:
        logger.debug("[AGENTS API] Failed to get runtime instruction for %s: %s", agent_name, e)
        return ""



def _safe_load_activity_data(raw_json: str | None):
    """Parse activity JSON safely so one malformed row does not break the response."""
    if not raw_json:
        return None
    try:
        return _json.loads(raw_json)
    except (_json.JSONDecodeError, TypeError):
        return None


def _get_runtime_tools(agent_name: str) -> dict[str, dict]:
    """Return per-MCP-server runtime attach status + tool list.

    Shape:
        {
          server_name: {
            "tools": [{"name", "description"}, ...],
            "status": "connected" | "failed",
            "error": "",  # filled in by _enrich_mcp_errors_async for failed servers
          }
        }

    ``connected`` servers come from ``aggregator._server_to_tool_map`` (only
    populated on successful attach). ``failed`` servers are computed as
    ``_configured_server_names - _attached_server_names`` so requested-but-
    unreachable MCPs surface in the UI with a red badge instead of being
    silently dropped.
    """
    try:
        if state.agent_app is None:
            return {}
        agent_instance = state.agent_app.get_agent(agent_name)
        if agent_instance is None:
            return {}
        aggregator = getattr(agent_instance, "_aggregator", None)
        if aggregator is None:
            return {}

        server_tool_map = getattr(aggregator, "_server_to_tool_map", {}) or {}
        configured = set(getattr(aggregator, "_configured_server_names", []) or [])
        attached = set(getattr(aggregator, "_attached_server_names", []) or [])

        result: dict[str, dict] = {}
        for server_name, namespaced_tools in server_tool_map.items():
            result[server_name] = {
                "tools": [
                    {
                        "name": nt.tool.name,
                        "description": getattr(nt.tool, "description", "") or "",
                    }
                    for nt in namespaced_tools
                ],
                "status": "connected",
                "error": "",
            }
        for server_name in configured - attached:
            if server_name in result:
                continue
            result[server_name] = {
                "tools": [],
                "status": "failed",
                "error": "",
            }
        return result
    except Exception as e:
        logger.debug("[AGENTS API] Failed to get runtime tools for %s: %s", agent_name, e)
        return {}


async def _enrich_mcp_errors_async(agent_name: str, tools: dict[str, dict]) -> None:
    """Fill ``error`` on failed entries of ``tools`` using live ServerStatus.

    Only called from the detail endpoint to avoid awaiting per-server probes
    on the (high-fanout) list endpoint.
    """
    failed_servers = [
        name for name, info in tools.items() if info.get("status") == "failed"
    ]
    if not failed_servers:
        return
    try:
        if state.agent_app is None:
            return
        agent_instance = state.agent_app.get_agent(agent_name)
        if agent_instance is None or not hasattr(agent_instance, "get_server_status"):
            return
        status_map = await agent_instance.get_server_status()
        for server_name in failed_servers:
            srv = status_map.get(server_name)
            if srv is None:
                continue
            err = getattr(srv, "error_message", None) or ""
            if err:
                tools[server_name]["error"] = str(err)
    except Exception as e:
        logger.debug("[AGENTS API] Failed to enrich MCP errors for %s: %s", agent_name, e)


# Icon mapping — the only hardcoded metadata (not available in runtime)
AGENT_ICONS = {
    "Jarvis": "smart_toy",
    "PersonalAgent": "person",
    "IoTAgent": "sensors",
    "MusicAgent": "music_note",
    "AudioReaderAgent": "menu_book",
    "FinanceAgent": "trending_up",
    "ResearchAgent": "search",
    "CrawlStoriesAgent": "auto_stories",
}


def _is_static_agent(name: str) -> bool:
    """True if the agent is defined in code (agent.py decorators), not
    loaded from a card source (file or DB). Code-defined agents cannot
    be edited or deleted via the API."""
    return name in fast.agents and name not in fast._agent_card_sources


def _is_dynamic_agent(name: str) -> bool:
    """True if the agent was loaded from an in-memory definition (DB).
    Detection: name is registered and its source path carries the
    `memory:` marker from fast_agent's in-memory loader."""
    from fast_agent.core.agent_card_loader import is_memory_card_path

    src = fast._agent_card_sources.get(name)
    return src is not None and is_memory_card_path(src)


def _build_agent_dict(name: str, agent_data: dict) -> dict:
    """Build a single agent dict from fast-agent runtime data."""
    config = agent_data.get("config")
    if not config:
        return {"name": name}
    
    # Determine parent: find which agent has this one as a child
    parent_agent = None
    for other_name, other_data in fast.agents.items():
        if other_name == name:
            continue
        children = other_data.get("child_agents") or []
        if name in children:
            parent_agent = other_name
            break
    
    child_agents = list(agent_data.get("child_agents") or [])
    
    # Use resolved instruction (with skills injected) as primary;
    # fall back to raw config instruction if runtime not available yet
    raw_instruction = getattr(config, "instruction", "")
    resolved = _get_runtime_instruction(name)
    
    return {
        "name": name,
        "description": getattr(config, "description", None) or "",
        "instruction": resolved or raw_instruction,
        "model": getattr(config, "model", None) or _get_default_model(),
        "servers": list(getattr(config, "servers", []) or []),
        # Wire labels (intentional vocab — see test_agents and dashboard):
        # - "card": template loaded from services.agent_definitions (DB-backed)
        # - "builtin": defined in code via @fast.agent in backend/agent.py
        # - "team" / "dynamic" are reserved for spawn_registry instances
        #   emitted by _build_spawn_agent_detail. The DB-backed templates
        #   here MUST NOT use "dynamic" — that label is the dashboard's
        #   marker for live spawn instances (see AgentDetail.vue:isSpawnedAgent).
        "type": "card" if name in fast._agent_card_sources else "builtin",
        "icon": AGENT_ICONS.get(name, "smart_toy"),
        "child_agents": child_agents,
        "parent_agent": parent_agent,
        "is_default": getattr(config, "default", False),
        "skills": _get_agent_skills(name),
        "tools": _get_runtime_tools(name),
        "status": "idle",
    }


def _build_agents_from_runtime() -> list[dict]:
    """Build complete agent list from fast-agent runtime registry (single source of truth)."""
    agents = []
    for name, agent_data in fast.agents.items():
        config = agent_data.get("config")
        if not config:
            continue
        agents.append(_build_agent_dict(name, agent_data))
    
    # Sort: default agent first, then alphabetical
    agents.sort(key=lambda a: (not a.get("is_default"), a["name"]))
    return agents


# Static agents defined in agent.py (hardcoded, cannot be modified via API)
STATIC_AGENTS = [
    {
        "name": "PersonalAgent",
        "description": "Email, calendar, personal reminders",
        "instruction": "Manage the user's email, calendar appointments, and personal reminders.",
        "model": "openai.gpt-4o-mini",
        "servers": ["personal-service", "calendar-service", "time-service"],
        "type": "static",
        "icon": "person",
    },
    {
        "name": "IoTAgent",
        "description": "Control IoT devices: lights, fans",
        "instruction": "Control IoT devices in the home: lights, fans, sensors.",
        "model": "openai.gpt-4o-mini",
        "servers": ["iot-service"],
        "type": "static",
        "icon": "sensors",
    },
    {
        "name": "MusicAgent",
        "description": "Play music, find songs",
        "instruction": "Play music and search for songs on YouTube on request.",
        "model": "openai.gpt-4o-mini",
        "servers": ["youtube-service"],
        "type": "static",
        "icon": "music_note",
    },
    {
        "name": "AudioReaderAgent",
        "description": "Read/play story audio",
        "instruction": "Read and play stories as text-to-speech audio.",
        "model": "openai.gpt-4o-mini",
        "servers": ["story-server"],
        "type": "static",
        "icon": "menu_book",
    },
]


class AgentCreate(BaseModel):
    name: str
    instruction: str
    model: str = ""  # Empty = use default_model from fastagent.config.yaml
    servers: list[str] = []
    use_history: bool = True


class AgentUpdate(BaseModel):
    instruction: str | None = None
    model: str | None = None
    servers: list[str] | None = None
    use_history: bool | None = None


def _normalize_create_payload(payload: dict) -> dict:
    """Translate the wire payload (AgentCreate / AgentUpdate) into the
    keyword shape the agent_definitions service expects.

    `model: ""` from the wire means "use default" — store NULL in DB so
    `list_definitions()` reflects the absence cleanly rather than carrying
    an empty-string sentinel through the load pipeline.
    """
    out = dict(payload)
    if "model" in out and not (out["model"] or "").strip():
        out["model"] = None
    return out


@router.get("", dependencies=[Depends(verify_api_key)])
async def list_agents(include_completed: bool = False):
    """List all agents (runtime + team) with skills.
    
    Agent metadata is read entirely from fast-agent runtime (single source of truth).
    By default, completed team agents are hidden.
    Pass include_completed=true to show history.
    """
    agents = _build_agents_from_runtime()
    
    # Add team/spawn agents from SQLite registry (single source of truth)
    try:
        import services.shared_state as state
        if hasattr(state, 'registry_db') and state.registry_db:
            registry = state.registry_db.get_all()
        else:
            registry = {}
            
        # ── Two-pass dedup: correctly classify team agents ──
        # Pass 1: Group ALL non-oneshot records by agent_name.
        # Include error/failed records for team_name resolution, but
        # mark them so we can deprioritize them for display.
        all_agent_records: dict[str, list[tuple[str, dict]]] = {}  # name → [(run_id, record)]
        for run_id, record in registry.items():
            lifecycle = record.get("lifecycle", "")
            if lifecycle == "oneshot":
                continue
            agent_name = record.get("agent_name", record.get("role", "agent"))
            all_agent_records.setdefault(agent_name, []).append((run_id, record))
        
        # Pass 2: Pick best record per agent, with correct team classification
        seen_names: dict[str, tuple[str, dict]] = {}  # name → (run_id, record)
        for agent_name, records in all_agent_records.items():
            # Determine if this agent is a team member (ANY record has team_name,
            # including error/failed ones — they still carry team metadata)
            any_team_name = ""
            for _, rec in records:
                tn = rec.get("team_name") or ""
                if tn:
                    any_team_name = tn
                    break
            
            # Filter out failed/error/killed for display selection
            displayable = [(rid, r) for rid, r in records
                           if r.get("status", "unknown") not in ("failed", "error", "killed")]
            
            # If ALL records are error/failed, still show the latest one
            if not displayable:
                displayable = sorted(records,
                    key=lambda x: x[1].get("started_at", 0), reverse=True)[:1]
            
            # Skip completed team agents unless explicitly requested
            is_team = bool(any_team_name)
            all_completed = all(r.get("status") == "completed" for _, r in displayable)
            if not include_completed and is_team and all_completed:
                continue
            
            # Pick best record: prefer running > completed, prefer team_name > none, prefer latest
            best_run_id, best_record = displayable[0]
            for run_id, rec in displayable:
                rec_team = rec.get("team_name") or ""
                best_team = best_record.get("team_name") or ""
                rec_status = rec.get("status", "unknown")
                best_status = best_record.get("status", "unknown")
                
                # Prefer running over completed
                if rec_status == "running" and best_status != "running":
                    best_run_id, best_record = run_id, rec
                    continue
                if rec_status != "running" and best_status == "running":
                    continue
                # Prefer record with team_name
                if rec_team and not best_team:
                    best_run_id, best_record = run_id, rec
                    continue
                if not rec_team and best_team:
                    continue
                # Both equal priority — prefer latest
                rec_ts = rec.get("created_at", rec.get("started_at", ""))
                best_ts = best_record.get("created_at", best_record.get("started_at", ""))
                if str(rec_ts) > str(best_ts):
                    best_run_id, best_record = run_id, rec
            
            # Ensure team_name is inherited if best record lacks it
            if any_team_name and not (best_record.get("team_name") or ""):
                best_record = dict(best_record)  # Don't mutate original
                best_record["team_name"] = any_team_name
            
            seen_names[agent_name] = (best_run_id, best_record)
        
        # Resolve snapshot DB path once for the whole loop — avoids
        # re-running Path.resolve() per agent.
        _snap_db = _snapshots_db_path()

        # Batch-fetch the latest snapshot for every agent in ONE query
        # instead of one query per agent. For an N-agent roster this
        # cuts SQLite round-trips from N to 1 — important once teams
        # grow past ~10 agents (incident scale: 7-agent retro audit
        # used to fan out 7 selects per ``/agents`` poll).
        _all_names = [
            r.get("agent_name") or r.get("role") or ""
            for _, r in seen_names.values()
        ]
        _snapshot_cache = _fetch_latest_snapshots_batch(_all_names, _snap_db)

        for run_id, record in seen_names.values():
            agent_name = record.get("agent_name", record.get("role", "agent"))
            # Multi-signal status (channel sock + snapshot trigger) instead
            # of trusting the bridge-fed DB field alone. See
            # ``_compute_effective_status`` for the decision tree.
            status = _compute_effective_status(
                record,
                snapshots_db_path=_snap_db,
                snapshot_cache=_snapshot_cache,
            )

            role = record.get("role", "")
            # Icon based on whether this agent is the team's
            # orchestrator (per template.orchestrator). Worker roles
            # share a generic icon — adding role-specific icons here
            # would re-introduce the hard-code we removed in the
            # 2026-05-19 audit (templates can define arbitrary role
            # names; only "orchestrator" is a system-wide concept).
            icon = _icon_for_role(record, default="smart_toy")
            
            model = record.get("original_config", {}).get("model", "")
            if not model:
                model = _get_default_model()
            team_name = record.get("team_name") or ""  # Coerce None → ""
            agent_type = "team" if team_name else "dynamic"

            # Prefer runtime_config (live agent data) over original_config (template)
            rt_cfg = record.get("runtime_config") or {}
            orig_cfg = record.get("original_config") or {}

            # Servers: from runtime_config tools keys, or original_config servers
            servers = list(rt_cfg.get("tools", {}).keys()) if rt_cfg.get("tools") else orig_cfg.get("servers", [])

            # Skills: from runtime_config skills, or original_config skills
            spawn_skills = []
            if rt_cfg.get("skills"):
                spawn_skills = rt_cfg["skills"]  # Already [{name, description}]
            elif orig_cfg.get("skills"):
                skill_csv = orig_cfg["skills"]
                if isinstance(skill_csv, str):
                    spawn_skills = [{"name": s.strip(), "description": ""} for s in skill_csv.split(",") if s.strip()]
                elif isinstance(skill_csv, list):
                    spawn_skills = [{"name": s, "description": ""} for s in skill_csv if s]



            agents.append({
                "name": agent_name,
                "instruction": record.get("task", ""),
                "model": model,
                "servers": servers,
                "type": agent_type,
                "use_history": False,
                "icon": icon,
                "status": status,
                "role": role,
                "run_id": run_id,
                "lifecycle": record.get("lifecycle", ""),
                "team_name": team_name,
                "skills": spawn_skills,
            })
    except Exception as e:
        logger.warning("[AGENTS API] Failed to read spawn registry: %s", e)
    
    # ── Post-process: override status with PauseManager state ──
    try:
        from services.pause_manager import pause_manager
        for agent in agents:
            name = agent.get("name", "")
            if pause_manager.is_paused(name):
                agent["status"] = "paused"
    except Exception:
        pass
    
    return agents


@router.get("/activity-stream", dependencies=[Depends(verify_api_key)])
async def activity_stream(agent_name: str | None = None):
    """SSE endpoint for realtime agent activity events.
    
    Optional query param `agent_name` filters to a specific agent.
    Returns Server-Sent Events with JSON data payloads.
    """
    import asyncio
    from services.activity_stream import activity_stream_manager
    
    sub_id, queue = activity_stream_manager.subscribe(agent_filter=agent_name)
    
    async def event_generator():
        try:
            # Send initial ping
            yield f"data: {_json.dumps({'type': 'connected', 'agent_filter': agent_name})}\n\n"
            
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {_json.dumps(event, ensure_ascii=False, default=str)}\n\n"
                except asyncio.TimeoutError:
                    # Keep-alive ping
                    yield f"data: {_json.dumps({'type': 'ping'})}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            activity_stream_manager.unsubscribe(sub_id)
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{name}", dependencies=[Depends(verify_api_key)])
async def get_agent(name: str):
    """Get a specific agent's details with skills (from runtime or spawn registry)."""
    # 1. Check fast-agent runtime (static/dynamic agents)
    agent_data = fast.agents.get(name)
    if agent_data:
        detail = _build_agent_dict(name, agent_data)
        # Probe live ServerStatus only for the detail endpoint so failed MCP
        # servers expose their error_message in the UI tooltip. The list
        # endpoint deliberately skips this to keep the fanout cheap.
        await _enrich_mcp_errors_async(name, detail.get("tools") or {})
        return detail

    # 2. Fall back to spawn registry (team/spawned agents)
    spawn_detail = _build_spawn_agent_detail(name)
    if spawn_detail:
        return spawn_detail

    raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")


@router.get("/{name}/mcp-status", dependencies=[Depends(verify_api_key)])
async def get_agent_mcp_status(name: str):
    """Get MCP server connection health for a spawned agent.

    Returns per-server status (connected/failed) and tool counts.
    Useful for diagnosing silent MCP failures in spawned agents.
    """
    try:
        import services.shared_state as _state
        if hasattr(_state, 'registry_db') and _state.registry_db:
            records = _state.registry_db.find_by_name(name)
            if records:
                # Pick latest record
                records.sort(
                    key=lambda r: r.get("started_at", 0),
                    reverse=True,
                )
                mcp_status = records[0].get("mcp_status")
                if mcp_status:
                    return {
                        "agent": name,
                        **mcp_status,
                    }
                return {
                    "agent": name,
                    "error": "MCP status not yet reported (agent may still be initializing)",
                }
    except Exception as e:
        logger.warning("[AGENTS API] Failed to get MCP status for %s: %s", name, e)

    raise HTTPException(status_code=404, detail=f"Agent '{name}' not found or no MCP status available")


def _build_spawn_agent_detail(name: str) -> dict | None:
    """Build agent detail dict from SQLite spawn registry for spawned/team agents.
    
    SQLite is the single source of truth (populated by SpawnRegistry on_change
    callback and SpawnProgressBridge event processing).
    Returns None if agent not found.
    """
    import services.shared_state as _state
    
    record = None
    try:
        if hasattr(_state, 'registry_db') and _state.registry_db:
            records = _state.registry_db.find_by_name(name)
            if records:
                # Pick best: prefer running, then latest
                records.sort(
                    key=lambda r: (
                        r.get("status") == "running",
                        r.get("started_at", 0),
                    ),
                    reverse=True,
                )
                record = records[0]
    except Exception as e:
        logger.debug("[AGENTS API] Failed reading SQLite registry for %s: %s", name, e)
    
    if not record:
        return None
    
    # --- Build detail dict ---
    orig_cfg = record.get("original_config", {})
    role = record.get("role", "")
    team_name = record.get("team_name") or ""
    
    # Icon by orchestrator-or-not (see _icon_for_role).
    icon = _icon_for_role(record, default="smart_toy")
    
    # Extract model
    model = orig_cfg.get("model", "")
    if not model:
        model = _get_default_model()
    
    # Extract servers
    servers = orig_cfg.get("servers", [])
    
    # --- Prefer runtime_config (from live agent) over original_config (template) ---
    rt_cfg = record.get("runtime_config") or {}
    mcp_status = record.get("mcp_status") or {}

    # Requested skill names — preserved from the spawn input so we can compute
    # the "requested but failed to load" diff without re-reading template YAML.
    requested_skill_names = _parse_skill_names(orig_cfg.get("skills"))

    runtime_pending = not rt_cfg

    if rt_cfg:
        instruction = rt_cfg.get("resolved_instruction", "")
        loaded_skills_raw = rt_cfg.get("skills") or []
        rt_tools = rt_cfg.get("tools") or {}
        spawn_skills = _merge_skill_status(loaded_skills_raw, requested_skill_names)
        tools = _merge_spawn_tool_status(rt_tools, mcp_status.get("servers") or {})
        # Populate servers from runtime tools keys if not already set
        if not servers and tools:
            servers = list(tools.keys())
    else:
        # Runtime introspection not yet delivered — surface a pending state and
        # let the dashboard refetch on the `runtime_config_ready` push instead
        # of lying with template data the live agent might not actually see.
        instruction = orig_cfg.get("instruction", "") or record.get("task", "")
        context = orig_cfg.get("context", "")
        if context:
            instruction = f"{instruction}\n\n--- Context ---\n{context}"
        spawn_skills = []
        tools = {}

    return {
        "name": name,
        "description": f"Team agent ({role})" if role else "Spawned agent",
        "instruction": instruction,
        "model": model,
        "servers": servers,
        "type": "team" if team_name else "dynamic",
        "icon": icon,
        "child_agents": [],
        "parent_agent": None,
        "is_default": False,
        "skills": spawn_skills,
        "tools": tools,
        "runtime_pending": runtime_pending,
        "status": record.get("status", "idle"),
        "role": role,
        "run_id": record.get("run_id", ""),
        "lifecycle": record.get("lifecycle", ""),
        "team_name": team_name,
        "mcp_status": record.get("mcp_status"),
    }


def _parse_skill_names(raw: Any) -> list[str]:
    """Return clean skill name list from the spawn record's ``original_config``."""
    if not raw:
        return []
    if isinstance(raw, str):
        return [s.strip() for s in raw.split(",") if s.strip()]
    if isinstance(raw, list):
        names: list[str] = []
        for item in raw:
            if isinstance(item, str) and item:
                names.append(item)
            elif isinstance(item, dict) and item.get("name"):
                names.append(item["name"])
        return names
    return []


def _merge_skill_status(loaded_raw: list, requested_names: list[str]) -> list[dict]:
    """Tag each skill with ``loaded`` / ``failed`` so the UI can render badges.

    ``loaded_raw`` is the runtime_config payload from the child process —
    each entry already has ``name`` / ``description`` / ``content``.
    ``requested_names`` came from the spawn input; anything in that list
    missing from ``loaded_raw`` is treated as a load failure.
    """
    result: list[dict] = []
    loaded_names: set[str] = set()
    for entry in loaded_raw:
        if not isinstance(entry, dict):
            continue
        entry_name = entry.get("name") or ""
        if not entry_name:
            continue
        loaded_names.add(entry_name)
        result.append({
            "name": entry_name,
            "description": entry.get("description") or "",
            "content": entry.get("content") or "",
            "status": "loaded",
        })
    for name in requested_names:
        if name in loaded_names:
            continue
        result.append({
            "name": name,
            "description": "",
            "content": "",
            "status": "failed",
        })
    return result


def _merge_spawn_tool_status(
    rt_tools: dict, mcp_servers: dict
) -> dict[str, dict]:
    """Combine runtime_config.tools (per-server tool lists, attach-only) with
    mcp_status.servers (per-server is_connected + error_message) into the
    unified ``{server: {tools, status, error}}`` shape that the built-in agent
    path also returns.

    Note: the ``mcp_servers`` payload emitted by isolated_runner._emit_mcp_status
    carries BOTH ``status`` ("connected"/"failed") and ``is_connected`` (bool).
    We read ``is_connected``; the spawn_progress_bridge log path reads ``status``.
    Both fields are load-bearing — dropping either silently regresses one reader.
    """
    result: dict[str, dict] = {}
    for server_name, tools_list in (rt_tools or {}).items():
        result[server_name] = {
            "tools": list(tools_list) if isinstance(tools_list, list) else [],
            "status": "connected",
            "error": "",
        }
    for server_name, info in (mcp_servers or {}).items():
        if not isinstance(info, dict):
            continue
        is_connected = info.get("is_connected")
        status = "connected" if is_connected else "failed"
        error = info.get("error") or ""
        existing = result.get(server_name)
        if existing is None:
            result[server_name] = {
                "tools": [],
                "status": status,
                "error": str(error or ""),
            }
        else:
            existing["status"] = status
            if error:
                existing["error"] = str(error)
    return result




@router.post("", dependencies=[Depends(verify_api_key)])
async def create_agent(agent: AgentCreate):
    """Create a new dynamic agent definition.

    Writes a row into the ``agent_definitions`` table and bumps the
    rev counter. The parent process's db_rev_poll_loop picks up the
    change within POLL_INTERVAL seconds and registers the agent with
    Jarvis as a tool. No filesystem involved.
    """
    if not re.match(r'^[A-Za-z][A-Za-z0-9_]*$', agent.name):
        raise HTTPException(status_code=400, detail="Name must be alphanumeric (start with letter)")

    if _is_static_agent(agent.name):
        raise HTTPException(status_code=409, detail=f"'{agent.name}' is a static agent (cannot be overridden)")

    from services import agent_definitions as defs_svc

    try:
        defs_svc.create_definition(
            **_normalize_create_payload(agent.model_dump())
        )
    except ValueError as e:
        # Duplicate name or validation error from the service layer.
        msg = str(e)
        status = 409 if "already exists" in msg else 400
        raise HTTPException(status_code=status, detail=msg)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    activity_stream_manager.broadcast({
        "agent_name": agent.name,
        "event_type": "agent_added",
        "message": f"Agent '{agent.name}' created",
        "timestamp": _time.time(),
    })

    logger.info("[AGENTS API] Created agent: %s", agent.name)
    return {"status": "created", "name": agent.name}


@router.put("/{name}", dependencies=[Depends(verify_api_key)])
async def update_agent(name: str, update: AgentUpdate):
    """Update an existing dynamic agent definition in the DB."""
    if _is_static_agent(name):
        raise HTTPException(status_code=403, detail=f"'{name}' is a static agent (cannot be edited)")

    from services import agent_definitions as defs_svc

    update_data = _normalize_create_payload(update.model_dump(exclude_none=True))
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update")

    try:
        defs_svc.update_definition(name, **update_data)
    except ValueError as e:
        msg = str(e)
        status = 404 if "not found" in msg else 400
        raise HTTPException(status_code=status, detail=msg)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    logger.info("[AGENTS API] Updated agent: %s", name)
    return {"status": "updated", "name": name}


@router.delete("/{name}", dependencies=[Depends(verify_api_key)])
async def delete_agent(name: str):
    """Delete a dynamic agent definition or remove a team/spawn agent
    from the runtime registry.

    Order of resolution:
    1. If the name is statically defined in code → 403 (cannot delete).
    2. If the name exists in `agent_definitions` → remove row + bump rev.
    3. Otherwise fall through to spawn_registry (active team / spawn
       instances), which is unchanged by this refactor.
    """
    if _is_static_agent(name):
        raise HTTPException(status_code=403, detail=f"'{name}' is a static agent (cannot be deleted)")

    from services import agent_definitions as defs_svc

    if defs_svc.delete_definition(name):
        activity_stream_manager.broadcast({
            "agent_name": name,
            "event_type": "agent_removed",
            "message": f"Agent '{name}' removed",
            "timestamp": _time.time(),
        })
        logger.info("[AGENTS API] Deleted dynamic agent definition: %s", name)
        return {"status": "deleted", "name": name}
    
    # Try spawn registry (team/spawned agents) — SQLite only
    deleted_count = 0
    try:
        import services.shared_state as state
        if hasattr(state, 'registry_db') and state.registry_db:
            deleted_count = state.registry_db.delete_by_name(name)
    except Exception as e:
        logger.warning("[AGENTS API] Failed to remove from registry: %s", e)
    
    if deleted_count > 0:
        # Also delete related activities
        try:
            from core.database import AgentActivity, get_db_session
            db = get_db_session()
            try:
                db.query(AgentActivity).filter(AgentActivity.agent_name == name).delete()
                db.commit()
            except Exception:
                db.rollback()
            finally:
                db.close()
        except Exception:
            pass
        # Broadcast removal to SSE subscribers
        activity_stream_manager.broadcast({
            "agent_name": name,
            "event_type": "agent_removed",
            "message": f"Agent '{name}' removed",
            "timestamp": _time.time(),
        })
        logger.info("[AGENTS API] Removed %d registry entries for: %s", deleted_count, name)
        return {"status": "deleted", "name": name}
    
    raise HTTPException(status_code=404, detail=f"Agent '{name}' not found")


@router.post("/{agent_name}/pause", dependencies=[Depends(verify_api_key)])
async def pause_agent(agent_name: str):
    """Pause an agent — blocks at next LLM/tool checkpoint.
    
    For in-process agents: clears asyncio.Event.
    For subprocess agents: sends SIGUSR1 to the process.
    """
    from services.pause_manager import pause_manager

    changed = pause_manager.pause(agent_name)
    if not changed:
        return {"status": "already_paused", "agent": agent_name}
    return {"status": "paused", "agent": agent_name}


@router.post("/{agent_name}/resume", dependencies=[Depends(verify_api_key)])
async def resume_agent(agent_name: str):
    """Resume a paused agent.
    
    For in-process agents: sets asyncio.Event.
    For subprocess agents: sends SIGUSR2 to the process.
    """
    from services.pause_manager import pause_manager

    changed = pause_manager.resume(agent_name)
    if not changed:
        return {"status": "not_paused", "agent": agent_name}
    return {"status": "resumed", "agent": agent_name}


@router.delete("/teams/{team_name}", dependencies=[Depends(verify_api_key)])
async def delete_team(team_name: str):
    """Delete an entire team and all associated data.
    
    Single authority for team deletion — cleans BOTH SQLite registry_db
    AND JSON spawn_registry, plus workspace, messages, sessions, activities, cards.
    """
    import shutil
    
    project_root = Path(__file__).parent.parent
    cleanup_log = []
    team_agent_names = []
    session_ids: set[str] = set()
    
    # ── 1. Delete from SQLite registry (primary source) ──
    try:
        import services.shared_state as state
        if hasattr(state, 'registry_db') and state.registry_db:
            # First gather info before delete
            all_records = state.registry_db.get_all()
            for run_id, rec in all_records.items():
                if rec.get("team_name") == team_name:
                    team_agent_names.append(rec.get("agent_name", ""))
                    sid = rec.get("session_id", "")
                    if sid:
                        session_ids.add(sid)
                    ws = (rec.get("original_config") or {}).get("workspace_dir", "")
                    if ws and "_" in Path(ws).name:
                        parts = Path(ws).name.rsplit("_", 1)
                        if len(parts) == 2:
                            session_ids.add(parts[1])
            
            deleted_count, _ = state.registry_db.delete_by_team(team_name)
            if deleted_count:
                cleanup_log.append(f"{deleted_count} SQLite records")
            
            # Also clean stale records with missing team_name for known members
            if team_agent_names:
                stale_count = 0
                for agent_name in team_agent_names:
                    if agent_name:
                        stale_count += state.registry_db.delete_by_name(agent_name)
                if stale_count:
                    cleanup_log.append(f"{stale_count} stale records")
    except Exception as e:
        logger.warning("[AGENTS API] SQLite registry cleanup error: %s", e)
    
    # (JSON registry is no longer read/written by backend — skip)
    
    if not team_agent_names and not cleanup_log:
        raise HTTPException(status_code=404, detail=f"Team '{team_name}' not found")
    
    workspaces_root = project_root / ".runtime" / "data" / "workspaces"

    # ── 3. Find session_ids that belong to this team (SoT = TeamSessionStore) ──
    # Workspace dirs use template name (e.g. "agile-team_{sid}") not team_name,
    # so we must look up sessions by team_name from the canonical store. The
    # ``team_sessions`` data lives in SQLite (fast-agent's TeamSessionStore);
    # there is no filesystem JSON copy any more — earlier versions of this
    # route scanned a non-existent ``workspaces/team_sessions/*.json`` dir,
    # which always matched zero files and silently leaked rows.
    try:
        from fast_agent.spawn.team_spawner import list_team_sessions as _list_team_sessions
        for sess in _list_team_sessions():
            if sess.get("team_name") == team_name and sess.get("session_id"):
                session_ids.add(sess["session_id"])
    except Exception as e:
        logger.warning("[AGENTS API] team_sessions lookup error: %s", e)

    # ── 4. Remove workspace dirs (match by session_id suffix, not team_name) ──
    # Dirs are named "{template}_{session_id}", e.g. "agile-team_6d85b825"
    if workspaces_root.is_dir():
        for d in list(workspaces_root.iterdir()):
            if not d.is_dir():
                continue
            for sid in session_ids:
                if d.name.endswith(f"_{sid}"):
                    try:
                        shutil.rmtree(d)
                        cleanup_log.append(f"workspace {d.name}")
                    except Exception as e:
                        logger.warning("Failed to remove workspace %s: %s", d, e)
                    break
    
    # ── 5. Remove message dirs ──
    messages_root = project_root / ".runtime" / "state" / "messages"
    if messages_root.is_dir():
        for sid in session_ids:
            msg_dir = messages_root / sid
            if msg_dir.is_dir():
                try:
                    shutil.rmtree(msg_dir)
                    cleanup_log.append(f"messages/{sid}")
                except Exception as e:
                    logger.warning("Failed to remove messages %s: %s", msg_dir, e)
    
    # ── 6. Drop team sessions from the canonical SoT ──
    # Single owner of ``team_sessions`` (SQLite + in-memory cache) is
    # ``fast_agent.spawn.team_spawner``. Going through its public delete
    # function keeps the cache consistent with the row write — direct
    # SQL from this route would leave the cached TeamSession objects
    # stale, which used to surface as "Jarvis spawned a team that
    # immediately self-resumed an orphan session" (incident 2026-05-10).
    try:
        from fast_agent.spawn.team_spawner import (
            delete_team_session as _ts_delete,
            delete_team_sessions_by_team_name as _ts_delete_by_name,
        )
        ts_deleted = sum(1 for sid in session_ids if _ts_delete(sid))
        # Belt-and-braces: catch sessions that weren't in the resolved
        # ``session_ids`` set but carry our team_name in their stored data.
        ts_deleted += _ts_delete_by_name(team_name)
        if ts_deleted:
            cleanup_log.append(f"{ts_deleted} team_session(s)")
    except Exception as e:
        logger.warning("[AGENTS API] team_sessions cleanup error: %s", e)
    
    # ── 7. Clean up DB activities ──
    try:
        from core.database import AgentActivity, get_db_session
        db = get_db_session()
        try:
            for agent_name in team_agent_names:
                if agent_name:
                    db.query(AgentActivity).filter(AgentActivity.agent_name == agent_name).delete()
            db.commit()
            cleanup_log.append("activities")
        except Exception:
            db.rollback()
        finally:
            db.close()
    except Exception:
        pass

    # ── 7b. Clean up team_completion notifications ──
    # Without this, a stale notification keyed on the team_name lingers
    # in /notifications even after the user deletes the team — and
    # blocks the dedupe of the next team that reuses the same name.
    # (See 2026-05-14 toolset-self-audit incident.) We match both
    # ``team_name`` (covers pre-2026-05-14 rows that lack session_id)
    # and the resolved ``session_ids`` (covers post-fix rows that key
    # dedupe by session).
    try:
        from core.database import NotificationModel, get_db_session
        db = get_db_session()
        try:
            # ⚠️ JSON SPACING IS LOAD-BEARING (matches writer in
            # ``spawn_progress_bridge._create_team_notification``).
            # ``json.dumps()`` default emits ``"key": "value"`` with a
            # space after the colon. These ``LIKE %...%`` substring
            # filters depend on that exact spacing. If the writer ever
            # switches to ``separators=(',', ':')`` this filter silently
            # matches nothing → cascade cleanup breaks → next re-spawn
            # of same team_name hits stale notification → dedupe blocks
            # the new completion notification.
            # Proper migration path: SQLite's ``json_extract()`` on the
            # metadata_json column — see GH issue for that work.
            q = db.query(NotificationModel).filter(
                NotificationModel.metadata_json.contains('"source": "team_completion"'),
            )
            patterns = [f'"team_name": "{team_name}"']
            patterns.extend(f'"session_id": "{sid}"' for sid in session_ids)
            from sqlalchemy import or_
            q = q.filter(
                or_(*(NotificationModel.metadata_json.contains(p) for p in patterns))
            )
            n_dropped = q.delete(synchronize_session=False)
            db.commit()
            if n_dropped:
                cleanup_log.append(f"{n_dropped} notification(s)")
        except Exception as e:
            db.rollback()
            logger.warning("[AGENTS API] notification cleanup error: %s", e)
        finally:
            db.close()
    except Exception:
        pass

    # (Context snapshots cleaned by lifecycle hook in spawn_progress_bridge._handle_removal)
    
    # ── 8. Remove dynamic agent definitions from DB ──
    # Team members may have been registered as DB-backed dynamic agents
    # (e.g. spawn_team_tool can persist members via `spawn_agent`). Each
    # delete bumps the rev counter so the reload loop drops them from
    # fast.agents without restart.
    from services import agent_definitions as defs_svc

    defs_removed = 0
    for agent_name in team_agent_names:
        if not agent_name:
            continue
        try:
            if defs_svc.delete_definition(agent_name):
                defs_removed += 1
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[AGENTS API] failed deleting definition '%s': %s",
                agent_name, e,
            )
    if defs_removed:
        cleanup_log.append(f"{defs_removed} agent definitions")
    
    logger.info("[AGENTS API] Team '%s' deleted: %s", team_name, cleanup_log)
    
    # ── 9. Broadcast agent_removed SSE events for live dashboard updates ──
    for agent_name in team_agent_names:
        if agent_name:
            activity_stream_manager.broadcast({
                "agent_name": agent_name,
                "event_type": "agent_removed",
                "message": f"Agent '{agent_name}' removed (team '{team_name}' disbanded)",
                "timestamp": _time.time(),
            })
    
    return {
        "status": "deleted",
        "team_name": team_name,
        "removed_agents": team_agent_names,
        "cleaned": cleanup_log,
    }



resources_router = APIRouter(prefix="/api/resources", tags=["resources"])


@resources_router.get("/skills", dependencies=[Depends(verify_api_key)])
async def list_all_skills():
    """List all available skills from the skills directory."""
    from fast_agent.skills.registry import SkillRegistry

    if not SKILLS_DIR.exists():
        return []

    registry = SkillRegistry(base_dir=SKILLS_DIR.parent, directories=[SKILLS_DIR])
    manifests = registry.load_manifests()
    return [
        {"name": m.name, "description": m.description}
        for m in manifests
    ]


# ─── Agent Activity Endpoints ─────────────────────────────────────────────────────


@router.get("/{name}/messages", dependencies=[Depends(verify_api_key)])
async def get_agent_messages(name: str, since: int = 0, limit: int = 200):
    """Return PromptMessageExtended turns from agent.message_history.

    Source of truth for the Team Monitor v2 UI. Each turn is one item:
    ``{turn_idx, role, message: <PromptMessageExtended dump>}``.
    Large text blocks are truncated — use ``/turns/{turn_idx}/full`` for
    untruncated content when the user expands a turn.

    Query params:
      ``since`` — first turn_idx to include (used for delta fetch on
                  reconnect; clients pass the highest turn_idx they have).
      ``limit`` — cap on returned turns (latest ``limit`` if history is
                  longer). Default 200; clamped to a sane range.
    """
    from services.agent_message_stream import list_agent_messages

    if limit <= 0 or limit > 500:
        limit = 200
    return list_agent_messages(name, since=max(0, since), limit=limit)


@router.get("/{name}/turns/{turn_idx}/full", dependencies=[Depends(verify_api_key)])
async def get_agent_turn_full(name: str, turn_idx: int):
    """Return the untruncated PromptMessageExtended for one turn.

    Called when a user clicks "Show full" on a truncated content block.
    """
    from services.agent_message_stream import get_agent_turn_full as _get_full

    result = _get_full(name, turn_idx)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"Turn {turn_idx} not found for agent '{name}'",
        )
    return result


@router.get("/activities/recent", dependencies=[Depends(verify_api_key)])
async def get_recent_activities(per_agent: int = 20):
    """Get recent activity events for ALL agents in one call (avoids N+1).
    
    Returns a dict keyed by agent_name, each containing up to `per_agent` recent events.
    """
    from core.database import AgentActivity, get_db_session

    db = get_db_session()
    try:
        # Get distinct agent names that have activities
        agent_names = [
            row[0] for row in
            db.query(AgentActivity.agent_name).distinct().all()
        ]

        result = {}
        for name in agent_names:
            activities = (
                db.query(AgentActivity)
                .filter(AgentActivity.agent_name == name)
                .order_by(AgentActivity.created_at.desc())
                .limit(per_agent)
                .all()
            )
            result[name] = [
                {
                    "id": a.id,
                    "event_type": a.event_type,
                    "message": a.message,
                    "run_id": a.run_id,
                    "data": _json.loads(a.data_json) if a.data_json else None,
                    "created_at": a.created_at,
                }
                for a in activities
            ]

        return result
    finally:
        db.close()


@router.get("/{name}/activities", dependencies=[Depends(verify_api_key)])
async def get_agent_activities(name: str, limit: int = 50, offset: int = 0):
    """Get recent activity events for an agent from SQLite."""
    from core.database import AgentActivity, get_db_session
    
    db = get_db_session()
    try:
        query = db.query(AgentActivity).filter(
            AgentActivity.agent_name == name
        ).order_by(AgentActivity.created_at.desc())
        
        total = query.count()
        activities = query.offset(offset).limit(limit).all()
        
        return {
            "agent_name": name,
            "total": total,
            "offset": offset,
            "limit": limit,
            "activities": [
                {
                    "id": a.id,
                    "event_type": a.event_type,
                    "message": a.message,
                    "run_id": a.run_id,
                    "data": _safe_load_activity_data(a.data_json),
                    "created_at": a.created_at,
                }
                for a in activities
            ],
        }
    finally:
        db.close()


# ---- Context Window Snapshots ---------------------------------------------------


@router.get("/{name}/context", dependencies=[Depends(verify_api_key)])
async def get_agent_context(name: str, run_id: str | None = None, limit: int = 5):
    """Get recent context window snapshot metadata for an agent.

    Does NOT return full context_json (too large). Use /{name}/context/{id}/messages.
    """
    from services.context_persistence import get_context_snapshot_meta
    snapshots = get_context_snapshot_meta(name, run_id=run_id, limit=limit)
    return {"snapshots": snapshots}


@router.get("/{name}/context/{snapshot_id}/messages", dependencies=[Depends(verify_api_key)])
async def get_context_messages_endpoint(name: str, snapshot_id: int):
    """Get parsed context window messages from a specific snapshot.

    Returns list of messages with role, content, tool call info — for UI rendering.
    """
    from services.context_persistence import get_context_messages
    messages = get_context_messages(snapshot_id)
    if messages is None:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return {"messages": messages, "total": len(messages)}
