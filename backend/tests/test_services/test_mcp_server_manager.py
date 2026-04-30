"""Unit tests for services.mcp_server_manager — MCP subprocess lifecycle.

Coverage map
------------
_resolve_env          — plain values, ${VAR} expansion, missing vars, None
_load_config          — normal YAML, missing file, empty YAML, missing keys
register_config_dep   — single, multi-server, same key twice
restart_for_change    — no deps, success, partial failure, all fail
_pid_alive            — alive, ProcessLookupError, PermissionError
_sigterm              — success, already-dead pid
_find_pids_by_cmdline — match, no match, whitespace output, timeout, exception
_stop                 — managed+alive, managed+dead(fallback), unmanaged+pgrep, no args
_spawn                — normal, url-based skipped, unknown raises, env merge, popen fail
restart               — stop then spawn ordering
install               — new server, duplicate raises, persists, spawns
uninstall             — removes, persists, stops, nonexistent safe
_persist_config       — writes servers, preserves non-mcp keys, creates mcp section
status                — unmanaged, running, dead
list_servers          — all names, post-install, post-uninstall
"""
from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
import yaml

from services.mcp_server_manager import (
    MCPServerManager,
    _find_pids_by_cmdline,
    _resolve_env,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

_BASE_CONFIG: dict = {
    "other_key": "preserved",
    "mcp": {
        "servers": {
            "time-service": {
                "command": "python",
                "args": ["tools/time_server.py"],
            },
            "gmail": {
                "command": "python",
                "args": ["tools/gmail_server.py"],
                "env": {
                    "PYTHONUNBUFFERED": "1",
                    "JARVIS_API_KEY": "${JARVIS_API_KEY}",
                },
            },
            "serpapi": {
                "url": "https://mcp.serpapi.com/${SERPAPI_API_KEY}/mcp",
            },
        }
    },
}


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "fastagent.config.yaml"
    with open(p, "w") as f:
        yaml.dump(_BASE_CONFIG, f, default_flow_style=False)
    return p


@pytest.fixture()
def mgr(config_file: Path) -> MCPServerManager:
    return MCPServerManager(config_path=config_file)


def _fake_proc(pid: int = 55555) -> MagicMock:
    p = MagicMock()
    p.pid = pid
    return p


# ── _resolve_env ──────────────────────────────────────────────────────────────

class TestResolveEnv:
    def test_plain_values_passed_through(self):
        assert _resolve_env({"A": "1", "B": "hello"}) == {"A": "1", "B": "hello"}

    def test_placeholder_resolved_from_environ(self, monkeypatch):
        monkeypatch.setenv("MY_TOKEN", "secret123")
        assert _resolve_env({"TOKEN": "${MY_TOKEN}"})["TOKEN"] == "secret123"

    def test_missing_placeholder_resolves_to_empty_string(self, monkeypatch):
        monkeypatch.delenv("GHOST_VAR", raising=False)
        assert _resolve_env({"K": "${GHOST_VAR}"})["K"] == ""

    def test_none_value_becomes_empty_string(self):
        assert _resolve_env({"K": None})["K"] == ""

    def test_empty_dict(self):
        assert _resolve_env({}) == {}

    def test_mixed_plain_placeholder_and_missing(self, monkeypatch):
        monkeypatch.setenv("API_KEY", "mykey")
        monkeypatch.delenv("NOT_SET", raising=False)
        result = _resolve_env({
            "PLAIN": "hello",
            "SECRET": "${API_KEY}",
            "MISSING": "${NOT_SET}",
        })
        assert result == {"PLAIN": "hello", "SECRET": "mykey", "MISSING": ""}

    def test_non_placeholder_dollar_sign_preserved(self):
        # "$100" is not a ${VAR} placeholder — must not be touched
        assert _resolve_env({"PRICE": "$100"})["PRICE"] == "$100"

    def test_multiple_placeholders_in_one_value(self, monkeypatch):
        monkeypatch.setenv("HOST", "localhost")
        monkeypatch.setenv("PORT", "8080")
        result = _resolve_env({"URL": "${HOST}:${PORT}"})
        assert result["URL"] == "localhost:8080"


# ── Config loading ─────────────────────────────────────────────────────────────

class TestLoadConfig:
    def test_parses_all_servers(self, mgr: MCPServerManager):
        assert set(mgr._servers) == {"time-service", "gmail", "serpapi"}

    def test_command_and_args_preserved(self, mgr: MCPServerManager):
        assert mgr._servers["time-service"]["command"] == "python"
        assert mgr._servers["time-service"]["args"] == ["tools/time_server.py"]

    def test_server_env_block_preserved(self, mgr: MCPServerManager):
        assert mgr._servers["gmail"]["env"]["PYTHONUNBUFFERED"] == "1"

    def test_url_server_has_no_command(self, mgr: MCPServerManager):
        assert "command" not in mgr._servers["serpapi"]

    def test_missing_file_yields_empty_and_warns(self, tmp_path: Path, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="services.mcp_server_manager"):
            m = MCPServerManager(config_path=tmp_path / "no_such_file.yaml")
        assert m._servers == {}
        assert "not found" in caplog.text.lower() or "config" in caplog.text.lower()

    def test_empty_yaml_yields_empty_servers(self, tmp_path: Path):
        p = tmp_path / "empty.yaml"
        p.write_text("")
        assert MCPServerManager(config_path=p)._servers == {}

    def test_yaml_without_mcp_key(self, tmp_path: Path):
        p = tmp_path / "no_mcp.yaml"
        p.write_text("other: true\n")
        assert MCPServerManager(config_path=p)._servers == {}

    def test_yaml_with_mcp_but_no_servers(self, tmp_path: Path):
        p = tmp_path / "no_servers.yaml"
        p.write_text("mcp:\n  other: value\n")
        assert MCPServerManager(config_path=p)._servers == {}


# ── Dependency registry ───────────────────────────────────────────────────────

class TestConfigDeps:
    def test_single_dep_registered(self, mgr: MCPServerManager):
        mgr.register_config_dep("time-service", "system", "TIMEZONE")
        assert "time-service" in mgr._config_deps[("system", "TIMEZONE")]

    def test_multiple_servers_same_key(self, mgr: MCPServerManager):
        mgr.register_config_dep("time-service", "system", "TIMEZONE")
        mgr.register_config_dep("calendar", "system", "TIMEZONE")
        deps = mgr._config_deps[("system", "TIMEZONE")]
        assert "time-service" in deps
        assert "calendar" in deps

    def test_no_deps_restart_returns_empty(self, mgr: MCPServerManager):
        assert mgr.restart_for_config_change("system", "NONEXISTENT") == []

    def test_restart_for_change_calls_restart_for_each_dep(self, mgr: MCPServerManager, monkeypatch):
        mgr.register_config_dep("time-service", "system", "TZ")
        mgr.register_config_dep("cal", "system", "TZ")
        restarted = []
        monkeypatch.setattr(mgr, "restart", lambda n: restarted.append(n))
        result = mgr.restart_for_config_change("system", "TZ")
        assert set(result) == {"time-service", "cal"}
        assert set(restarted) == {"time-service", "cal"}

    def test_failing_server_does_not_block_others(self, mgr: MCPServerManager, monkeypatch):
        mgr.register_config_dep("bad", "system", "TZ")
        mgr.register_config_dep("good", "system", "TZ")
        good_calls = []

        def fake_restart(name: str):
            if name == "bad":
                raise RuntimeError("popen failed")
            good_calls.append(name)

        monkeypatch.setattr(mgr, "restart", fake_restart)
        result = mgr.restart_for_config_change("system", "TZ")
        assert result == ["good"]
        assert good_calls == ["good"]

    def test_all_fail_returns_empty_list(self, mgr: MCPServerManager, monkeypatch):
        mgr.register_config_dep("bad", "system", "TZ")
        monkeypatch.setattr(mgr, "restart", lambda _: (_ for _ in ()).throw(RuntimeError()))
        assert mgr.restart_for_config_change("system", "TZ") == []


# ── _pid_alive ────────────────────────────────────────────────────────────────

class TestPidAlive:
    def test_returns_true_for_live_pid(self):
        with patch("services.mcp_server_manager.os.kill", return_value=None):
            assert MCPServerManager._pid_alive(12345) is True

    def test_returns_false_on_process_lookup_error(self):
        with patch("services.mcp_server_manager.os.kill", side_effect=ProcessLookupError):
            assert MCPServerManager._pid_alive(99999) is False

    def test_returns_false_on_permission_error(self):
        # Process exists but belongs to another user — not ours to manage.
        with patch("services.mcp_server_manager.os.kill", side_effect=PermissionError):
            assert MCPServerManager._pid_alive(1) is False

    def test_sends_signal_zero(self):
        with patch("services.mcp_server_manager.os.kill") as mk:
            mk.return_value = None
            MCPServerManager._pid_alive(42)
        mk.assert_called_once_with(42, 0)


# ── _sigterm ──────────────────────────────────────────────────────────────────

class TestSigterm:
    def test_sends_sigterm(self):
        with patch("services.mcp_server_manager.os.kill") as mk:
            MCPServerManager._sigterm(12345)
        mk.assert_called_once_with(12345, signal.SIGTERM)

    def test_already_dead_pid_does_not_raise(self):
        with patch("services.mcp_server_manager.os.kill", side_effect=ProcessLookupError):
            MCPServerManager._sigterm(99999)  # must not raise


# ── _find_pids_by_cmdline ─────────────────────────────────────────────────────

class TestFindPids:
    def _run_mock(self, stdout: str) -> MagicMock:
        m = MagicMock()
        m.stdout = stdout
        return m

    def test_returns_list_of_pids(self):
        with patch("services.mcp_server_manager.subprocess.run", return_value=self._run_mock("123\n456\n789\n")):
            assert _find_pids_by_cmdline("time_server.py") == [123, 456, 789]

    def test_single_pid(self):
        with patch("services.mcp_server_manager.subprocess.run", return_value=self._run_mock("42\n")):
            assert _find_pids_by_cmdline("x") == [42]

    def test_empty_output_returns_empty(self):
        with patch("services.mcp_server_manager.subprocess.run", return_value=self._run_mock("")):
            assert _find_pids_by_cmdline("nothing") == []

    def test_whitespace_only_output_returns_empty(self):
        with patch("services.mcp_server_manager.subprocess.run", return_value=self._run_mock("  \n  \n")):
            assert _find_pids_by_cmdline("nothing") == []

    def test_timeout_returns_empty(self):
        with patch(
            "services.mcp_server_manager.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="pgrep", timeout=3),
        ):
            assert _find_pids_by_cmdline("pattern") == []

    def test_file_not_found_returns_empty(self):
        with patch(
            "services.mcp_server_manager.subprocess.run",
            side_effect=FileNotFoundError("pgrep not found"),
        ):
            assert _find_pids_by_cmdline("pattern") == []

    def test_other_exception_returns_empty(self):
        with patch(
            "services.mcp_server_manager.subprocess.run",
            side_effect=OSError("unexpected"),
        ):
            assert _find_pids_by_cmdline("pattern") == []

    def test_passes_correct_pgrep_command(self):
        with patch("services.mcp_server_manager.subprocess.run", return_value=self._run_mock("")) as mk:
            _find_pids_by_cmdline("tools/time_server.py")
        mk.assert_called_once()
        cmd = mk.call_args[0][0]
        assert cmd == ["pgrep", "-f", "tools/time_server.py"]

    def test_uses_timeout_argument(self):
        with patch("services.mcp_server_manager.subprocess.run", return_value=self._run_mock("")) as mk:
            _find_pids_by_cmdline("x")
        assert mk.call_args[1].get("timeout") is not None


# ── _stop ─────────────────────────────────────────────────────────────────────

class TestStop:
    def test_managed_alive_sends_sigterm_and_removes_pid(self, mgr: MCPServerManager):
        mgr._managed_pids["time-service"] = 11111

        def kill_side(pid, sig):
            if sig == 0:
                return  # alive check passes
            # SIGTERM — ok

        with patch("services.mcp_server_manager.os.kill", side_effect=kill_side) as mk:
            mgr._stop("time-service")

        assert "time-service" not in mgr._managed_pids
        assert call(11111, signal.SIGTERM) in mk.call_args_list

    def test_managed_dead_pid_falls_through_to_pgrep(self, mgr: MCPServerManager):
        mgr._managed_pids["time-service"] = 11111
        # os.kill(pid, 0) raises → considered dead → stale PID cleaned, fall through to pgrep
        with (
            patch("services.mcp_server_manager.os.kill", side_effect=ProcessLookupError),
            patch("services.mcp_server_manager.subprocess.run", return_value=MagicMock(stdout="")) as mk_run,
        ):
            mgr._stop("time-service")
        mk_run.assert_called_once()
        assert "pgrep" in mk_run.call_args[0][0]
        assert "time-service" not in mgr._managed_pids  # stale PID must be cleaned up

    def test_no_managed_pid_pgrep_finds_process(self, mgr: MCPServerManager):
        assert "time-service" not in mgr._managed_pids
        with (
            patch("services.mcp_server_manager.subprocess.run", return_value=MagicMock(stdout="22222\n")),
            patch("services.mcp_server_manager.os.kill") as mk_kill,
        ):
            mgr._stop("time-service")
        mk_kill.assert_called_once_with(22222, signal.SIGTERM)

    def test_no_managed_pid_pgrep_empty_no_kill(self, mgr: MCPServerManager):
        with (
            patch("services.mcp_server_manager.subprocess.run", return_value=MagicMock(stdout="")),
            patch("services.mcp_server_manager.os.kill") as mk_kill,
        ):
            mgr._stop("time-service")
        mk_kill.assert_not_called()

    def test_pgrep_kills_multiple_pids(self, mgr: MCPServerManager):
        with (
            patch("services.mcp_server_manager.subprocess.run", return_value=MagicMock(stdout="100\n200\n")),
            patch("services.mcp_server_manager.os.kill") as mk_kill,
        ):
            mgr._stop("time-service")
        assert call(100, signal.SIGTERM) in mk_kill.call_args_list
        assert call(200, signal.SIGTERM) in mk_kill.call_args_list

    def test_url_server_without_args_skips_pgrep(self, mgr: MCPServerManager):
        # serpapi has no args — pgrep must not be invoked
        with patch("services.mcp_server_manager.subprocess.run") as mk_run:
            mgr._stop("serpapi")
        mk_run.assert_not_called()

    def test_unknown_server_name_safe(self, mgr: MCPServerManager):
        with patch("services.mcp_server_manager.subprocess.run") as mk_run:
            mgr._stop("totally-unknown")
        mk_run.assert_not_called()


# ── _spawn ─────────────────────────────────────────────────────────────────────

class TestSpawn:
    def test_popen_called_with_command_and_args(self, mgr: MCPServerManager):
        with patch("services.mcp_server_manager.subprocess.Popen", return_value=_fake_proc()) as mk:
            mgr._spawn("time-service")
        assert mk.call_args[0][0] == ["python", "tools/time_server.py"]

    def test_cwd_is_config_parent_dir(self, mgr: MCPServerManager):
        with patch("services.mcp_server_manager.subprocess.Popen", return_value=_fake_proc()) as mk:
            mgr._spawn("time-service")
        assert mk.call_args[1]["cwd"] == str(mgr._config_path.parent)

    def test_pid_stored_after_spawn(self, mgr: MCPServerManager):
        with patch("services.mcp_server_manager.subprocess.Popen", return_value=_fake_proc(pid=42)):
            mgr._spawn("time-service")
        assert mgr._managed_pids["time-service"] == 42

    def test_child_env_inherits_os_environ(self, mgr: MCPServerManager, monkeypatch):
        monkeypatch.setenv("JARVIS_TIMEZONE", "Europe/London")
        with patch("services.mcp_server_manager.subprocess.Popen", return_value=_fake_proc()) as mk:
            mgr._spawn("time-service")
        assert mk.call_args[1]["env"]["JARVIS_TIMEZONE"] == "Europe/London"

    def test_server_env_overrides_merged(self, mgr: MCPServerManager, monkeypatch):
        monkeypatch.setenv("JARVIS_API_KEY", "the-key")
        with patch("services.mcp_server_manager.subprocess.Popen", return_value=_fake_proc()) as mk:
            mgr._spawn("gmail")
        env = mk.call_args[1]["env"]
        assert env["JARVIS_API_KEY"] == "the-key"
        assert env["PYTHONUNBUFFERED"] == "1"

    def test_missing_env_placeholder_resolves_to_empty(self, mgr: MCPServerManager, monkeypatch):
        monkeypatch.delenv("JARVIS_API_KEY", raising=False)
        with patch("services.mcp_server_manager.subprocess.Popen", return_value=_fake_proc()) as mk:
            mgr._spawn("gmail")
        assert mk.call_args[1]["env"]["JARVIS_API_KEY"] == ""

    def test_url_server_skips_spawn(self, mgr: MCPServerManager):
        with patch("services.mcp_server_manager.subprocess.Popen") as mk:
            mgr._spawn("serpapi")
        mk.assert_not_called()
        assert "serpapi" not in mgr._managed_pids

    def test_unknown_server_raises(self, mgr: MCPServerManager):
        with pytest.raises(ValueError, match="Unknown MCP server"):
            mgr._spawn("ghost-server")

    def test_popen_failure_propagates_and_no_pid_stored(self, mgr: MCPServerManager):
        with patch(
            "services.mcp_server_manager.subprocess.Popen",
            side_effect=FileNotFoundError("python not found"),
        ):
            with pytest.raises(FileNotFoundError):
                mgr._spawn("time-service")
        # Must not record a PID if spawn failed
        assert "time-service" not in mgr._managed_pids

    def test_stdio_pipes_configured(self, mgr: MCPServerManager):
        with patch("services.mcp_server_manager.subprocess.Popen", return_value=_fake_proc()) as mk:
            mgr._spawn("time-service")
        kw = mk.call_args[1]
        assert kw["stdin"] == subprocess.PIPE
        assert kw["stdout"] == subprocess.PIPE
        assert kw["stderr"] == subprocess.PIPE


# ── restart ───────────────────────────────────────────────────────────────────

class TestRestart:
    def test_stop_called_before_spawn(self, mgr: MCPServerManager, monkeypatch):
        order: list[str] = []
        monkeypatch.setattr(mgr, "_stop", lambda _: order.append("stop"))
        monkeypatch.setattr(mgr, "_spawn", lambda _: order.append("spawn"))
        mgr.restart("time-service")
        assert order == ["stop", "spawn"]

    def test_both_called_with_server_name(self, mgr: MCPServerManager, monkeypatch):
        names: dict[str, list] = {"stop": [], "spawn": []}
        monkeypatch.setattr(mgr, "_stop", lambda n: names["stop"].append(n))
        monkeypatch.setattr(mgr, "_spawn", lambda n: names["spawn"].append(n))
        mgr.restart("time-service")
        assert names["stop"] == ["time-service"]
        assert names["spawn"] == ["time-service"]


# ── install ───────────────────────────────────────────────────────────────────

class TestInstall:
    def test_adds_server_to_registry(self, mgr: MCPServerManager, monkeypatch):
        monkeypatch.setattr(mgr, "_spawn", lambda _: None)
        mgr.install("new-tool", command="python", args=["tools/new_tool.py"])
        assert "new-tool" in mgr._servers

    def test_persists_to_yaml(self, mgr: MCPServerManager, monkeypatch):
        monkeypatch.setattr(mgr, "_spawn", lambda _: None)
        mgr.install("new-tool", command="echo", args=["hi"])
        with open(mgr._config_path) as f:
            on_disk = yaml.safe_load(f)
        assert "new-tool" in on_disk["mcp"]["servers"]

    def test_with_env_stored_in_config(self, mgr: MCPServerManager, monkeypatch):
        monkeypatch.setattr(mgr, "_spawn", lambda _: None)
        mgr.install("new-tool", command="python", args=["x.py"], env={"FOO": "bar"})
        assert mgr._servers["new-tool"]["env"] == {"FOO": "bar"}

    def test_without_env_omits_env_key(self, mgr: MCPServerManager, monkeypatch):
        monkeypatch.setattr(mgr, "_spawn", lambda _: None)
        mgr.install("new-tool", command="python", args=["x.py"])
        assert "env" not in mgr._servers["new-tool"]

    def test_calls_spawn(self, mgr: MCPServerManager, monkeypatch):
        spawned: list[str] = []
        monkeypatch.setattr(mgr, "_spawn", lambda n: spawned.append(n))
        mgr.install("new-tool", command="echo", args=["hi"])
        assert spawned == ["new-tool"]

    def test_duplicate_name_raises_value_error(self, mgr: MCPServerManager, monkeypatch):
        monkeypatch.setattr(mgr, "_spawn", lambda _: None)
        with pytest.raises(ValueError, match="already exists"):
            mgr.install("time-service", command="python", args=["x.py"])

    def test_spawn_failure_rolls_back_registry_entry(self, mgr: MCPServerManager, monkeypatch):
        monkeypatch.setattr(mgr, "_spawn", lambda _: (_ for _ in ()).throw(RuntimeError("no binary")))
        with pytest.raises(RuntimeError):
            mgr.install("fail-tool", command="bad", args=[])
        assert "fail-tool" not in mgr._servers

    def test_yaml_not_written_when_spawn_fails(self, mgr: MCPServerManager, monkeypatch):
        persist_calls: list = []
        monkeypatch.setattr(mgr, "_spawn", lambda _: (_ for _ in ()).throw(RuntimeError("no binary")))
        monkeypatch.setattr(mgr, "_persist_config", lambda: persist_calls.append(1))
        with pytest.raises(RuntimeError):
            mgr.install("fail-tool", command="bad", args=[])
        assert persist_calls == []

    def test_spawn_called_before_persist_on_success(self, mgr: MCPServerManager, monkeypatch):
        order: list[str] = []
        monkeypatch.setattr(mgr, "_spawn", lambda _: order.append("spawn"))
        monkeypatch.setattr(mgr, "_persist_config", lambda: order.append("persist"))
        mgr.install("new-tool", command="echo", args=[])
        assert order == ["spawn", "persist"]


# ── uninstall ─────────────────────────────────────────────────────────────────

class TestUninstall:
    def test_removes_from_registry(self, mgr: MCPServerManager, monkeypatch):
        monkeypatch.setattr(mgr, "_stop", lambda _: None)
        mgr.uninstall("time-service")
        assert "time-service" not in mgr._servers

    def test_persists_removal(self, mgr: MCPServerManager, monkeypatch):
        monkeypatch.setattr(mgr, "_stop", lambda _: None)
        mgr.uninstall("time-service")
        with open(mgr._config_path) as f:
            on_disk = yaml.safe_load(f)
        assert "time-service" not in on_disk.get("mcp", {}).get("servers", {})

    def test_calls_stop(self, mgr: MCPServerManager, monkeypatch):
        stopped: list[str] = []
        monkeypatch.setattr(mgr, "_stop", lambda n: stopped.append(n))
        mgr.uninstall("time-service")
        assert stopped == ["time-service"]

    def test_nonexistent_server_does_not_crash(self, mgr: MCPServerManager, monkeypatch):
        monkeypatch.setattr(mgr, "_stop", lambda _: None)
        mgr.uninstall("ghost")  # must not raise


# ── _persist_config ───────────────────────────────────────────────────────────

class TestPersistConfig:
    def test_new_server_written_to_yaml(self, mgr: MCPServerManager):
        mgr._servers["new"] = {"command": "echo", "args": []}
        mgr._persist_config()
        with open(mgr._config_path) as f:
            on_disk = yaml.safe_load(f)
        assert "new" in on_disk["mcp"]["servers"]

    def test_preserves_non_mcp_top_level_keys(self, mgr: MCPServerManager):
        mgr._persist_config()
        with open(mgr._config_path) as f:
            on_disk = yaml.safe_load(f)
        assert on_disk.get("other_key") == "preserved"

    def test_creates_mcp_section_if_absent(self, tmp_path: Path):
        p = tmp_path / "bare.yaml"
        p.write_text("top_level: value\n")
        m = MCPServerManager(config_path=p)
        m._servers = {"my-server": {"command": "python", "args": ["x.py"]}}
        m._persist_config()
        with open(p) as f:
            on_disk = yaml.safe_load(f)
        assert on_disk["mcp"]["servers"]["my-server"]["command"] == "python"
        assert on_disk["top_level"] == "value"

    def test_removed_server_not_in_yaml(self, mgr: MCPServerManager):
        del mgr._servers["time-service"]
        mgr._persist_config()
        with open(mgr._config_path) as f:
            on_disk = yaml.safe_load(f)
        assert "time-service" not in on_disk["mcp"]["servers"]


# ── status / list_servers ─────────────────────────────────────────────────────

class TestStatus:
    def test_no_managed_pid_returns_unmanaged(self, mgr: MCPServerManager):
        assert mgr.status("time-service") == "unmanaged"

    def test_managed_alive_pid_returns_running(self, mgr: MCPServerManager):
        mgr._managed_pids["time-service"] = 11111
        with patch("services.mcp_server_manager.os.kill", return_value=None):
            assert mgr.status("time-service") == "running"

    def test_managed_dead_pid_returns_dead(self, mgr: MCPServerManager):
        mgr._managed_pids["time-service"] = 11111
        with patch("services.mcp_server_manager.os.kill", side_effect=ProcessLookupError):
            assert mgr.status("time-service") == "dead"

    def test_unknown_server_returns_unmanaged(self, mgr: MCPServerManager):
        assert mgr.status("ghost") == "unmanaged"


class TestListServers:
    def test_returns_all_configured_names(self, mgr: MCPServerManager):
        assert set(mgr.list_servers()) == {"time-service", "gmail", "serpapi"}

    def test_reflects_install(self, mgr: MCPServerManager, monkeypatch):
        monkeypatch.setattr(mgr, "_spawn", lambda _: None)
        mgr.install("extra", command="echo", args=[])
        assert "extra" in mgr.list_servers()

    def test_reflects_uninstall(self, mgr: MCPServerManager, monkeypatch):
        monkeypatch.setattr(mgr, "_stop", lambda _: None)
        mgr.uninstall("gmail")
        assert "gmail" not in mgr.list_servers()
