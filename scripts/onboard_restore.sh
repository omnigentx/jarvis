#!/usr/bin/env bash
# onboard_restore.sh — restore a snapshot created by onboard_snapshot.sh.
#
# Usage:
#     bash scripts/onboard_restore.sh .onboard-backups/<timestamp>
#
# Restores:
#     backend/.env
#     backend/fastagent.secrets.yaml
#     backend/fastagent.secrets.docker.yaml  (if present in snapshot)
#     backend/data/jarvis.db
#
# Existing files are moved aside to *.pre-restore.<timestamp> before being
# overwritten so nothing is lost if the snapshot turns out to be wrong.
set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <snapshot-dir>" >&2
    exit 2
fi

SNAP="$1"
if [[ ! -d "$SNAP" ]]; then
    echo "Snapshot directory not found: $SNAP" >&2
    exit 1
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAMP="$(date +%Y-%m-%d_%H%M%S)"

restore_one() {
    local src="$1"
    local dest="$2"
    if [[ ! -e "$src" ]]; then
        echo "  ⏭  $(basename "$src") not in snapshot — skipped"
        return
    fi
    if [[ -e "$dest" ]]; then
        local backup="${dest}.pre-restore.${STAMP}"
        mv "$dest" "$backup"
        echo "  ↪  saved current $(basename "$dest") → $(basename "$backup")"
    fi
    mkdir -p "$(dirname "$dest")"
    cp "$src" "$dest"
    echo "  ✓ restored $(basename "$dest")"
}

echo "Restoring from: $SNAP"
restore_one "$SNAP/.env" "$ROOT/backend/.env"
restore_one "$SNAP/fastagent.secrets.yaml" "$ROOT/backend/fastagent.secrets.yaml"
restore_one "$SNAP/fastagent.secrets.docker.yaml" "$ROOT/backend/fastagent.secrets.docker.yaml"
restore_one "$SNAP/jarvis.db" "$ROOT/backend/data/jarvis.db"

echo ""
echo "Restore complete."
echo "  Previous files (if any) preserved with suffix .pre-restore.$STAMP"
echo ""
echo "Next steps:"
echo "  1. Restart the backend so it re-reads env + DB."
echo "  2. Reload the dashboard; the wizard should pick up where you left off."
