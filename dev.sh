#!/usr/bin/env bash
#
# dev.sh — run Jarvis locally WITHOUT Docker (backend + frontend, one command).
#
# Why this exists: `docker compose up` is the supported path, but for quick
# local hacking you may not want Docker. This boots both services, waits until
# they are actually healthy, then prints (and opens) the URL to use.
#
# Launch commands (per backend/README.md + frontend/vite.config.js):
#   backend  → uv run uvicorn server:app --host 0.0.0.0 --port 8000
#   frontend → npm run dev  (Vite :3000, proxies /api + /ws → :8000)
#
# The frontend dev server proxies /api and /ws to the backend, so the single
# URL a human opens is the FRONTEND one (http://localhost:3000).
#
# Usage:
#   ./dev.sh              # start both, wait until ready, open browser
#   ./dev.sh --no-open    # same, but don't auto-open the browser
#
# Override ports via env: BACKEND_PORT=8001 VITE_PORT=3001 ./dev.sh
# Pin the first-run web-login key: JARVIS_API_KEY=mypass ./dev.sh
#   (only used when creating backend/.env; otherwise one is generated + printed)
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${VITE_PORT:-3000}"
LOG_DIR="$ROOT/.dev"           # *.log is gitignored, so this stays untracked
OPEN_BROWSER=1
READY_TIMEOUT=120              # seconds to wait for backend health

for arg in "$@"; do
  case "$arg" in
    --no-open) OPEN_BROWSER=0 ;;
    -h|--help)
      grep -E '^#( |$)' "$0" | sed -E 's/^# ?//'
      exit 0 ;;
    *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

# --- pretty output helpers ------------------------------------------------
if [ -t 1 ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; CYAN=$'\033[36m'; RED=$'\033[31m'
  HL=$'\033[30;43m'  # black on yellow — the "look here" highlight
  RESET=$'\033[0m'
else BOLD=""; DIM=""; GREEN=""; CYAN=""; RED=""; HL=""; RESET=""; fi
info()  { echo "${CYAN}▸${RESET} $*"; }
ok()    { echo "${GREEN}✓${RESET} $*"; }
warn()  { echo "${RED}!${RESET} $*" >&2; }
# highlight(): a yellow attention bar for things the user must notice (port moves).
highlight() { echo "${HL} ⚠ $* ${RESET}"; }
die()   { warn "$*"; exit 1; }

# --- requirements ---------------------------------------------------------
# Verify prerequisites up front so a missing tool is an obvious one-line error,
# not a cryptic failure mid-boot. Required versions: Python 3.13.x (pyproject
# `>=3.13.5,<3.14`), Node >= 20.19 (vite 8). Python is provisioned by uv, so we
# only need uv on PATH — not a system python3.13.
echo "${BOLD}Checking requirements…${RESET}"
REQ_OK=1

if command -v uv >/dev/null 2>&1; then ok "uv      $(uv --version 2>/dev/null | awk '{print $2}')"
else warn "uv missing — install: https://docs.astral.sh/uv/getting-started/installation/"; REQ_OK=0; fi

if command -v node >/dev/null 2>&1; then
  node_major=$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)
  if [ "$node_major" -ge 20 ]; then ok "node    $(node -v)"
  else warn "node $(node -v) too old — need >= 20.19 (vite 8). Upgrade Node.js."; REQ_OK=0; fi
else warn "node missing — install Node.js 20+ (https://nodejs.org)"; REQ_OK=0; fi
command -v npm >/dev/null 2>&1 || { warn "npm missing — bundled with Node.js"; REQ_OK=0; }

# Python comes from uv. If the venv exists, surface its interpreter so the user
# can confirm it's 3.13.x; otherwise uv will provision 3.13 on first `uv sync`.
if [ -f "$ROOT/backend/.venv/pyvenv.cfg" ]; then
  pyver=$(awk -F'= *' '/^version/{print $2}' "$ROOT/backend/.venv/pyvenv.cfg" 2>/dev/null)
  case "$pyver" in
    3.13.*) ok "python  $pyver (backend/.venv)" ;;
    "")     : ;;
    *)      warn "backend venv python is $pyver — project expects 3.13.x; recreate: (cd backend && rm -rf .venv && uv sync)" ;;
  esac
fi

# CLI tools this script itself depends on.
for t in curl lsof; do
  command -v "$t" >/dev/null 2>&1 || { warn "$t missing — required by this script"; REQ_OK=0; }
done

[ "$REQ_OK" -eq 1 ] || die "Missing prerequisites above — install them and re-run."

# --- preflight ------------------------------------------------------------
[ -f "$ROOT/backend/fast-agent/pyproject.toml" ] || \
  die "Submodules missing. Run:  git submodule update --init --recursive"

# Seed config from the examples on first run (mirrors README quick start).
# We NEVER touch an existing .env — these only fire when the file is absent.
#
# Important: .env.example ships placeholders like __GENERATE_JARVIS_API_KEY__,
# and the backend does NOT reject them — copying as-is would boot with weak,
# predictable secrets. So instead of a blind copy we generate a fresh random
# value (token_urlsafe(32), exactly as the .env.example comments instruct) for
# every __GENERATE_*__ marker. The result is working AND secure on first run.
# (We do NOT and cannot invent your LLM/API provider keys — add those yourself.)
#
# JARVIS_API_KEY is the bootstrap web-login password (routes/auth.py). You may
# pin it:  JARVIS_API_KEY=mypass ./dev.sh  — otherwise we generate one and print
# it once in the banner below so you don't have to dig it out of backend/.env.
ENV_CREATED=0
FIRST_RUN_API_KEY=""
if [ ! -f "$ROOT/backend/.env" ]; then
  FIRST_RUN_API_KEY=$(uv run --no-project python - \
    "$ROOT/backend/.env.example" "$ROOT/backend/.env" "${JARVIS_API_KEY:-}" <<'PY'
import re, secrets, sys
src, dst, provided = sys.argv[1], sys.argv[2], sys.argv[3]
api_key = provided.strip() or secrets.token_urlsafe(32)
# Use the caller-supplied key for JARVIS_API_KEY; generate fresh for the rest.
def repl(m):
    return api_key if m.group(0) == "__GENERATE_JARVIS_API_KEY__" else secrets.token_urlsafe(32)
open(dst, "w").write(re.sub(r"__GENERATE_[A-Z_]+__", repl, open(src).read()))
print(api_key)   # sole stdout line → captured for the banner
PY
)
  ENV_CREATED=1
  warn "Created backend/.env with generated secrets — add your LLM/API provider keys to it."
fi
# fastagent.secrets.yaml holds external provider keys we can't generate — copy
# the template and let the user fill it in (or use the in-app Setup Wizard).
if [ ! -f "$ROOT/backend/fastagent.secrets.yaml" ]; then
  cp "$ROOT/backend/fastagent.secrets.yaml.example" "$ROOT/backend/fastagent.secrets.yaml"
  warn "Created backend/fastagent.secrets.yaml from example — add your provider keys."
fi

# Install frontend deps on first run.
if [ ! -d "$ROOT/frontend/node_modules" ]; then
  info "Installing frontend dependencies (first run)…"
  ( cd "$ROOT/frontend" && npm install )
fi

# --- port handling: auto-switch when busy, warn loudly ---------------------
port_busy() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
# next_free_port: first free port at/after $1, scanning up to +20. Empty if none.
next_free_port() {
  local p=$1 max=$(( $1 + 20 ))
  while [ "$p" -le "$max" ]; do port_busy "$p" || { echo "$p"; return 0; }; p=$((p+1)); done
  return 1
}

# Backend is single-instance PER WORKSPACE (a Unix socket keyed by cwd, see
# server.py:_short_unix_socket). So a busy backend port has two meanings:
#   (a) a healthy Jarvis backend is already up → reuse it (starting a second
#       one would just abort on the socket guard), or
#   (b) some unrelated process holds the port → move to the next free port.
REUSE_BACKEND=0
if port_busy "$BACKEND_PORT"; then
  if curl -fsS -o /dev/null "http://localhost:$BACKEND_PORT/api/health" 2>/dev/null; then
    highlight "Backend already running on :$BACKEND_PORT — reusing it (one backend per workspace)."
    REUSE_BACKEND=1
  else
    new=$(next_free_port "$BACKEND_PORT") || die "No free port found near $BACKEND_PORT. Free one or set BACKEND_PORT=…"
    highlight "Port $BACKEND_PORT is busy → backend will use :$new instead."
    BACKEND_PORT=$new
  fi
fi

# Frontend always needs its own dev server; just hop to the next free port.
if port_busy "$FRONTEND_PORT"; then
  new=$(next_free_port "$FRONTEND_PORT") || die "No free port found near $FRONTEND_PORT. Free one or set VITE_PORT=…"
  highlight "Port $FRONTEND_PORT is busy → frontend will use :$new instead."
  FRONTEND_PORT=$new
fi

mkdir -p "$LOG_DIR"

# --- lifecycle: kill the whole process tree on exit -----------------------
BACKEND_PID="" FRONTEND_PID="" TAIL_PID=""
kill_tree() {
  local pid=$1
  [ -n "$pid" ] || return 0
  local child
  for child in $(pgrep -P "$pid" 2>/dev/null); do kill_tree "$child"; done
  kill "$pid" 2>/dev/null || true
}
cleanup() {
  trap - INT TERM EXIT
  echo
  info "Shutting down…"
  [ -n "$TAIL_PID" ]     && kill "$TAIL_PID" 2>/dev/null || true
  kill_tree "$FRONTEND_PID"
  kill_tree "$BACKEND_PID"
  wait 2>/dev/null || true
  ok "Stopped."
}
trap cleanup INT TERM EXIT

# --- start services -------------------------------------------------------
if [ "$REUSE_BACKEND" -eq 1 ]; then
  ok "Reusing backend already on http://localhost:$BACKEND_PORT (not managed by this script)."
else
  info "Starting backend  → http://localhost:$BACKEND_PORT  ${DIM}(log: .dev/backend.log)${RESET}"
  # Canonical launch command from backend/README.md. `uv run` syncs the venv to
  # backend/uv.lock first (a no-op once synced), then execs uvicorn. First run on
  # a fresh clone provisions the venv + Python 3.13, so it may take a minute.
  ( cd "$ROOT/backend" && exec uv run uvicorn server:app --host 0.0.0.0 --port "$BACKEND_PORT" ) \
    >"$LOG_DIR/backend.log" 2>&1 &
  BACKEND_PID=$!
fi

info "Starting frontend → http://localhost:$FRONTEND_PORT ${DIM}(log: .dev/frontend.log)${RESET}"
# Keep the Vite proxy pointed at whatever backend port we actually used, so
# overriding BACKEND_PORT doesn't silently break /api and /ws (vite.config.js).
export VITE_PROXY_TARGET="http://localhost:$BACKEND_PORT"
( cd "$ROOT/frontend" && exec npm run dev -- --port "$FRONTEND_PORT" ) \
  >"$LOG_DIR/frontend.log" 2>&1 &
FRONTEND_PID=$!

# --- wait until actually ready (not just "process launched") --------------
wait_http() {  # $1=url  $2=label  $3=pid
  local url=$1 label=$2 pid=$3 log="$LOG_DIR/$2.log" i=0
  printf "%s" "  waiting for $label "
  while [ "$i" -lt "$READY_TIMEOUT" ]; do
    # Two ways to detect a failed boot: the process exits, OR it prints a fatal
    # startup error but lingers (uvicorn keeps the `uv` wrapper / a background
    # thread alive). Catch both so we fail in ~1s instead of waiting the full
    # timeout.
    if ! kill -0 "$pid" 2>/dev/null \
       || grep -qE "Application startup failed|Address already in use" "$log" 2>/dev/null; then
      echo; warn "$label failed to start — last log lines:"
      tail -n 25 "$log" >&2
      exit 1
    fi
    if curl -fsS -o /dev/null "$url" 2>/dev/null; then echo " ${GREEN}ready${RESET}"; return 0; fi
    printf "."; sleep 1; i=$((i+1))
  done
  echo; die "$label did not become ready within ${READY_TIMEOUT}s (see $log)"
}

# Reused backend was already confirmed healthy above; only wait on one we started.
[ "$REUSE_BACKEND" -eq 1 ] || \
  wait_http "http://localhost:$BACKEND_PORT/api/health" "backend"  "$BACKEND_PID"
wait_http "http://localhost:$FRONTEND_PORT/"          "frontend" "$FRONTEND_PID"

# --- banner ---------------------------------------------------------------
URL="http://localhost:$FRONTEND_PORT"
echo
echo "${GREEN}${BOLD}  Jarvis is up.${RESET}"
echo "  ${BOLD}Open:${RESET}     ${CYAN}${BOLD}$URL${RESET}"
if [ "$REUSE_BACKEND" -eq 1 ]; then
  echo "  ${DIM}Backend:  http://localhost:$BACKEND_PORT  (reused — not managed here)${RESET}"
  echo "  ${DIM}Logs:     tail -f .dev/frontend.log${RESET}"
else
  echo "  ${DIM}Backend:  http://localhost:$BACKEND_PORT  (API + /api/health)${RESET}"
  echo "  ${DIM}Logs:     tail -f .dev/backend.log .dev/frontend.log${RESET}"
fi
echo "  ${DIM}Stop:     Ctrl+C${RESET}"
# On a fresh .env, surface the bootstrap web-login key once so the user can log
# in without opening backend/.env. Only shown the run we created the file.
if [ "$ENV_CREATED" -eq 1 ] && [ -n "$FIRST_RUN_API_KEY" ]; then
  echo
  echo "  ${HL} First-run login key (JARVIS_API_KEY) ${RESET}"
  echo "    ${BOLD}$FIRST_RUN_API_KEY${RESET}"
  echo "  ${DIM}saved in backend/.env — use it to log in to the web UI the first time${RESET}"
fi
echo

if [ "$OPEN_BROWSER" -eq 1 ]; then
  if   command -v open     >/dev/null 2>&1; then open "$URL"           # macOS
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1 || true  # Linux
  fi
fi

# --- stream live logs; Ctrl+C triggers cleanup ---------------------------
# Only tail logs for services THIS script started (a reused backend writes
# elsewhere), and only wait on PIDs we own.
if [ "$REUSE_BACKEND" -eq 1 ]; then
  tail -n 0 -f "$LOG_DIR/frontend.log" & TAIL_PID=$!
  wait "$FRONTEND_PID"
else
  tail -n 0 -f "$LOG_DIR/backend.log" "$LOG_DIR/frontend.log" & TAIL_PID=$!
  wait "$BACKEND_PID" "$FRONTEND_PID"
fi
