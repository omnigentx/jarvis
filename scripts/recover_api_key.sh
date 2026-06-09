#!/usr/bin/env bash
# recover_api_key.sh — print the web/API key (the login "password") for THIS
# install when you've forgotten it.
#
# The key is stored ENCRYPTED in backend/data/jarvis.db
# (system_config: auth / JARVIS_API_KEY) and is decrypted with JARVIS_MASTER_KEY
# (from backend/.env). The UI deliberately never shows the key — you can only
# CHANGE it (Settings → General → Change API Key). This script is the recovery
# path: it decrypts and prints the current key locally; nothing leaves the host.
#
# Usage:
#     scripts/recover_api_key.sh           # prints the key
#     KEY=$(scripts/recover_api_key.sh)    # capture it (key → stdout only)
#
# Prefer to SET a new known key instead of recovering the old one? Use
# Settings → General → Change API Key in the web UI.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND="$ROOT/backend"
DB="$BACKEND/data/jarvis.db"

[[ -f "$DB" ]] || { echo "No jarvis.db at $DB — run Setup first." >&2; exit 1; }
command -v uv >/dev/null || { echo "uv not found on PATH (needed to decrypt)." >&2; exit 1; }

# JARVIS_MASTER_KEY decrypts every stored secret. Prefer an already-exported
# value; otherwise read it from backend/.env (cut -d= -f2- keeps '=' in the
# value intact).
if [[ -z "${JARVIS_MASTER_KEY:-}" && -f "$BACKEND/.env" ]]; then
    JARVIS_MASTER_KEY="$(grep -E '^JARVIS_MASTER_KEY=' "$BACKEND/.env" | head -1 | cut -d= -f2-)"
    export JARVIS_MASTER_KEY
fi
[[ -n "${JARVIS_MASTER_KEY:-}" ]] || {
    echo "JARVIS_MASTER_KEY not found (checked env + backend/.env) — cannot decrypt." >&2
    exit 1
}

# Decrypt via the same code path the app uses. The key goes to stdout; all
# human-facing text goes to stderr so the output stays pipeable.
echo "Recovering API key from $DB ..." >&2
KEY="$(cd "$BACKEND" && uv run python -c \
    "from services.config_service import config_service; print(config_service.get('auth','JARVIS_API_KEY') or '')")"

if [[ -z "$KEY" ]]; then
    echo "No API key is set (auth is open / pre-Setup), or decryption returned empty." >&2
    exit 1
fi

echo "Paste this into the login screen's API key field:" >&2
printf '%s\n' "$KEY"
