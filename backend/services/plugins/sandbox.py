"""Host-owned OCI profile for plugin MCP code; manifests cannot set privileges."""

from __future__ import annotations

import os
import re
from pathlib import Path

from fast_agent.config import MCPServerSettings

from services.plugins.package import PackageError

IMAGE = re.compile(r"^(?:[a-zA-Z0-9._:/-]+@)?sha256:[a-f0-9]{64}$")
ENVIRONMENT = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
HOST_ENV = {
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "PWD",
    "TMPDIR",
    "TEMP",
    "TMP",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
}
HOST_ENV_PREFIXES = (
    "LD_",
    "DOCKER_",
    "PYTHON",
    "NODE_",
    "XDG_",
    "SSL_",
    "GIT_",
    "UV_",
    "JARVIS_",
)
COMMANDS = {"python", "python3", "node", "npx", "uv", "uvx"}


def sandbox_settings(
    root: Path, server: dict, image: str, *, credentials: dict[str, str] | None = None
) -> MCPServerSettings:
    """No host shell, network, writable mount, daemon socket or inherited env."""
    if not isinstance(image, str) or not IMAGE.fullmatch(image):
        raise PackageError("A reviewed immutable sandbox image digest is required")
    transport = server.get("transport", server.get("type", "stdio"))
    if transport != "stdio" or any(key in server for key in ("cwd", "url", "headers")):
        raise PackageError("Invalid sandbox transport or host path configuration")
    command = server.get("command")
    if command not in COMMANDS:
        raise PackageError("Command requires an approved sandbox runtime adapter")
    declared = server.get("args", [])
    if (
        not isinstance(declared, list)
        or len(declared) > 128
        or any(not isinstance(arg, str) or len(arg) > 8192 for arg in declared)
    ):
        raise PackageError("Invalid sandbox command arguments")
    if not root.is_dir() or root.is_symlink():
        raise PackageError("Invalid plugin mount root")
    mount = root.resolve()
    host_root = os.environ.get("JARVIS_PLUGIN_HOST_ROOT")
    if host_root:
        runtime_root = os.environ.get("JARVIS_PLUGIN_RUNTIME_ROOT")
        if (
            not runtime_root
            or not Path(host_root).is_absolute()
            or ":" in host_root
            or "\\" in host_root
        ):
            raise PackageError("Invalid host plugin mount mapping")
        try:
            relative = mount.relative_to(Path(runtime_root).resolve())
        except ValueError as exc:
            raise PackageError(
                "Plugin mount is outside the configured runtime root"
            ) from exc
        mount = Path(host_root) / relative
    if ":" in str(mount):
        raise PackageError("Invalid plugin mount path")
    args = [
        "run",
        "--rm",
        "--interactive",
        "--pull=never",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user=65532:65532",
        "--pids-limit=64",
        "--memory=256m",
        "--cpus=1",
        "--stop-timeout=3",
        "--tmpfs=/tmp:rw,noexec,nosuid,size=32m,mode=1777",
        "--volume",
        f"{mount}:/plugin:ro",
        "--workdir=/plugin",
    ]
    environment = server.get("env", {})
    if not isinstance(environment, dict) or len(environment) > 64:
        raise PackageError("Invalid MCP environment")
    resolved = {}
    for key, value in environment.items():
        if (
            not isinstance(key, str)
            or not ENVIRONMENT.fullmatch(key)
            or key in HOST_ENV
            or key.startswith(HOST_ENV_PREFIXES)
        ):
            raise PackageError("Unsafe MCP environment key")
        if not isinstance(value, str) or len(value) > 8192:
            raise PackageError("Invalid MCP environment value")
        if value.startswith("${") and value.endswith("}"):
            slot = value[2:-1]
            if slot not in (credentials or {}):
                raise PackageError("MCP credentials require explicit configuration")
            value = credentials[slot]
        if not isinstance(value, str) or len(value) > 8192:
            raise PackageError("Invalid configured MCP credential")
        if "${" in value:
            raise PackageError("Unresolved MCP environment placeholder")
        resolved[key] = value
        # Pass names only in process args; values stay in the child environment.
        args.extend(["--env", key])
    mapped = [
        arg.replace("${CLAUDE_PLUGIN_ROOT}", "/plugin")
        .replace("${CODEX_PLUGIN_ROOT}", "/plugin")
        .replace("${PLUGIN_ROOT}", "/plugin")
        for arg in declared
    ]
    if any("${" in arg for arg in mapped):
        raise PackageError("Unresolved MCP argument placeholder")
    args.extend([image, command, *mapped])
    return MCPServerSettings(
        transport="stdio",
        command="docker",
        args=args,
        env=resolved,
        read_timeout_seconds=30,
        ping_interval_seconds=0,
    )
