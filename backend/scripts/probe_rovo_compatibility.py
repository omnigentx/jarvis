"""Inspect the exact reported vendor package, without installation/execution.

Exit 2 means compatibility is blocked. Exit 0 proves inspection only, never
OAuth, external tool use or functional acceptance. No vendor files are retained.
Run from backend with PYTHONPATH=.: uv run python scripts/probe_rovo_compatibility.py
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from services.plugins.archive import download_package
from services.plugins.package import PackageError, inspect_package
from services.plugins.policy import validate_policy

REPOSITORY = "openai/plugins"
COMMIT = "5fd93af4cd0c623e020d0cc7e9ce178b4ac1f70f"
SUBDIRECTORY = "plugins/atlassian-rovo"


async def probe() -> tuple[dict, int]:
    with tempfile.TemporaryDirectory(prefix="jarvis-rovo-compatibility-") as directory:
        root = await download_package(
            REPOSITORY, COMMIT, SUBDIRECTORY, Path(directory) / "package", {REPOSITORY}
        )
        package = inspect_package(root)
        policy_result = "inspection accepted"
        policy_blocked = False
        try:
            # Syntactic profile only: never starts Docker or grants permissions.
            validate_policy(root, package, {"image": "sha256:" + "a" * 64, "credentials": {}})
        except PackageError as exc:
            policy_blocked = True
            policy_result = str(exc)
        report = {
            "repo": REPOSITORY,
            "commit": COMMIT,
            "subdirectory": SUBDIRECTORY,
            "name": package.name,
            "version": package.version,
            "digest": package.digest,
            "skills": len(package.skills),
            "transports": {name: server["transport"] for name, server in package.servers.items()},
            "blockers": list(package.blockers),
            "policy_validation": policy_result,
            "plugin_executed": False,
            "authorization_test": "NOT RUN",
            "external_tool_call": "NOT RUN",
            "functional_acceptance": "NOT PASSED",
        }
        return report, 2 if package.blockers or policy_blocked else 0


if __name__ == "__main__":
    try:
        result, code = asyncio.run(probe())
    except Exception as exc:  # CLI boundary: do not print credential-bearing responses.
        result, code = {"inspection": "FAILED", "error_type": type(exc).__name__}, 1
    print(json.dumps(result, indent=2))
    raise SystemExit(code)
