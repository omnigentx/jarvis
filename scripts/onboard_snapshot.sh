#!/usr/bin/env bash
# onboard_snapshot.sh — freeze every piece of state the Setup Wizard touches,
# plus a human-readable cheat-sheet you can paste from while running the
# wizard in a browser.
#
# Produces: .onboard-backups/<timestamp>/
#     .env                     (copy of backend/.env)
#     fastagent.secrets.yaml   (copy of backend/fastagent.secrets.yaml)
#     jarvis.db                (copy of backend/data/jarvis.db)
#     system_config.csv        (exported plaintext config rows, if any)
#     setup_wizard.csv         (wizard step state)
#     ONBOARD_CHEATSHEET.md    (values to paste into wizard, grouped by step)
#
# Run from the repo root:
#     bash scripts/onboard_snapshot.sh
#
# Restore with:
#     bash scripts/onboard_restore.sh .onboard-backups/<timestamp>
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAMP="$(date +%Y-%m-%d_%H%M%S)"
OUT="$ROOT/.onboard-backups/$STAMP"

mkdir -p "$OUT"

copy_if_exists() {
    local src="$1"
    local dest="$2"
    if [[ -e "$src" ]]; then
        cp "$src" "$dest"
        echo "  ✓ copied $(basename "$src")"
    else
        echo "  ⏭  $(basename "$src") missing — skipped"
    fi
}

echo "Snapshot → $OUT"
copy_if_exists "$ROOT/backend/.env" "$OUT/.env"
copy_if_exists "$ROOT/backend/fastagent.secrets.yaml" "$OUT/fastagent.secrets.yaml"
copy_if_exists "$ROOT/backend/fastagent.secrets.docker.yaml" "$OUT/fastagent.secrets.docker.yaml"
copy_if_exists "$ROOT/backend/data/jarvis.db" "$OUT/jarvis.db"

# Dump config tables so the cheatsheet can reference them.
if command -v sqlite3 >/dev/null && [[ -f "$ROOT/backend/data/jarvis.db" ]]; then
    sqlite3 -header -csv "$ROOT/backend/data/jarvis.db" \
        "SELECT category, key, value, is_secret, source FROM system_config ORDER BY category, key;" \
        > "$OUT/system_config.csv" 2>/dev/null || true
    sqlite3 -header -csv "$ROOT/backend/data/jarvis.db" \
        "SELECT step_name, completed, skipped, completed_at FROM setup_wizard ORDER BY step_name;" \
        > "$OUT/setup_wizard.csv" 2>/dev/null || true
    echo "  ✓ exported system_config + setup_wizard as CSV"
fi

# -------- cheat-sheet -------------------------------------------------------
CHEAT="$OUT/ONBOARD_CHEATSHEET.md"
{
    echo "# Jarvis Onboarding Cheat-Sheet"
    echo ""
    echo "_Generated: ${STAMP}_"
    echo ""
    echo "Keep this file open while walking through \`/#/setup\` so you can paste"
    echo "values straight into each wizard step."
    echo ""
    echo "> ⚠️  Treat this file like a password manager export — it contains every"
    echo "> secret that was on the machine at backup time."
    echo ""

    if [[ -f "$OUT/.env" ]]; then
        echo "## Step 1 — Auth (master key)"
        echo ""
        echo "Either re-enter this value (the wizard will 'adopt' it) or pick a"
        echo "new one — if you pick new, remember to update \`backend/.env\` too."
        echo ""
        MASTER=$(grep -E "^JARVIS_API_KEY=" "$OUT/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d "'\"" || true)
        echo '```'
        echo "JARVIS_API_KEY=$MASTER"
        echo '```'
        echo ""
    fi

    echo "## Step 2 — LLM Provider"
    echo ""
    if [[ -f "$OUT/fastagent.secrets.yaml" ]]; then
        DEFAULT_MODEL=$(grep -E "^default_model:" "$ROOT/backend/fastagent.config.yaml" 2>/dev/null | head -1 | awk '{print $2}' || true)
        echo "From \`fastagent.config.yaml\`: **default_model** = \`${DEFAULT_MODEL:-<unset>}\`"
        echo ""
        echo "The matching provider block from \`fastagent.secrets.yaml\`:"
        echo ""
        echo '```yaml'
        # Print only top-level provider blocks (skip the `mcp:` tree, which is
        # covered under Step 3).  A provider block starts at col 0 with a known
        # name and ends when the next col-0 key appears.
        awk '
            BEGIN { emit=0 }
            /^[a-z_]+:/ {
                if ($1 ~ /^(openai|anthropic|google|openresponses|codexresponses|deepseek|generic|ollama|xai|tensorzero):$/) {
                    emit=1
                } else {
                    emit=0
                }
            }
            emit { print }
        ' "$OUT/fastagent.secrets.yaml" || true
        echo '```'
        echo ""
    fi

    echo "## Step 3 — Services"
    echo ""
    echo "Paste each of these into the wizard's service field (or \`config/services.json\`)."
    echo ""
    if [[ -f "$OUT/.env" ]]; then
        echo "**From \`backend/.env\`:**"
        echo ""
        echo '```'
        grep -vE "^\s*#|^\s*$" "$OUT/.env" | sort || true
        echo '```'
        echo ""
    fi

    if [[ -f "$OUT/fastagent.secrets.yaml" ]]; then
        echo "**MCP server envs from \`fastagent.secrets.yaml\`:**"
        echo ""
        echo '```yaml'
        # Emit the mcp: block until the next top-level (col-0) key starts.
        awk '
            /^mcp:/ { emit=1; print; next }
            emit && /^[a-z_]+:/ { emit=0 }
            emit { print }
        ' "$OUT/fastagent.secrets.yaml" || true
        echo '```'
        echo ""
    fi

    echo "## Step 4 — YAML Config"
    echo ""
    echo "The YAML editor loads \`fastagent.config.yaml\` and \`fastagent.secrets.yaml\`"
    echo "straight off disk.  The snapshots in this directory are byte-for-byte copies —"
    echo "if you need to eyeball them:"
    echo ""
    echo "  - \`$OUT/fastagent.secrets.yaml\`"
    [[ -f "$OUT/fastagent.secrets.docker.yaml" ]] && echo "  - \`$OUT/fastagent.secrets.docker.yaml\`"
    echo ""

    echo "## Step 5 — Verify"
    echo ""
    echo "No data to paste.  The verify step just checks the wizard covered the"
    echo "critical keys (\`auth.JARVIS_API_KEY\`, \`llm.api_key\`, \`llm.model\`)."
    echo ""

    echo "---"
    echo ""
    echo "## Restore (if anything breaks)"
    echo ""
    echo '```bash'
    echo "bash scripts/onboard_restore.sh $OUT"
    echo '```'
    echo ""
    echo "Or reset *only* the wizard state (keeps \`.env\` and stored secrets intact):"
    echo ""
    echo '```bash'
    echo "bash scripts/onboard_reset_wizard.sh"
    echo '```'
} > "$CHEAT"

echo "  ✓ wrote $CHEAT"
echo ""
echo "Backup complete."
echo "  Path:       $OUT"
echo "  Cheatsheet: $CHEAT"
echo ""
echo "To restore:     bash scripts/onboard_restore.sh $OUT"
echo "To wipe wizard: bash scripts/onboard_reset_wizard.sh"
