"""Persistent candidate inventory and truthful, content-bound activation.

SQLite lives in Jarvis's existing engine. Package files are disposable runtime
data. This service authorizes no executable components and owns no credentials.
"""
from __future__ import annotations

import asyncio
import fcntl
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

from services.plugins.package import PluginPackage, inspect_package

logger = logging.getLogger(__name__)
Record = dict[str, Any]
Approval = Callable[[Record], Awaitable[bool]]
Apply = Callable[[Path, PluginPackage, str], Awaitable[bool]]


class PluginStateError(ValueError):
    """Invalid activation state or missing candidate."""


class PluginLifecycle:
    """One authenticated Jarvis tenant; no client-supplied owner identifiers."""

    def __init__(self, engine: Engine, root: Path) -> None:
        self.engine = engine
        self.root = root
        if root.is_symlink():
            raise PluginStateError("Runtime directory must not be a symlink")
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        root.chmod(0o700)
        with engine.begin() as db:
            db.execute(text("""CREATE TABLE IF NOT EXISTS plugin_candidates (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                status TEXT NOT NULL, touched REAL NOT NULL
            )"""))
            db.execute(text("""CREATE TABLE IF NOT EXISTS plugin_bindings (
                candidate TEXT NOT NULL, agent TEXT NOT NULL,
                status TEXT NOT NULL, PRIMARY KEY(candidate, agent)
            )"""))

    def package_path(self, identity: str) -> Path:
        try:
            normalized = str(uuid.UUID(identity))
        except (ValueError, TypeError, AttributeError) as exc:
            raise PluginStateError("Invalid candidate identifier") from exc
        if normalized != identity:
            raise PluginStateError("Invalid candidate identifier")
        return self.root / identity / "package"

    def stage(self, source: Path, *, repo: str, commit: str, subdirectory: str) -> Record:
        """Snapshot one inspected package; neither install nor execute anything."""
        package = inspect_package(source)
        identity = str(uuid.uuid4())
        target = self.package_path(identity)
        try:
            shutil.copytree(source, target, symlinks=True)
            if inspect_package(target).digest != package.digest:
                raise PluginStateError("Source changed while staging")
            payload = {"id": identity, "repo": repo, "commit": commit,
                       "subdirectory": subdirectory, **asdict(package)}
            with self.engine.begin() as db:
                db.execute(text("INSERT INTO plugin_candidates VALUES (:id, :payload, :status, :now)"),
                           {"id": identity, "payload": json.dumps(payload),
                            "status": "needs_approval", "now": time.time()})
        except BaseException:
            shutil.rmtree(target.parent, ignore_errors=True)
            raise
        return self.get(identity)

    def get(self, identity: str) -> Record:
        self.package_path(identity)
        with self.engine.connect() as db:
            row = db.execute(text("SELECT payload, status FROM plugin_candidates WHERE id=:id"),
                             {"id": identity}).first()
            if row is None:
                raise PluginStateError("Candidate not found")
            bindings = db.execute(text("SELECT agent,status FROM plugin_bindings WHERE candidate=:id"),
                                  {"id": identity}).all()
        return {**json.loads(row[0]), "status": row[1],
                "bindings": [{"agent": r[0], "status": r[1]} for r in bindings]}

    def list(self) -> list[Record]:
        with self.engine.connect() as db:
            identities = db.execute(text("SELECT id FROM plugin_candidates ORDER BY touched DESC")).scalars().all()
        return [self.get(identity) for identity in identities]

    def _status(self, identity: str, status: str, agent: str | None = None) -> Record:
        with self.engine.begin() as db:
            db.execute(text("UPDATE plugin_candidates SET status=:status,touched=:now WHERE id=:id"),
                       {"id": identity, "status": status, "now": time.time()})
            if agent is not None:
                db.execute(text("""INSERT INTO plugin_bindings VALUES (:id,:agent,:status)
                    ON CONFLICT(candidate,agent) DO UPDATE SET status=excluded.status"""),
                           {"id": identity, "agent": agent, "status": status})
        record = self.get(identity)
        # Consumers subscribe through the existing activity stream, not polling.
        from services.activity_stream import activity_stream_manager

        activity_stream_manager.broadcast({"event_type": "plugin_status", "agent_name": agent or "",
                                           "data": {"id": identity, "status": status, "agent": agent}})
        return {**record, "agent": agent} if agent else record

    async def activate(self, identity: str, agent: str, *, approve: Approval, apply: Apply) -> Record:
        """Keep a cross-process lock and require exact-content runtime acknowledgement."""
        if not isinstance(agent, str) or not agent.strip() or len(agent) > 128:
            raise PluginStateError("Invalid target agent")
        self.get(identity)
        lock_path = self.package_path(identity).parent / "operation.lock"
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PluginStateError("Plugin operation already in progress") from exc
            record = self.get(identity)
            package = await asyncio.to_thread(inspect_package, self.package_path(identity))
            if package.digest != record["digest"]:
                self._status(identity, "integrity_failed", agent)
                raise PluginStateError("Package changed after inspection")
            if package.blockers:
                return self._status(identity, "unsupported", agent)
            if not await approve(record):
                return self._status(identity, "needs_approval", agent)
            if (await asyncio.to_thread(inspect_package, self.package_path(identity))).digest != record["digest"]:
                self._status(identity, "integrity_failed", agent)
                raise PluginStateError("Package changed during approval")
            self._status(identity, "activating", agent)
            try:
                live = await apply(self.package_path(identity), package, agent)
                if live is not True:
                    return self._status(identity, "activation_failed", agent)
            except asyncio.CancelledError:
                self._status(identity, "activation_interrupted", agent)
                raise
            except Exception:  # noqa: BLE001 — runtime boundary, redact secret-bearing exceptions
                # Exception messages may contain credentials; don't expose them.
                logger.warning("[plugins] Activation failed candidate=%s agent=%s", identity, agent)
                return self._status(identity, "activation_failed", agent)
            return self._status(identity, "ready", agent)

    async def deactivate(self, identity: str, agent: str, *, remove: Apply) -> Record:
        """Release a binding only after the live runtime confirms removal."""
        record = self.get(identity)
        if not any(binding["agent"] == agent for binding in record["bindings"]):
            raise PluginStateError("Agent has no plugin binding")
        with (self.package_path(identity).parent / "operation.lock").open("a") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PluginStateError("Plugin operation already in progress") from exc
            self._status(identity, "deactivating", agent)
            try:
                package = await asyncio.to_thread(inspect_package, self.package_path(identity))
                removed = await remove(self.package_path(identity), package, agent)
            except asyncio.CancelledError:
                self._status(identity, "detach_failed", agent)
                raise
            except Exception:  # noqa: BLE001 — runtime boundary, redact secret-bearing exceptions
                logger.warning("[plugins] Removal failed candidate=%s agent=%s", identity, agent)
                removed = False
            return self._status(identity, "disabled" if removed is True else "detach_failed", agent)

    def cleanup(self, *, now: float | None = None, retention: float = 86400) -> list[str]:
        """Delete only inactive, acknowledged packages; preserve registry audit.

        Uncertain bindings remain pinned. The host calls this on lifecycle
        events or its maintenance scheduler, never as a UI polling operation.
        """
        if retention < 0:
            raise PluginStateError("Invalid retention period")
        cutoff = (time.time() if now is None else now) - retention
        with self.engine.connect() as db:
            identities = db.execute(text("""SELECT id FROM plugin_candidates
                WHERE touched < :cutoff AND status != 'expired'"""), {"cutoff": cutoff}).scalars().all()
        removed: list[str] = []
        for identity in identities:
            parent = self.package_path(identity).parent
            if not parent.exists():
                continue
            with (parent / "operation.lock").open("a") as lock:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                # Recheck after locking: activation may have won the race.
                with self.engine.connect() as db:
                    row = db.execute(text("SELECT touched FROM plugin_candidates WHERE id=:id"), {"id": identity}).first()
                record = self.get(identity)
                if row is None or row[0] >= cutoff or any(
                    binding["status"] != "disabled" for binding in record["bindings"]
                ):
                    continue
                # Keep the lock directory to avoid unlink/recreate lock races.
                try:
                    shutil.rmtree(self.package_path(identity))
                except FileNotFoundError:
                    pass
                # Filesystem maintenance runs in a worker. Publish on the
                # owning event loop in the caller, not into asyncio.Queue here.
                with self.engine.begin() as db:
                    db.execute(text("UPDATE plugin_candidates SET status='expired',touched=:now WHERE id=:id"),
                               {"id": identity, "now": time.time()})
                removed.append(identity)
        return removed
