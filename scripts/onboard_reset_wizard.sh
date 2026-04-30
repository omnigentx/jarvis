#!/usr/bin/env bash
# onboard_reset_wizard.sh — wipe just the wizard-specific state so you can
# walk through /#/setup again without losing .env or fastagent.secrets.yaml.
#
# What it clears in jarvis.db:
#     setup_wizard   → all rows (wizard steps return to "pending")
#     system_config  → all rows with source='wizard' (keeps anything written
#                      directly by the app or imported outside the wizard)
#
# What it does NOT touch:
#     backend/.env
#     backend/fastagent.secrets.yaml
#     backend/fastagent.secrets.docker.yaml
#     Any non-wizard rows in system_config
#
# Tip: run `scripts/onboard_snapshot.sh` once before the first reset so you
# have a safety net.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DB="$ROOT/backend/data/jarvis.db"

if [[ ! -f "$DB" ]]; then
    echo "No jarvis.db at $DB — nothing to reset." >&2
    exit 0
fi

if ! command -v sqlite3 >/dev/null; then
    echo "sqlite3 not found on PATH.  Install it or reset via the UI instead." >&2
    exit 1
fi

echo "Resetting wizard state in: $DB"
echo ""

WIZARD_BEFORE=$(sqlite3 "$DB" "SELECT COUNT(*) FROM setup_wizard;" 2>/dev/null || echo 0)
CFG_BEFORE=$(sqlite3 "$DB" "SELECT COUNT(*) FROM system_config WHERE source='wizard';" 2>/dev/null || echo 0)

sqlite3 "$DB" <<SQL
BEGIN;
DELETE FROM setup_wizard;
DELETE FROM system_config WHERE source='wizard';
COMMIT;
SQL

echo "  ✓ cleared $WIZARD_BEFORE row(s) from setup_wizard"
echo "  ✓ cleared $CFG_BEFORE row(s) from system_config (source='wizard')"
echo ""
echo "Restart the backend (or hit /api/settings/restart) so the setup-gate"
echo "middleware drops its cache, then reload the dashboard.  /#/setup"
echo "should now be the active route."
