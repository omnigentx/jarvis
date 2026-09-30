"""Runtime-only package acquisition; source approval precedes network access."""
from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

from services.plugins.archive import download_package, validate_source
from services.plugins.lifecycle import Approval, PluginLifecycle, Record

Download = Callable[[str, str, str, Path], Awaitable[Path | None]]


class PluginInstaller:
    def __init__(self, lifecycle: PluginLifecycle) -> None:
        self.lifecycle = lifecycle

    async def install(
        self, repo: str, commit: str, subdirectory: str, *,
        approve: Approval, download: Download | None = None,
    ) -> Record:
        """Approve one exact source snapshot; catalog labels confer no trust."""
        validate_source(repo, commit, subdirectory, {repo})
        source = {"repo": repo, "commit": commit, "subdirectory": subdirectory}
        if not await approve(source):
            return {**source, "status": "needs_source_approval"}
        if download is None:
            async def download(repo: str, commit: str, subdirectory: str, destination: Path) -> Path:
                return await download_package(repo, commit, subdirectory, destination, {repo})
        with tempfile.TemporaryDirectory(prefix="download-", dir=self.lifecycle.root) as temp:
            destination = Path(temp) / "package"
            await download(repo, commit, subdirectory, destination)
            result = await asyncio.to_thread(
                self.lifecycle.stage, destination, repo=repo, commit=commit,
                subdirectory=subdirectory,
            )
        # Opportunistic cleanup is driven by a user operation, not polling.
        expired = await asyncio.to_thread(self.lifecycle.cleanup)
        from services.activity_stream import activity_stream_manager

        for identity in expired:
            activity_stream_manager.broadcast({"event_type": "plugin_status", "agent_name": "",
                                               "data": {"id": identity, "status": "expired"}})
        return result


async def approve_source(record: Record) -> bool:
    """Reuse the human review gate for exact repository/commit/path identity."""
    import json

    from services.approval_gate import request_approval

    allowed, _reason = request_approval(
        approval_type="plugin_source", scope_key="plugin-source:" + record["repo"],
        title="Review plugin download source",
        content_md="Download metadata and inspect files from this exact source. "
        "This does not authorize execution or activation.\n\n```json\n"
        + json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n```",
    )
    return allowed


async def approve_content(record: Record) -> bool:
    """Bind content review to digest, destination and supported capabilities."""
    import json

    from services.approval_gate import request_approval

    review = {key: record[key] for key in (
        "repo", "commit", "subdirectory", "name", "digest", "skills", "blockers", "target_agent"
    )}
    allowed, _reason = request_approval(
        approval_type="plugin_content", scope_key="plugin:" + record["id"],
        title="Review plugin skill activation",
        content_md="Third-party skill instructions can influence the agent and its existing tools. "
        "Review source content before approving. No new shell or executable permission is granted.\n\n```json\n"
        + json.dumps(review, ensure_ascii=True, sort_keys=True) + "\n```",
    )
    return allowed
