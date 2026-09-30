"""Human-controlled, package-scoped sandbox policy and encrypted credentials."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from core.secrets_crypto import decrypt, encrypt
from services.plugins.package import PackageError, PluginPackage
from services.plugins.sandbox import sandbox_settings


class PluginPolicyStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        with engine.begin() as db:
            db.execute(
                text("""CREATE TABLE IF NOT EXISTS plugin_execution_policies (
                candidate TEXT PRIMARY KEY, image TEXT NOT NULL, credentials TEXT NOT NULL,
                revision TEXT NOT NULL DEFAULT ''
            )""")
            )
            columns = {
                row[1]
                for row in db.execute(
                    text("PRAGMA table_info(plugin_execution_policies)")
                )
            }
            if "revision" not in columns:
                db.execute(
                    text(
                        "ALTER TABLE plugin_execution_policies ADD COLUMN revision TEXT NOT NULL DEFAULT ''"
                    )
                )

    def put(self, candidate: str, image: str, credentials: dict[str, str]) -> None:
        # Encrypt the whole map; neither approval payload nor inventory needs
        # secret values. Missing master key fails closed before persistence.
        encoded = encrypt(json.dumps(credentials)) if credentials else ""
        with self.engine.begin() as db:
            db.execute(
                text("""INSERT INTO plugin_execution_policies (candidate,image,credentials,revision) VALUES (:id,:image,:credentials,:revision)
                ON CONFLICT(candidate) DO UPDATE SET image=excluded.image,credentials=excluded.credentials,revision=excluded.revision"""),
                {
                    "id": candidate,
                    "image": image,
                    "credentials": encoded,
                    "revision": str(uuid.uuid4()),
                },
            )

    def get(self, candidate: str) -> dict[str, Any] | None:
        with self.engine.connect() as db:
            row = db.execute(
                text(
                    "SELECT image,credentials,revision FROM plugin_execution_policies WHERE candidate=:id"
                ),
                {"id": candidate},
            ).first()
        if not row:
            return None
        return {
            "image": row[0],
            "credentials": json.loads(decrypt(row[1])) if row[1] else {},
            "revision": row[2],
        }

    def delete(self, candidate: str) -> None:
        with self.engine.begin() as db:
            db.execute(
                text("DELETE FROM plugin_execution_policies WHERE candidate=:id"),
                {"id": candidate},
            )


def validate_policy(root: Path, package: PluginPackage, policy: dict[str, Any]) -> bool:
    """Support isolated stdio MCP, retaining all other compatibility blockers."""
    allowed = {"mcp_requires_policy_review"}
    # Executable MCP code is contained in OCI; executable skill resources are
    # not offered to the host's trusted shell through this adapter.
    if not package.skills and package.servers:
        allowed.add("executable_content")
    if set(package.blockers) - allowed or not package.servers:
        raise PackageError("This plugin needs an unsupported runtime adapter")
    for config in package.servers.values():
        sandbox_settings(
            root, config, policy["image"], credentials=policy["credentials"]
        )
    return True
