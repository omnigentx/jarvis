"""Runtime-only package acquisition; source approval precedes network access."""

from __future__ import annotations

import asyncio
import fcntl
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

from services.plugins.archive import download_package, validate_source
from services.plugins.lifecycle import (
    Approval,
    ApprovalRejected,
    PluginLifecycle,
    Record,
)

DOWNLOAD_TIMEOUT = 180

Download = Callable[[str, str, str, Path], Awaitable[Path | None]]


class PluginInstaller:
    def __init__(self, lifecycle: PluginLifecycle) -> None:
        self.lifecycle = lifecycle
        self._downloads = asyncio.Semaphore(4)

    async def install(
        self,
        repo: str,
        commit: str,
        subdirectory: str,
        *,
        approve: Approval,
        download: Download | None = None,
    ) -> Record:
        """Approve one exact source snapshot; catalog labels confer no trust."""
        validate_source(repo, commit, subdirectory, {repo})
        source = {"repo": repo, "commit": commit, "subdirectory": subdirectory}
        try:
            if not await approve(source):
                return {**source, "status": "needs_source_approval"}
        except ApprovalRejected:
            return {**source, "status": "rejected"}
        if download is None:

            async def download(
                repo: str, commit: str, subdirectory: str, destination: Path
            ) -> Path:
                return await download_package(
                    repo, commit, subdirectory, destination, {repo}
                )

        with tempfile.TemporaryDirectory(
            prefix="download-", dir=self.lifecycle.root
        ) as temp:
            lease = (Path(temp) / ".lease").open("a")
            fcntl.flock(lease.fileno(), fcntl.LOCK_EX)
            destination = Path(temp) / "package"
            try:
                async with self._downloads, asyncio.timeout(DOWNLOAD_TIMEOUT):
                    await download(repo, commit, subdirectory, destination)
                    result = await asyncio.to_thread(
                        self.lifecycle.stage,
                        destination,
                        repo=repo,
                        commit=commit,
                        subdirectory=subdirectory,
                    )
            finally:
                lease.close()
        # Opportunistic cleanup is driven by a user operation, not polling.
        expired = await asyncio.to_thread(self.lifecycle.cleanup)
        from services.activity_stream import activity_stream_manager

        for identity in expired:
            activity_stream_manager.broadcast(
                {
                    "event_type": "plugin_status",
                    "agent_name": "",
                    "data": {"id": identity, "status": "expired"},
                }
            )
        return result


async def approve_source(record: Record, *, requested_by: str = "Jarvis") -> bool:
    """Reuse the human review gate for exact repository/commit/path identity."""
    import json

    from services.approval_gate import request_approval

    allowed, _reason = request_approval(
        approval_type="plugin_source",
        agent_name=requested_by,
        pause=False,
        scope_key="plugin-source:"
        + record["repo"]
        + ":"
        + record["commit"]
        + ":"
        + record["subdirectory"],
        title="Review plugin download source",
        content_md="Download metadata and inspect files from this exact source. "
        "This does not authorize execution or activation.\n\n```json\n"
        + json.dumps(record, ensure_ascii=True, sort_keys=True)
        + "\n```",
    )
    if not allowed and "reject" in _reason:
        raise ApprovalRejected("The user rejected this exact plugin review")
    return allowed


async def approve_content(record: Record) -> bool:
    """Bind content review to digest, destination and supported capabilities."""
    import json

    from services.approval_gate import request_approval

    review = {
        key: record[key]
        for key in (
            "repo",
            "commit",
            "subdirectory",
            "name",
            "digest",
            "skills",
            "blockers",
            "target_agent",
        )
    }
    review["execution_policy"] = record.get("execution_policy")
    if record.get("target_label"):
        review["target_label"] = record["target_label"]
    allowed, _reason = request_approval(
        approval_type="plugin_content",
        agent_name=record.get("requested_by", "Jarvis"),
        pause=False,
        scope_key="plugin:"
        + record["id"]
        + ":"
        + record["target_agent"]
        + ":"
        + record["digest"],
        title="Review plugin capability activation",
        content_md="Third-party skill instructions can influence the agent and its existing tools. "
        "Review source content before approving. Host shell privileges are unchanged. "
        "MCP execution, when declared, uses the displayed host-controlled sandbox policy.\n\n```json\n"
        + json.dumps(review, ensure_ascii=True, sort_keys=True)
        + "\n```",
    )
    if not allowed and "reject" in _reason:
        raise ApprovalRejected("The user rejected this exact plugin review")
    return allowed
