"""Separate human-reviewed global sharing; per-runtime ACKs remain authoritative."""

from __future__ import annotations

import asyncio
import fcntl

from sqlalchemy import text

from services.plugins.lifecycle import ApprovalRejected, PluginStateError
from services.plugins.package import inspect_package
from services.plugins.policy import PluginPolicyStore, validate_policy


def stop_sharing(lifecycle, identity: str) -> None:
    lifecycle.get(identity)
    with (lifecycle.root / "sharing.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PluginStateError(
                "Plugin sharing operation already in progress"
            ) from exc
        with lifecycle.engine.begin() as db:
            db.execute(
                text(
                    "UPDATE plugin_shared_candidates SET enabled=0 WHERE candidate=:id"
                ),
                {"id": identity},
            )
        lifecycle._status(identity, lifecycle.get(identity)["status"])


async def promote(lifecycle, identity: str, targets: list[str], *, approve, apply):
    """Share only this exact package/policy, including with future runtimes.

    A separate global review is required even if an individual binding exists.
    Partial failures stay visible. Stopping sharing does not remove existing
    bindings; those need their own acknowledged Disable operation.
    """
    lifecycle.get(identity)
    root = lifecycle.package_path(identity)
    with (lifecycle.root / "sharing.lock").open("a") as global_lock:
        try:
            fcntl.flock(global_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PluginStateError(
                "Plugin sharing operation already in progress"
            ) from exc
        with lifecycle.operation_lock(identity).open("a") as candidate_lock:
            try:
                fcntl.flock(candidate_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PluginStateError("Plugin operation already in progress") from exc
            record = lifecycle.get(identity)
            package = await asyncio.to_thread(inspect_package, root)
            if package.digest != record["digest"]:
                raise PluginStateError("Package changed before global review")
            policy = PluginPolicyStore(lifecycle.engine).get(identity)
            if package.blockers and (
                policy is None or not validate_policy(root, package, policy)
            ):
                raise PluginStateError(
                    "This package cannot be shared with its current policy"
                )
            reviewed_policy = (
                {
                    "image": policy["image"],
                    "credential_slots": sorted(policy["credentials"]),
                    "revision": policy["revision"],
                    "network": "none",
                    "read_only": True,
                }
                if policy
                else None
            )
            review = {
                **record,
                "target_agent": "global:all-current-and-future-agents",
                "execution_policy": reviewed_policy,
            }
            try:
                if not await approve(review):
                    return {**record, "status": "needs_global_approval"}
            except ApprovalRejected:
                return {**record, "status": "rejected"}
            if (await asyncio.to_thread(inspect_package, root)).digest != record[
                "digest"
            ]:
                raise PluginStateError("Package changed during global review")
            with lifecycle.engine.begin() as db:
                other = db.execute(
                    text(
                        "SELECT candidate FROM plugin_shared_candidates WHERE enabled=1 AND name=:name AND candidate!=:id"
                    ),
                    {"name": package.name, "id": identity},
                ).first()
                if other:
                    raise PluginStateError(
                        "Stop sharing the previous version before promoting another version"
                    )
                db.execute(
                    text("""INSERT INTO plugin_shared_candidates(candidate,name,enabled,digest,policy_revision)
                    VALUES (:id,:name,1,:digest,:revision) ON CONFLICT(candidate) DO UPDATE SET enabled=1,digest=excluded.digest,policy_revision=excluded.policy_revision"""),
                    {
                        "id": identity,
                        "name": package.name,
                        "digest": record["digest"],
                        "revision": policy["revision"] if policy else "",
                    },
                )
        lifecycle._status(identity, record["status"])
        failed = False
        for target in dict.fromkeys(targets):

            async def approved(_record):
                return True

            try:
                result = await lifecycle.activate(
                    identity,
                    target,
                    approve=approved,
                    apply=apply,
                    authorize=lambda root, package: (
                        policy is not None and validate_policy(root, package, policy)
                    ),
                )
                failed |= (
                    next(
                        item["status"]
                        for item in result["bindings"]
                        if item["agent"] == target
                    )
                    != "ready"
                )
            except PluginStateError:
                lifecycle._status(identity, "activation_failed", target)
                failed = True
        if failed:
            lifecycle._status(identity, "activation_failed")
        return lifecycle.get(identity)
