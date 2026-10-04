"""Resolve reviewed transport settings without running package executables."""

from __future__ import annotations
import os
import sys
from pathlib import Path
from sqlalchemy import create_engine
from fast_agent.config import MCPServerSettings
from services.plugins.package import PackageError
from services.plugins.remote_auth import (
    REMOTE_POLICY,
    RemoteTokenStore,
    approved_endpoint,
)
from services.plugins.sandbox import sandbox_settings


def connection_settings(
    root: Path, name: str, config: dict, image: str, credentials: dict | None
):
    if image != REMOTE_POLICY:
        return sandbox_settings(root, config, image, credentials=credentials)
    endpoint = approved_endpoint(config)
    database = (credentials or {}).get("_database")
    runtime_root = (credentials or {}).get("_runtime_root")
    if not database or not runtime_root or not Path(database).is_file():
        raise PackageError("Connect the remote account in Settings first")
    if root.resolve() != (Path(runtime_root) / root.parent.name / "package").resolve():
        raise PackageError("Remote policy belongs to a different runtime root")
    engine = create_engine("sqlite:///" + database)
    try:
        store = RemoteTokenStore(engine, root.parent.name, name, endpoint)
        if not store.read().get("tokens") or not store.read().get("client"):
            raise PackageError("Connect the remote account in Settings first")
    finally:
        engine.dispose()
    backend = Path(__file__).resolve().parents[2]
    env = {"PYTHONPATH": str(backend)}
    if os.environ.get("JARVIS_MASTER_KEY"):
        env["JARVIS_MASTER_KEY"] = os.environ["JARVIS_MASTER_KEY"]
    return MCPServerSettings(
        transport="stdio",
        command=sys.executable,
        args=[
            "-m",
            "services.plugins.remote_worker",
            database,
            str(runtime_root),
            root.parent.name,
            name,
        ],
        env=env,
        read_timeout_seconds=60,
        ping_interval_seconds=0,
    )


def policy_summary(policy: dict | None, servers: dict) -> dict | None:
    if not policy:
        return None
    if policy["image"] == REMOTE_POLICY:
        return {
            "transport": "http",
            "authentication": "oauth",
            "endpoints": [approved_endpoint(s) for s in servers.values()],
            "adapter": "native_mcp",
            "network": "reviewed_atlassian_endpoint",
            "local_plugin_code": False,
            "revision": policy["revision"],
        }
    return {
        "image": policy["image"],
        "credential_slots": sorted(policy["credentials"]),
        "network": "none",
        "read_only": True,
        "revision": policy["revision"],
    }
