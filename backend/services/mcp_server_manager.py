"""MCP server subprocess lifecycle manager.

Owns restart / install / uninstall for named MCP servers defined in
fastagent.config.yaml. Complements fast-agent's one-time boot spawn:
fast-agent connects to all servers at startup; this manager handles
mid-run restarts and dynamic additions.

Usage pattern
-------------
1.  Declare dependencies once at import time::

        mcp_server_manager.register_config_dep("time-service", "system", "TIMEZONE")

2.  In runtime_config listeners, call after updating os.environ::

        mcp_server_manager.restart_for_config_change("system", "TIMEZONE")

3.  For dynamic install/uninstall (future)::

        mcp_server_manager.install("my-tool", command="python",
                                   args=["tools/my_tool.py"])
        mcp_server_manager.uninstall("my-tool")

Reconnect caveat
----------------
fast-agent holds stdio connections to each subprocess. Killing and
respawning a subprocess breaks that connection — fast-agent will error
on the next call to that server. fast-agent v0.x does not auto-reconnect
for stdio transport. Until we add a reconnect hook or switch to HTTP
transport, callers should treat restart() as best-effort: the subprocess
restarts with updated env, but fast-agent may need a full backend restart
to re-establish the connection.
"""
from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_FASTAGENT_CONFIG = Path(__file__).parent.parent / "fastagent.config.yaml"
_PLACEHOLDER_RE = re.compile(r"\$\{([^}]+)\}")


def _resolve_env(raw_env: dict[str, Any]) -> dict[str, str]:
    """Expand ${VAR} placeholders in a server's env block against os.environ."""
    result: dict[str, str] = {}
    for k, v in raw_env.items():
        s = str(v) if v is not None else ""
        resolved = _PLACEHOLDER_RE.sub(
            lambda m: os.environ.get(m.group(1), ""), s
        )
        result[k] = resolved
    return result


class MCPServerManager:
    def __init__(self, config_path: Path = _FASTAGENT_CONFIG) -> None:
        self._config_path = config_path
        # name -> {command, args, env, url}
        self._servers: dict[str, dict[str, Any]] = {}
        # PIDs of subprocesses *we* spawned (not fast-agent's)
        self._managed_pids: dict[str, int] = {}
        # (category, key) -> [server_name, ...]
        self._config_deps: dict[tuple[str, str], list[str]] = {}
        self._load_config()

    # ── Config loading ────────────────────────────────────────────────

    def _load_config(self) -> None:
        try:
            with open(self._config_path) as f:
                raw = yaml.safe_load(f) or {}
            self._servers = raw.get("mcp", {}).get("servers", {})
        except FileNotFoundError:
            logger.warning("[MCP] Config not found: %s", self._config_path)
            self._servers = {}

    def _persist_config(self) -> None:
        with open(self._config_path) as f:
            raw = yaml.safe_load(f) or {}
        raw.setdefault("mcp", {})["servers"] = self._servers
        with open(self._config_path, "w") as f:
            yaml.dump(raw, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    # ── Dependency registry ───────────────────────────────────────────

    def register_config_dep(self, server_name: str, category: str, key: str) -> None:
        """Declare that server_name must restart when config category/key changes."""
        self._config_deps.setdefault((category, key), []).append(server_name)

    def restart_for_config_change(self, category: str, key: str) -> list[str]:
        """Restart every server that declared a dependency on category/key.

        Must be called *after* os.environ has already been updated with the
        new value so spawned subprocesses inherit it.

        Returns the list of server names that were successfully restarted.
        """
        servers = self._config_deps.get((category, key), [])
        restarted: list[str] = []
        for name in servers:
            try:
                self.restart(name)
                restarted.append(name)
            except Exception:
                logger.exception("[MCP] restart failed for %s after %s/%s change", name, category, key)
        return restarted

    # ── Lifecycle: restart / install / uninstall ──────────────────────

    def restart(self, name: str) -> None:
        """Kill the named MCP server subprocess and respawn it with current env."""
        self._stop(name)
        self._spawn(name)

    def install(
        self,
        name: str,
        *,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
    ) -> None:
        """Register a new MCP server, start it, then write config on success."""
        if name in self._servers:
            raise ValueError(f"Server {name!r} already exists; call restart() to reload.")
        entry: dict[str, Any] = {"command": command, "args": args}
        if env:
            entry["env"] = env
        self._servers[name] = entry
        try:
            self._spawn(name)
        except Exception:
            self._servers.pop(name)
            raise
        self._persist_config()
        logger.info("[MCP] Installed %s", name)

    def uninstall(self, name: str) -> None:
        """Stop the named server and remove it from config."""
        self._stop(name)
        self._servers.pop(name, None)
        self._persist_config()
        logger.info("[MCP] Uninstalled %s", name)

    # ── Low-level process control ─────────────────────────────────────

    def _stop(self, name: str) -> None:
        """SIGTERM the subprocess for name, whether we spawned it or fast-agent did."""
        # Our own managed PIDs take priority.
        pid = self._managed_pids.get(name)
        if pid is not None:
            if self._pid_alive(pid):
                self._sigterm(pid)
                self._managed_pids.pop(name, None)
                logger.info("[MCP] Stopped %s (managed pid=%d)", name, pid)
                return
            else:
                # PID already dead — clean up stale entry before falling through to pgrep.
                self._managed_pids.pop(name, None)
                logger.debug("[MCP] Managed PID %d for %s already dead, cleaning up", pid, name)

        # Fall back: search by cmdline for servers fast-agent spawned.
        cfg = self._servers.get(name, {})
        args = cfg.get("args", [])
        if args:
            pattern = " ".join(str(a) for a in args)
            found = _find_pids_by_cmdline(pattern)
            for p in found:
                self._sigterm(p)
            if found:
                logger.info("[MCP] Stopped %s via pgrep (pids=%s)", name, found)

    def _spawn(self, name: str) -> None:
        """Start the named MCP server as a subprocess, recording its PID."""
        cfg = self._servers.get(name)
        if cfg is None:
            raise ValueError(f"Unknown MCP server: {name!r}")

        command = cfg.get("command")
        if not command:
            # URL-based servers (e.g. serpapi) have no subprocess to manage.
            logger.info("[MCP] %s is URL-based; skipping spawn", name)
            return

        args: list[str] = cfg.get("args", [])
        raw_env: dict[str, Any] = cfg.get("env", {})

        child_env = {**os.environ, **_resolve_env(raw_env)}
        cwd = str(self._config_path.parent)

        proc = subprocess.Popen(
            [command, *args],
            cwd=cwd,
            env=child_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._managed_pids[name] = proc.pid
        logger.info("[MCP] Spawned %s (pid=%d)", name, proc.pid)

    # ── Introspection ─────────────────────────────────────────────────

    def status(self, name: str) -> str:
        """Return 'running', 'dead', or 'unmanaged'."""
        pid = self._managed_pids.get(name)
        if pid is None:
            return "unmanaged"
        return "running" if self._pid_alive(pid) else "dead"

    def list_servers(self) -> list[str]:
        return list(self._servers.keys())

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    @staticmethod
    def _sigterm(pid: int) -> None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass  # already gone


def _find_pids_by_cmdline(pattern: str) -> list[int]:
    """Return PIDs whose cmdline contains pattern (via pgrep -f)."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True, text=True, timeout=3,
        )
        return [int(p) for p in result.stdout.split() if p.strip()]
    except Exception:
        logger.debug("[MCP] pgrep failed for pattern %r", pattern)
        return []


# ── Module-level singleton + dependency declarations ──────────────────────

mcp_server_manager = MCPServerManager()
