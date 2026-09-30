"""Persistent candidate inventory and truthful, content-bound activation.

SQLite lives in Jarvis's existing engine. Package files are disposable runtime
data. This service authorizes no executable components and owns no credentials.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from services.plugins.package import PluginPackage, PluginSkill, inspect_package

logger = logging.getLogger(__name__)
Record = dict[str, Any]
Approval = Callable[[Record], Awaitable[bool]]
Apply = Callable[[Path, PluginPackage, str], Awaitable[bool]]


class PluginStateError(ValueError):
    """Invalid activation state or missing candidate."""


class RuntimeUncertain(RuntimeError):
    """Runtime cleanup was not acknowledged; keep files and policy pinned."""


class ApprovalRejected(PluginStateError):
    """The user rejected this exact source or capability review."""


class PluginLifecycle:
    """One authenticated Jarvis tenant; no client-supplied owner identifiers."""

    def __init__(
        self,
        engine: Engine,
        root: Path,
        on_status: Callable[[Record], None] | None = None,
    ) -> None:
        self.engine = engine
        self.root = root
        self.on_status = on_status
        if root.is_symlink():
            raise PluginStateError("Runtime directory must not be a symlink")
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        root.chmod(0o700)
        locks = root / "locks"
        if locks.is_symlink():
            raise PluginStateError("Runtime lock directory must not be a symlink")
        locks.mkdir(mode=0o700, exist_ok=True)
        locks.chmod(0o700)
        with engine.begin() as db:
            db.execute(
                text("""CREATE TABLE IF NOT EXISTS plugin_candidates (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                status TEXT NOT NULL, touched REAL NOT NULL
            )""")
            )
            db.execute(
                text("""CREATE TABLE IF NOT EXISTS plugin_shared_candidates (
                candidate TEXT PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL,
                digest TEXT NOT NULL, policy_revision TEXT NOT NULL
            )""")
            )
            db.execute(
                text("""CREATE TABLE IF NOT EXISTS plugin_bindings (
                candidate TEXT NOT NULL, agent TEXT NOT NULL,
                status TEXT NOT NULL, PRIMARY KEY(candidate, agent)
            )""")
            )

    def package_path(self, identity: str) -> Path:
        try:
            normalized = str(uuid.UUID(identity))
        except (ValueError, TypeError, AttributeError) as exc:
            raise PluginStateError("Invalid candidate identifier") from exc
        if normalized != identity:
            raise PluginStateError("Invalid candidate identifier")
        return self.root / identity / "package"

    def operation_lock(self, identity: str) -> Path:
        """Fixed 256 shards keep lock inodes stable while deleting snapshots."""
        self.package_path(identity)
        return self.root / "locks" / (identity[:2] + ".lock")

    def stage(
        self, source: Path, *, repo: str, commit: str, subdirectory: str
    ) -> Record:
        """Snapshot one inspected package; neither install nor execute anything."""
        package = inspect_package(source)
        with (self.root / "stage.lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            for record in self.list():
                if (  # noqa: SIM102 — separate identity and immutable-byte checks
                    record["repo"],
                    record["commit"],
                    record["subdirectory"],
                    record["digest"],
                ) == (repo, commit, subdirectory, package.digest) and record[
                    "status"
                ] != "expired":
                    if (
                        self.package_path(record["id"]).is_dir()
                        and inspect_package(self.package_path(record["id"])).digest
                        == package.digest
                    ):
                        return record
            from services.plugins.resources import check_candidate_quota

            check_candidate_quota(len(self.list()))
            identity = str(uuid.uuid4())
            target = self.package_path(identity)
            try:
                shutil.copytree(source, target, symlinks=True)
                # The outer runtime root remains owner-only. Docker mounts this
                # exact immutable snapshot, whose files must be readable by the
                # unprivileged container UID. Never preserve executable bits.
                target.chmod(0o755)
                for path in target.rglob("*"):
                    if not path.is_symlink():
                        path.chmod(0o755 if path.is_dir() else 0o644)
                if inspect_package(target).digest != package.digest:
                    raise PluginStateError("Source changed while staging")
                payload = {
                    "id": identity,
                    "repo": repo,
                    "commit": commit,
                    "subdirectory": subdirectory,
                    **asdict(package),
                }
                with self.engine.begin() as db:
                    db.execute(
                        text(
                            "INSERT INTO plugin_candidates VALUES (:id, :payload, :status, :now)"
                        ),
                        {
                            "id": identity,
                            "payload": json.dumps(payload),
                            "status": "needs_approval",
                            "now": time.time(),
                        },
                    )
            except BaseException:
                shutil.rmtree(target.parent, ignore_errors=True)
                raise
            return self.get(identity)

    def get(self, identity: str) -> Record:
        self.package_path(identity)
        with self.engine.connect() as db:
            row = db.execute(
                text("SELECT payload, status FROM plugin_candidates WHERE id=:id"),
                {"id": identity},
            ).first()
            if row is None:
                raise PluginStateError("Candidate not found")
            bindings = db.execute(
                text("SELECT agent,status FROM plugin_bindings WHERE candidate=:id"),
                {"id": identity},
            ).all()
            shared = db.execute(
                text(
                    "SELECT enabled,digest,policy_revision FROM plugin_shared_candidates WHERE candidate=:id"
                ),
                {"id": identity},
            ).first()
        return {
            **json.loads(row[0]),
            "status": row[1],
            "global_enabled": bool(shared and shared[0]),
            "global_policy_revision": shared[2] if shared else "",
            "bindings": [{"agent": r[0], "status": r[1]} for r in bindings],
        }

    def list(self) -> list[Record]:
        with self.engine.connect() as db:
            identities = (
                db.execute(
                    text(
                        "SELECT id FROM plugin_candidates WHERE status != 'expired' ORDER BY touched DESC"
                    )
                )
                .scalars()
                .all()
            )
        return [self.get(identity) for identity in identities]

    def _status(self, identity: str, status: str, agent: str | None = None) -> Record:
        with self.engine.begin() as db:
            db.execute(
                text(
                    "UPDATE plugin_candidates SET status=:status,touched=:now WHERE id=:id"
                ),
                {"id": identity, "status": status, "now": time.time()},
            )
            if agent is not None:
                db.execute(
                    text("""INSERT INTO plugin_bindings VALUES (:id,:agent,:status)
                    ON CONFLICT(candidate,agent) DO UPDATE SET status=excluded.status"""),
                    {"id": identity, "agent": agent, "status": status},
                )
        record = self.get(identity)
        # Removing one destination must not hide another destination that is
        # still live. Per-binding deltas carry their own status separately.
        if status == "disabled" and any(
            item["status"] == "ready" for item in record["bindings"]
        ):
            with self.engine.begin() as db:
                db.execute(
                    text("UPDATE plugin_candidates SET status='ready' WHERE id=:id"),
                    {"id": identity},
                )
            record["status"] = "ready"
        # Consumers subscribe through the existing activity stream, not polling.
        from services.activity_stream import activity_stream_manager

        delta = {
            "id": identity,
            "status": record["status"],
            "binding_status": status,
            "agent": agent,
            "global_enabled": record["global_enabled"],
        }
        if self.on_status:
            self.on_status(delta)
        else:
            activity_stream_manager.broadcast(
                {
                    "event_type": "plugin_status",
                    "agent_name": agent or "",
                    "data": delta,
                }
            )
        return {**record, "agent": agent} if agent else record

    async def activate(
        self,
        identity: str,
        agent: str,
        *,
        approve: Approval,
        apply: Apply,
        authorize: Callable[[Path, PluginPackage], bool] | None = None,
    ) -> Record:
        """Keep a cross-process lock and require exact-content runtime acknowledgement."""
        if not isinstance(agent, str) or not agent.strip() or len(agent) > 128:
            raise PluginStateError("Invalid target agent")
        self.get(identity)
        lock_path = self.operation_lock(identity)
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PluginStateError("Plugin operation already in progress") from exc
            record = self.get(identity)
            package = await asyncio.to_thread(
                inspect_package, self.package_path(identity)
            )
            if package.digest != record["digest"]:
                self._status(identity, "integrity_failed", agent)
                raise PluginStateError("Package changed after inspection")
            namespace = hashlib.sha256(
                (agent + "\0" + package.name).encode()
            ).hexdigest()
            with (self.root / "locks" / ("namespace-" + namespace[:2] + ".lock")).open(
                "a"
            ) as namespace_lock:
                try:
                    fcntl.flock(namespace_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise PluginStateError(
                        "Plugin namespace operation already in progress"
                    ) from exc
                for existing in self.list():
                    if (
                        existing["id"] != identity
                        and existing["name"] == package.name
                        and any(
                            item["agent"] == agent
                            and item["status"]
                            in {
                                "ready",
                                "activating",
                                "deactivating",
                                "activation_interrupted",
                                "detach_failed",
                            }
                            for item in existing["bindings"]
                        )
                    ):
                        raise PluginStateError(
                            "Disable the previous plugin version before activating this one"
                        )
                if package.blockers and (
                    authorize is None
                    or await asyncio.to_thread(
                        authorize, self.package_path(identity), package
                    )
                    is not True
                ):
                    return self._status(identity, "unsupported", agent)
                try:
                    if not await approve(record):
                        return self._status(identity, "needs_approval", agent)
                except ApprovalRejected:
                    return self._status(identity, "rejected", agent)
                if (
                    await asyncio.to_thread(
                        inspect_package, self.package_path(identity)
                    )
                ).digest != record["digest"]:
                    self._status(identity, "integrity_failed", agent)
                    raise PluginStateError("Package changed during approval")
                self._status(identity, "activating", agent)
                try:
                    live = await apply(self.package_path(identity), package, agent)
                    if live is not True:
                        return self._status(identity, "activation_failed", agent)
                except RuntimeUncertain:
                    return self._status(identity, "activation_interrupted", agent)
                except asyncio.CancelledError:
                    self._status(identity, "activation_interrupted", agent)
                    raise
                except Exception:  # noqa: BLE001 — runtime boundary, redact secret-bearing exceptions
                    # Exception messages may contain credentials; don't expose them.
                    logger.warning(
                        "[plugins] Activation failed candidate=%s agent=%s",
                        identity,
                        agent,
                    )
                    return self._status(identity, "activation_interrupted", agent)
                return self._status(identity, "ready", agent)

    async def deactivate(self, identity: str, agent: str, *, remove: Apply) -> Record:
        """Release a binding only after the live runtime confirms removal."""
        record = self.get(identity)
        if not any(binding["agent"] == agent for binding in record["bindings"]):
            raise PluginStateError("Agent has no plugin binding")
        with self.operation_lock(identity).open("a") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PluginStateError("Plugin operation already in progress") from exc
            self._status(identity, "deactivating", agent)
            try:
                # Removal must target the aliases and manifests that were
                # acknowledged, even if disposable files disappeared/changed.
                package = PluginPackage(
                    record["name"],
                    record["version"],
                    record["ecosystem"],
                    record["digest"],
                    tuple(PluginSkill(**skill) for skill in record["skills"]),
                    record["servers"],
                    tuple(record["blockers"]),
                    record["license"],
                )
                removed = await remove(self.package_path(identity), package, agent)
            except asyncio.CancelledError:
                self._status(identity, "detach_failed", agent)
                raise
            except Exception:  # noqa: BLE001 — runtime boundary, redact secret-bearing exceptions
                logger.warning(
                    "[plugins] Removal failed candidate=%s agent=%s", identity, agent
                )
                removed = False
            return self._status(
                identity, "disabled" if removed is True else "detach_failed", agent
            )

    def uninstall(self, identity: str) -> Record:
        """Discard inactive runtime bytes and credentials; retain audit identity."""
        self.get(identity)
        parent = self.package_path(identity).parent
        with self.operation_lock(identity).open("a") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PluginStateError("Plugin operation already in progress") from exc
            record = self.get(identity)
            if record["global_enabled"]:
                raise PluginStateError("Stop global sharing before uninstalling")
            if any(item["status"] != "disabled" for item in record["bindings"]):
                raise PluginStateError(
                    "Disable and confirm every binding before uninstalling"
                )
            if parent.exists():
                shutil.rmtree(parent)
            from services.plugins.policy import PluginPolicyStore

            PluginPolicyStore(self.engine).delete(identity)
            with self.engine.begin() as db:
                db.execute(
                    text(
                        "UPDATE plugin_candidates SET status='expired',touched=:now WHERE id=:id"
                    ),
                    {"id": identity, "now": time.time()},
                )
            return self.get(identity)

    def cleanup(
        self, *, now: float | None = None, retention: float = 86400
    ) -> list[str]:
        """Delete only inactive, acknowledged packages; preserve registry audit.

        Uncertain bindings remain pinned. The host calls this on lifecycle
        events or its maintenance scheduler, never as a UI polling operation.
        """
        if retention < 0:
            raise PluginStateError("Invalid retention period")
        cutoff = (time.time() if now is None else now) - retention
        with self.engine.connect() as db:
            identities = (
                db.execute(
                    text("""SELECT id FROM plugin_candidates
                WHERE touched < :cutoff AND status != 'expired'"""),
                    {"cutoff": cutoff},
                )
                .scalars()
                .all()
            )
        removed: list[str] = []
        for identity in identities:
            parent = self.package_path(identity).parent
            with self.operation_lock(identity).open("a") as lock:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                # Recheck after locking: activation may have won the race.
                with self.engine.connect() as db:
                    row = db.execute(
                        text("SELECT touched FROM plugin_candidates WHERE id=:id"),
                        {"id": identity},
                    ).first()
                record = self.get(identity)
                if (
                    row is None
                    or row[0] >= cutoff
                    or record["global_enabled"]
                    or any(
                        binding["status"]
                        not in {
                            "disabled",
                            "needs_approval",
                            "rejected",
                            "unsupported",
                            "activation_failed",
                        }
                        for binding in record["bindings"]
                    )
                ):
                    continue
                # Locks live in fixed shards, outside disposable snapshots.
                try:
                    shutil.rmtree(parent)
                except FileNotFoundError:
                    pass
                # Filesystem maintenance runs in a worker. Publish on the
                # owning event loop in the caller, not into asyncio.Queue here.
                with self.engine.begin() as db:
                    db.execute(
                        text(
                            "UPDATE plugin_candidates SET status='expired',touched=:now WHERE id=:id"
                        ),
                        {"id": identity, "now": time.time()},
                    )
                from services.plugins.policy import PluginPolicyStore

                PluginPolicyStore(self.engine).delete(identity)
                removed.append(identity)
        return removed
