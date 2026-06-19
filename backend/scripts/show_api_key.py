"""Print the effective ``JARVIS_API_KEY`` — the auth key the Setup wizard and
every HTTP request check — so an operator who forgot it can recover it without
hand-querying SQLite.

This is the recovery tool. It is NOT ``rotate_master_key.py``: that one rotates
the *encryption* master (``JARVIS_MASTER_KEY``) and re-encrypts stored secrets;
it never reveals the auth key.

Resolution mirrors the backend boot path exactly (``core/auth.py``:
``load_dotenv()`` → ``os.getenv("JARVIS_API_KEY")``, and if that is empty the
value hydrated from ``system_config[auth/JARVIS_API_KEY]``). Whatever this
prints is therefore the same value ``verify_api_key`` compares against and the
wizard expects — single source of truth, no divergence.

Usage (from the ``backend/`` directory)::

    uv run scripts/show_api_key.py

Diagnostics go to stderr and the bare key is the only thing on stdout, so it
pipes cleanly::

    uv run scripts/show_api_key.py | pbcopy   # copy straight to clipboard
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_BACKEND = Path(__file__).resolve().parents[1]


def main() -> int:
    # Same .env the backend loads, regardless of the caller's CWD.
    load_dotenv(_BACKEND / ".env")

    key = os.getenv("JARVIS_API_KEY", "").strip()
    source = "environment / .env (JARVIS_API_KEY)"

    if not key:
        # Fall back to the persisted value, the way boot hydration does.
        sys.path.insert(0, str(_BACKEND))
        from core.database import SessionLocal, SystemConfig

        with SessionLocal() as db:
            row = (
                db.query(SystemConfig)
                .filter(
                    SystemConfig.category == "auth",
                    SystemConfig.key == "JARVIS_API_KEY",
                )
                .first()
            )
        key = (row.value or "").strip() if row else ""
        source = "system_config DB (auth/JARVIS_API_KEY)"

    if not key:
        print(
            "No JARVIS_API_KEY is configured yet — auth is currently open and "
            "the Setup wizard will generate one on first run.",
            file=sys.stderr,
        )
        return 1

    print("# JARVIS_API_KEY — the AUTH key (paste into the Setup wizard, or", file=sys.stderr)
    print("#                  use as the 'Authorization: Bearer <key>' token).", file=sys.stderr)
    print(f"# source: {source}", file=sys.stderr)
    print(key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
