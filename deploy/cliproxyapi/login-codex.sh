#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_PATH="${CLI_PROXY_CONFIG_PATH:-$ROOT_DIR/config.yaml}"
AUTH_PATH="${CLI_PROXY_AUTH_PATH:-$ROOT_DIR/auths}"
IMAGE="${CLI_PROXY_IMAGE:-eceasy/cli-proxy-api:latest}"

mkdir -p "$AUTH_PATH"

cat <<'EOF'
1. On your laptop, open an SSH tunnel first:
   ssh -L 1455:127.0.0.1:1455 <user>@<ubuntu-server>

2. Keep that SSH session open while completing the browser login.
EOF

docker run --rm \
  --network host \
  -v "$CONFIG_PATH:/CLIProxyAPI/config.yaml:ro" \
  -v "$AUTH_PATH:/root/.cli-proxy-api" \
  "$IMAGE" \
  /CLIProxyAPI/CLIProxyAPI --codex-login --no-browser
