"""Jarvis V3 Backend - FastAPI Application Entry Point.

This is the main application module. Routes are organized in routes/ package.
Business logic lives in services/ and helpers/.
Core infrastructure (auth, database) lives in core/.
"""
import os
import sys
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

# Configure logging (centralized)
from core.logging_config import setup_logging
setup_logging()
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Set SPAWN_REGISTRY_DB so all processes (including spawned subprocesses)
# use SQLite as the single source of truth for the agent registry.
os.environ.setdefault("SPAWN_REGISTRY_DB", str(Path("data/jarvis.db").resolve()))

# Set SPAWN_PROJECT_DIR so child agents can find spawn_registry, team_sessions,
# and other project-level resources.  Needed by team notifications and email tools.
os.environ.setdefault("SPAWN_PROJECT_DIR", str(Path.cwd().resolve()))

# Patch sys.argv to avoid conflicts with FastAgent's internal argument parsing
if "uvicorn" in sys.argv[0]:
    sys.argv = [sys.argv[0]]

from core.database import init_db

# CRITICAL ORDERING: seed os.environ from DB BEFORE importing `agent` (which
# constructs FastAgent, which eagerly loads fastagent.config.yaml and resolves
# ``${VAR}`` placeholders against whatever is already in os.environ). If we
# defer this to the lifespan, YAML is already parsed with empty values and
# every MCP subprocess spawned later inherits empty creds.
def _bootstrap_env_from_db() -> None:
    init_db()
    from services.config_service import config_service
    from services.runtime_config import apply_master_key, reconcile_service_env

    if not os.environ.get("JARVIS_API_KEY"):
        stored_master = config_service.get("auth", "JARVIS_API_KEY")
        if stored_master:
            apply_master_key(stored_master)
            logger.info("[BOOTSTRAP] Restored JARVIS_API_KEY from DB (pre-agent)")

    seeded = reconcile_service_env(config_service)
    if seeded:
        logger.info("[BOOTSTRAP] Seeded %d service env entr%s (pre-agent)",
                    seeded, "y" if seeded == 1 else "ies")

    from services import google_oauth
    if google_oauth.seed_client_from_env():
        logger.info("[BOOTSTRAP] Migrated Google OAuth client from env to DB (client_type=desktop)")


_bootstrap_env_from_db()

# Pre-seed the Runtime RPC socket path BEFORE importing agent.py — fast-agent
# resolves ``${VAR}`` placeholders in fastagent.config.yaml at module load,
# so the env var must be set already or every spawned MCP subprocess will
# inherit the literal string and fail to connect. The actual server starts
# later in the lifespan; the path itself is deterministic so the early seed
# matches what the lifespan will create.
os.environ.setdefault(
    "JARVIS_RUNTIME_RPC_SOCKET",
    str(Path(".runtime/state/runtime_rpc.sock").resolve()),
)

from helpers.audio_cache import clean_audio_cache, cleanup_stale_generating
from agent import fast

# Import shared state module (initializes singletons at import time)
import services.shared_state as state


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing FastAgent...")
    
    # Initialize spawn runtime directories (agent_cards, .runtime, etc.)
    try:
        from fast_agent.spawn.runtime_paths import ensure_runtime_dirs
        ensure_runtime_dirs(".")
        logger.info("Spawn runtime directories initialized.")
    except ImportError:
        logger.warning("fast_agent.spawn not available — skipping spawn init.")
    
    # Wire spawn events → SSE progress stream (cross-process via Unix domain socket)
    event_socket_server = None
    try:
        from services.spawn_progress_bridge import SpawnProgressBridge
        from services.spawn_event_socket import SpawnEventSocketServer
        from services.sse_progress import progress_manager
        from core.agent_registry_db import AgentRegistryDB

        # Create registry DB query adapter (reads from spawn_registry table)
        state.registry_db = AgentRegistryDB()
        logger.info("AgentRegistryDB ready (SPAWN_REGISTRY_DB=%s)", os.environ.get('SPAWN_REGISTRY_DB'))

        # Create bridge (event processor — no longer watches files)
        state.spawn_bridge = SpawnProgressBridge(progress_manager, registry_db=state.registry_db)

        # Start Unix domain socket server for receiving events from MCP subprocesses
        socket_path = str(Path(".runtime/state/spawn_events.sock").resolve())
        event_socket_server = SpawnEventSocketServer(socket_path, state.spawn_bridge)
        await event_socket_server.start()
        os.environ["SPAWN_EVENT_SOCKET"] = socket_path
        logger.info("Spawn event socket ready: %s", socket_path)
    except Exception as e:
        logger.warning("Failed to start spawn event socket: %s", e)

    # Start the RuntimeRpcServer — request/response RPC for MCP subprocesses
    # that need to mutate live backend state (e.g. skill_server delegating to
    # skill_service so rebuild_agent_instruction runs in this process). Generic
    # framework: future tools register their own handlers here too.
    # The socket path was pre-seeded into os.environ before agent.py was
    # imported (so ${JARVIS_RUNTIME_RPC_SOCKET} placeholders in
    # fastagent.config.yaml resolve correctly); we reuse the same value here.
    runtime_rpc_server = None
    try:
        from services.runtime_rpc import RuntimeRpcServer
        from services import skill_rpc_handlers

        rpc_socket_path = os.environ["JARVIS_RUNTIME_RPC_SOCKET"]
        runtime_rpc_server = RuntimeRpcServer(rpc_socket_path)
        skill_rpc_handlers.register(runtime_rpc_server)
        await runtime_rpc_server.start()
        state.runtime_rpc_server = runtime_rpc_server
        logger.info(
            "Runtime RPC bridge ready: %s (methods=%d)",
            rpc_socket_path, len(runtime_rpc_server.methods()),
        )
    except Exception as e:
        logger.warning("Failed to start Runtime RPC bridge: %s", e)
    
    # Wire meeting events → SSE stream (cross-process via SQLite)
    meeting_watcher_task = None
    try:
        from services.meeting_hooks_bridge import MeetingEventBridge
        from services.meeting_events import meeting_event_manager

        state.meeting_event_manager = meeting_event_manager
        db_path = str(Path("data/jarvis.db").resolve())
        state.meeting_bridge = MeetingEventBridge(db_path, meeting_event_manager)
        state.meeting_bridge.reset_cursor()
        meeting_watcher_task = asyncio.create_task(
            state.meeting_bridge.watch()
        )
        logger.info("Meeting event bridge watching: %s", db_path)
    except Exception as e:
        logger.warning("Failed to start meeting event bridge: %s", e)
    
    # Initialize database
    init_db()
    logger.info("Database initialized.")

    # Wire hot-reload dispatcher so DB-backed config changes (master key,
    # log level, voice engines) take effect without restarting the backend.
    try:
        from services.config_service import config_service
        from services.runtime_config import (
            apply_log_console_level,
            apply_master_key,
            reconcile_service_env,
            register_config_listeners,
        )

        # Bootstrap the master key from DB if env is empty. Without this,
        # container restarts (which blow away os.environ mutations set by
        # Setup Wizard Step 1) leave JARVIS_API_KEY unset → crypto can't
        # decrypt previously-stored secrets → verify_api_key silently falls
        # back to dev mode (open access).
        if not os.environ.get("JARVIS_API_KEY"):
            stored_master = config_service.get("auth", "JARVIS_API_KEY")
            if stored_master:
                apply_master_key(stored_master)
                logger.info("[BOOTSTRAP] Restored JARVIS_API_KEY from DB (env was empty)")

        # Reconcile cached globals with whatever is already stored in the DB
        # — otherwise a value set in a previous run and read before the UI
        # touches it would appear stale.
        stored_level = config_service.get("system", "LOG_CONSOLE_LEVEL")
        if stored_level:
            try:
                apply_log_console_level(stored_level)
            except ValueError as exc:
                logger.warning("[RUNTIME] Ignoring invalid stored LOG_CONSOLE_LEVEL: %s", exc)

        stored_tz = config_service.get("system", "TIMEZONE")
        if stored_tz:
            try:
                from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
                ZoneInfo(stored_tz)  # validate before writing to env
                os.environ["JARVIS_TIMEZONE"] = stored_tz
                logger.info("[BOOTSTRAP] Timezone set to %s", stored_tz)
            except (ValueError, ZoneInfoNotFoundError) as exc:
                logger.warning("[RUNTIME] Ignoring invalid stored TIMEZONE: %s", exc)

        # Voice config (registry-aware, DB-backed JSON). Always run — apply_*
        # falls back to registry defaults when the DB key is absent, so this
        # doubles as the bootstrap path for first-run users. STT is NOT
        # eager-loaded (heavy: torch + whisper); /ws/voice builds it lazily.
        try:
            from services.runtime_config import (
                apply_voice_chat_config,
                apply_voice_stories_config,
            )
            apply_voice_chat_config(None)
            apply_voice_stories_config(None)
        except Exception as exc:
            logger.warning("[BOOTSTRAP] Voice provider bootstrap skipped: %s", exc)

        # Per-provider LLM config: migrate any legacy single-slot keys left
        # over from pre-namespaced installs, then push whatever is stored
        # under the new schema into the env + fastagent.secrets.yaml so
        # fast-agent subprocesses spawn with the right credentials.
        from services.llm_provider_sync import (
            ensure_provider_sections,
            migrate_legacy_keys,
            reconcile_from_db,
        )
        migrate_legacy_keys(config_service)
        reconcile_from_db(config_service)
        # Guarantee every supported provider section has a non-empty api_key
        # placeholder so fast-agent's startup validation (runs a few lines
        # below in fast.run()) doesn't crash for providers the user hasn't
        # configured yet.
        ensure_provider_sections()

        # Seed ``os.environ`` from ``service.*`` rows so MCP subprocesses
        # (iot-control, etc.) inherit the credentials the user saved via the
        # Settings UI. Must run before ``fast.run()`` spawns those subprocs
        # — the change-listener below only fires on mutations, never on
        # startup.
        seeded = reconcile_service_env(config_service)
        if seeded:
            logger.info("[BOOTSTRAP] Seeded %d service env entr%s", seeded, "y" if seeded == 1 else "ies")

        register_config_listeners(config_service)
    except Exception as exc:
        logger.warning("Runtime hot-reload wiring skipped: %s", exc)

    # Mirror ``service.github.*`` DB rows into ``/app/git-credentials`` +
    # ``/app/gitconfig`` (ephemeral in container workspace, regenerated
    # from the DB every boot — see services/git_credential_sync.py) and
    # into ``fastagent.secrets.yaml`` for the github MCP. Must complete
    # BEFORE ``fast.run()`` below so neither MCP subprocesses nor the
    # agent shell can invoke git before ``GIT_CONFIG_GLOBAL`` points at
    # a valid file.
    #
    # Deliberately NOT wrapped in the hot-reload try/except above: the
    # whole reason this module exists is to prevent dev-agent ``git clone``
    # from silently failing at runtime (prod incident 2026-04-24). Letting
    # a disk-full / permission / DB-corrupt failure get swallowed to a
    # WARNING here would reproduce the exact class of bug. Fail lifespan
    # loud → container crash-loops → operator sees the real error.
    from services.config_service import config_service as _cfg_for_git_sync
    from services import git_credential_sync
    git_credential_sync.reconcile_from_db(_cfg_for_git_sync)
    
    # Restore pending approvals — re-register paused agents from DB
    try:
        from services.approval_service import approval_service
        restored = approval_service.restore_pending_on_startup()
        if restored:
            logger.info("Restored %d approval-paused agents from pending approvals", restored)
    except Exception as e:
        logger.warning("Approval restore skipped: %s", e)
    
    # No more JSON → SQLite migration needed.
    # SpawnRegistry now uses SqliteBackend directly (via SPAWN_REGISTRY_DB env var).
    # One-time migration: seed spawn_registry table from old JSON file
    try:
        json_registry = Path(".runtime/state/spawn_registry.json")
        if json_registry.exists() and json_registry.stat().st_size > 2:
            import json as _json
            old_data = _json.loads(json_registry.read_text("utf-8"))
            if isinstance(old_data, dict) and old_data:
                from fast_agent.spawn.registry_backends import SqliteBackend
                db_path = os.environ.get("SPAWN_REGISTRY_DB", "data/jarvis.db")
                backend = SqliteBackend(db_path)
                # Merge: load existing + overlay old JSON records
                existing = backend.load()
                for run_id, rec in old_data.items():
                    if run_id not in existing:
                        existing[run_id] = rec
                backend.save(existing)
                # Rename to .bak to avoid re-migrating
                json_registry.rename(json_registry.with_suffix(".json.bak"))
                logger.info("Migrated %d records from JSON→SQLite, renamed to .bak", len(old_data))
    except Exception as e:
        logger.warning("JSON migration skipped: %s", e)
    
    # Clean up stale running records from previous sessions (PIDs that are dead)
    try:
        if hasattr(state, 'registry_db') and state.registry_db:
            stale_count = state.registry_db.mark_stale_running()
            if stale_count > 0:
                logger.info("Cleaned up %d stale running spawn records at startup", stale_count)
    except Exception as e:
        logger.warning("Stale record cleanup skipped: %s", e)
    
    # Cleanup Audio Cache on Startup
    cleanup_stale_generating()  # Reset stuck 'generating' entries from prev crash
    clean_audio_cache()
    
    # Clean stale TTS lock files — nothing can be generating before scheduler starts,
    # so ALL lock files at this point are guaranteed stale (from crashes/restarts).
    import glob
    stale_locks = glob.glob(os.path.join("data", "audio_cache", "*.lock"))
    if stale_locks:
        for lock_path in stale_locks:
            try:
                os.remove(lock_path)
                # Also remove partial audio file if it exists
                audio_path = lock_path.replace(".lock", "")
                if os.path.exists(audio_path):
                    os.remove(audio_path)
            except OSError:
                pass
        logger.info(f"Cleaned {len(stale_locks)} stale TTS lock files on startup")
    
    # Start Background Job Scheduler
    from services.background_jobs import BackgroundJobScheduler
    from services.tts_pregen_job import TTSPreGenJob
    from services.pregen_stream import pregen_stream_manager
    state.bg_scheduler = BackgroundJobScheduler()
    state.bg_scheduler.set_generation_tasks_ref(state.generation_tasks)
    state.bg_scheduler.set_pregen_stream(pregen_stream_manager)
    pregen_job = TTSPreGenJob(state.bg_scheduler, pregen_stream=pregen_stream_manager)
    state.bg_scheduler.register_job(pregen_job)
    scheduler_task = asyncio.create_task(state.bg_scheduler.start())
    logger.info("Background Job Scheduler started.")
    
    # Start Cron Scheduler (event-driven, runs its own loop)
    from services.cron_scheduler import cron_scheduler, CronBackgroundJob
    state.cron_scheduler = cron_scheduler
    cron_bg_job = CronBackgroundJob(cron_scheduler)
    state.bg_scheduler.register_job(cron_bg_job)
    cron_task = asyncio.create_task(cron_scheduler.start())
    logger.info("Cron Scheduler started.")
    
    # Start CrawlPoller — picks up pending crawl jobs from DB
    from services.crawl_poller import CrawlPoller
    state.crawl_poller = CrawlPoller()
    crawl_poller_task = asyncio.create_task(state.crawl_poller.start())
    logger.info("CrawlPoller started.")
    
    # Enable shell execution runtime (equivalent to --shell CLI flag)
    await fast.app.initialize()
    setattr(fast.app.context, "shell_runtime", True)
    logger.info("Shell execution runtime enabled on context: %s", getattr(fast.app.context, "shell_runtime", False))
    
    async with fast.run() as agent:
        state.agent_app = agent
        logger.info("FastAgent initialized.")
        
        # Wire CronScheduler agent references (for agent_turn execution)
        if state.cron_scheduler:
            state.cron_scheduler.set_agent_refs(agent, state.session_service)
        
        # Pre-load dynamic agent cards and attach to Jarvis
        from services.dynamic_agents import preload_agent_cards, signal_reload_loop
        loaded = await preload_agent_cards(agent)
        if loaded:
            logger.info("Dynamic agents ready: %s", loaded)
        
        # Start reload loop for hot-loading agent card changes
        reload_task = asyncio.create_task(signal_reload_loop(agent))
        
        # Populate MCP server tools DB from static agents' aggregators
        cached_servers = set()
        try:
            if state.registry_db:
                tools_by_server = {}
                for ag_name in fast.agents:
                    ag = agent.get_agent(ag_name)
                    if ag is None:
                        continue
                    agg = getattr(ag, "_aggregator", None)
                    if not agg:
                        continue
                    server_tool_map = getattr(agg, "_server_to_tool_map", {})
                    for svr, nts in server_tool_map.items():
                        if svr not in tools_by_server:
                            tools_by_server[svr] = [
                                {"name": nt.tool.name, "description": getattr(nt.tool, "description", "") or ""}
                                for nt in nts
                            ]
                if tools_by_server:
                    cached = state.registry_db.bulk_upsert_server_tools(tools_by_server)
                    cached_servers = set(tools_by_server.keys())
                    logger.info("MCP server tools: cached %d servers from aggregators", cached)
        except Exception as e:
            logger.warning("MCP server tools cache failed: %s", e)
        
        # Background: discover tools for uncached servers using MCP SDK
        async def _discover_uncached_servers():
            """Briefly connect to uncached MCP servers to list their tools."""
            import yaml as _yaml
            try:
                config_path = Path(__file__).parent / "fastagent.config.yaml"
                if not config_path.exists():
                    return
                
                with open(config_path) as f:
                    raw_cfg = _yaml.safe_load(f) or {}
                
                server_configs = raw_cfg.get("mcp", {}).get("servers", {})
                
                # Merge overrides from secrets file (e.g. local paths for figma-ui-mcp)
                secrets_path = Path(__file__).parent / "fastagent.secrets.yaml"
                if secrets_path.exists():
                    try:
                        with open(secrets_path) as f:
                            secrets_cfg = _yaml.safe_load(f) or {}
                        secrets_servers = secrets_cfg.get("mcp", {}).get("servers", {})
                        for svr_name, svr_override in secrets_servers.items():
                            if svr_name in server_configs:
                                # Deep merge: merge env dicts instead of overwriting
                                for key, val in svr_override.items():
                                    if key == "env" and isinstance(val, dict) and isinstance(server_configs[svr_name].get("env"), dict):
                                        server_configs[svr_name]["env"].update(val)
                                    else:
                                        server_configs[svr_name][key] = val
                            else:
                                server_configs[svr_name] = svr_override
                    except Exception:
                        pass
                
                if not server_configs:
                    return
                
                # Also skip servers already in DB from previous runs
                existing_in_db = set()
                if state.registry_db:
                    for svr_name in server_configs:
                        if svr_name not in cached_servers:
                            tools = state.registry_db.get_server_tools([svr_name])
                            if tools:
                                existing_in_db.add(svr_name)
                
                skip = cached_servers | existing_in_db
                uncached = [s for s in server_configs if s not in skip]
                if not uncached:
                    logger.info("MCP server tools: all %d servers cached (aggregator=%d, db=%d)",
                               len(server_configs), len(cached_servers), len(existing_in_db))
                    return
                
                logger.info("MCP server tools: discovering %d uncached servers: %s", len(uncached), uncached)
                
                from mcp.client.stdio import stdio_client, StdioServerParameters
                from mcp import ClientSession
                import os
                
                discovered = {}
                for server_name in uncached:
                    svr_cfg = server_configs[server_name]
                    command = svr_cfg.get("command")
                    if not command:
                        # SSE/non-stdio servers — skip
                        continue
                    
                    args = svr_cfg.get("args", [])
                    # Skip OAuth-based servers (mcp-remote requires browser auth)
                    if any("mcp-remote" in str(a) for a in [command] + args):
                        logger.debug("MCP server tools: skipping %s (uses mcp-remote/OAuth)", server_name)
                        continue
                    
                    env_cfg = svr_cfg.get("env") or {}
                    # Resolve env vars in args (e.g. ${SERPAPI_API_KEY})
                    import re
                    resolved_args = []
                    for a in args:
                        if isinstance(a, str) and "${" in a:
                            a = re.sub(r'\$\{(\w+)\}', lambda m: os.environ.get(m.group(1), m.group(0)), a)
                        resolved_args.append(str(a))
                    
                    # Resolve env vars in env config values too
                    # Look up from both os.environ and env_cfg itself (for cross-refs)
                    lookup = {**os.environ, **{k: str(v) for k, v in env_cfg.items()}}
                    resolved_env = {}
                    for k, v in env_cfg.items():
                        if isinstance(v, str) and "${" in v:
                            v = re.sub(r'\$\{(\w+)\}', lambda m: lookup.get(m.group(1), m.group(0)), v)
                        resolved_env[k] = str(v)
                    
                    # Merge env
                    server_env = {**os.environ, **resolved_env} if resolved_env else None
                    
                    try:
                        params = StdioServerParameters(
                            command=command,
                            args=resolved_args,
                            env=server_env,
                        )
                        async with stdio_client(params) as (read_stream, write_stream):
                            async with ClientSession(read_stream, write_stream) as session:
                                await session.initialize()
                                result = await session.list_tools()
                                tools = [
                                    {"name": t.name, "description": t.description or ""}
                                    for t in result.tools
                                ]
                                if tools:
                                    discovered[server_name] = tools
                                    logger.debug("MCP server tools: %s → %d tools", server_name, len(tools))
                    except Exception as e:
                        logger.debug("MCP server tools: failed to discover %s: %s", server_name, e)
                
                if discovered and state.registry_db:
                    count = state.registry_db.bulk_upsert_server_tools(discovered)
                    logger.info("MCP server tools: discovered %d servers in background: %s",
                               count, list(discovered.keys()))
            except Exception as e:
                logger.warning("MCP server tools: background discovery failed: %s", e)
        
        asyncio.create_task(_discover_uncached_servers())
        
        print(f"\n{'═' * 50}")
        print(f"🚀 Jarvis backend ready | agents={len(fast.agents)} | mcp_cached={len(cached_servers)}")
        print(f"{'═' * 50}\n")
        
        yield
    
    # Shutdown reload loop
    reload_task.cancel()
    try:
        await reload_task
    except asyncio.CancelledError:
        pass
    
    # Shutdown spawn event socket server
    if event_socket_server:
        await event_socket_server.stop()

    # Shutdown runtime RPC bridge
    if runtime_rpc_server:
        try:
            await runtime_rpc_server.stop()
        except Exception:
            logger.exception("Failed to stop runtime RPC server")
    
    # Shutdown meeting event watcher
    if meeting_watcher_task:
        meeting_watcher_task.cancel()
        try:
            await meeting_watcher_task
        except asyncio.CancelledError:
            pass
    
    
    
    # Kill all spawned agent processes via PID from SQLite registry
    try:
        import signal as _signal
        if hasattr(state, 'registry_db') and state.registry_db:
            all_records = state.registry_db.get_all()
            killed = 0
            for run_id, rec in all_records.items():
                status = rec.get("status")
                if status not in ("running", "pending", "idle", "paused"):
                    continue
                pid = rec.get("pid")
                if pid:
                    try:
                        os.kill(pid, _signal.SIGTERM)
                        killed += 1
                        logger.info("Killed spawn pid=%s agent=%s", pid, rec.get("agent_name", run_id))
                    except (ProcessLookupError, PermissionError):
                        pass
            if killed:
                logger.info("Shutdown cleanup: killed %d spawn processes", killed)
    except Exception as e:
        logger.warning("Error during spawn PID cleanup: %s", e)
    
    # Shutdown CrawlPoller
    state.crawl_poller.stop()
    crawl_poller_task.cancel()
    
    # Shutdown Cron Scheduler
    cron_scheduler.stop()
    cron_task.cancel()
    
    # Shutdown scheduler
    state.bg_scheduler.stop()
    scheduler_task.cancel()
    logger.info("FastAgent shutdown.")


app = FastAPI(lifespan=lifespan)

# CORS Configuration
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS if CORS_ORIGINS != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Setup-gate middleware: blocks non-bootstrap API traffic until the Setup
# Wizard is complete. Registered *after* CORS so pre-flight requests still
# succeed even when the API is gated.
from middleware.setup_gate import SetupGateMiddleware, refresh_setup_complete

app.add_middleware(SetupGateMiddleware)
refresh_setup_complete()

# Register all route modules
from routes import all_routers
for router in all_routers:
    app.include_router(router)
