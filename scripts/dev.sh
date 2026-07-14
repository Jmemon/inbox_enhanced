#!/usr/bin/env bash
set -euo pipefail

# Local dev: postgres + redis in docker, fastapi + vite + celery worker + beat.
#
# Ports are picked at startup instead of hardcoded: each service prefers its
# classic default and walks deterministically upward when another local app
# holds it. The API range is deliberately tiny (8000-8004) because each
# candidate's /auth/callback must be pre-registered as a Google OAuth
# redirect URI in the GCP console — register all five once.
#
# Before picking ports we reap any survivors of a PREVIOUS run of this app
# (pidfile + repo-scoped process patterns). Without this, an orphaned old
# server can squat the preferred port and a fresh stack would silently come
# up elsewhere while the browser still talks to stale code — the exact
# failure mode this script previously suffered from.

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEV_DIR="$ROOT/.dev"
PIDFILE="$DEV_DIR/pids"
PORTSFILE="$DEV_DIR/ports.env"
mkdir -p "$DEV_DIR"

if [ ! -f "$ROOT/.env" ]; then
  echo "ERROR: $ROOT/.env not found. Copy .env.example to .env and fill it in."
  exit 1
fi

# --- reap survivors of a previous run (pidfile first, then repo-scoped patterns) ---
if [ -f "$PIDFILE" ]; then
  while read -r pid; do
    kill "$pid" 2>/dev/null || true
  done < "$PIDFILE"
  rm -f "$PIDFILE"
fi
# Safety net for orphans that escaped the pidfile (e.g. a wedged --reload
# supervisor). Patterns are scoped to THIS repo's paths so other projects'
# uvicorn/celery/vite processes are never touched.
pkill -f "$ROOT/server/.venv/bin/uvicorn" 2>/dev/null || true
pkill -f "$ROOT/server/.venv/bin/celery" 2>/dev/null || true
pkill -f "$ROOT/client/node_modules" 2>/dev/null || true
sleep 1

# --- port picking -----------------------------------------------------------
pick_port() { # pick_port PREFERRED MAX  -> echoes first bindable port, or fails
  python3 - "$1" "$2" <<'PY'
import socket, sys
start, end = int(sys.argv[1]), int(sys.argv[2])
for port in range(start, end + 1):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        s.close()
        print(port)
        sys.exit(0)
    except OSError:
        s.close()
sys.exit(1)
PY
}

# compose_port SERVICE CONTAINER_PORT -> echoes the host port of an already-
# running compose service ("" if not running). Reusing a live container's
# existing mapping avoids docker recreating (and restarting) it just because
# the walk would have picked a different number this time.
compose_port() {
  docker compose -f "$ROOT/docker-compose.yml" port "$1" "$2" 2>/dev/null \
    | sed -n 's/.*:\([0-9][0-9]*\)$/\1/p'
}

POSTGRES_PORT="$(compose_port postgres 5432)"
if [ -z "$POSTGRES_PORT" ]; then
  POSTGRES_PORT="$(pick_port 5432 5441)" || { echo "ERROR: no free port in 5432-5441"; exit 1; }
fi
REDIS_PORT="$(compose_port redis 6379)"
if [ -z "$REDIS_PORT" ]; then
  REDIS_PORT="$(pick_port 6379 6388)" || { echo "ERROR: no free port in 6379-6388"; exit 1; }
fi
API_PORT="$(pick_port 8000 8004)" || {
  echo "ERROR: no free port in 8000-8004. That range is fixed because each"
  echo "candidate must be a Google-registered OAuth redirect URI. Free one up."
  exit 1
}
VITE_PORT="$(pick_port 5173 5182)" || { echo "ERROR: no free port in 5173-5182"; exit 1; }

export POSTGRES_PORT REDIS_PORT API_PORT VITE_PORT
# Env vars beat .env in pydantic-settings, so these overrides reach the api,
# worker, beat AND alembic without touching the user's .env defaults.
export DATABASE_URL="postgresql+psycopg://inbox:inbox@localhost:${POSTGRES_PORT}/inbox"
export REDIS_URL="redis://localhost:${REDIS_PORT}/0"
export GOOGLE_REDIRECT_URI="http://localhost:${API_PORT}/auth/callback"

cat > "$PORTSFILE" <<EOF
POSTGRES_PORT=$POSTGRES_PORT
REDIS_PORT=$REDIS_PORT
API_PORT=$API_PORT
VITE_PORT=$VITE_PORT
EOF

echo "==> ports: postgres=$POSTGRES_PORT redis=$REDIS_PORT api=$API_PORT vite=$VITE_PORT (also in .dev/ports.env)"

echo "==> starting postgres + redis"
docker compose -f "$ROOT/docker-compose.yml" up -d --wait postgres redis

echo "==> applying migrations"
( cd "$ROOT/server" && uv run alembic upgrade head )

echo "==> starting backend on :$API_PORT"
( cd "$ROOT/server" && uv run uvicorn app.main:app --reload --port "$API_PORT" ) &
BACKEND_PID=$!

echo "==> starting celery worker"
( cd "$ROOT/server" && uv run celery -A app.workers.celery_app worker --loglevel=info --concurrency=2 ) &
WORKER_PID=$!

echo "==> starting celery beat"
( cd "$ROOT/server" && uv run celery -A app.workers.celery_app beat --loglevel=info --schedule=/tmp/celerybeat-schedule ) &
BEAT_PID=$!

echo "==> starting frontend on :$VITE_PORT (proxies /auth and /api to :$API_PORT)"
( cd "$ROOT/client" && bun run dev ) &
FRONTEND_PID=$!

printf '%s\n' "$BACKEND_PID" "$WORKER_PID" "$BEAT_PID" "$FRONTEND_PID" > "$PIDFILE"

echo ""
echo "==> app: http://localhost:$VITE_PORT"

trap 'kill $BACKEND_PID $WORKER_PID $BEAT_PID $FRONTEND_PID 2>/dev/null || true; rm -f "$PIDFILE"' EXIT
wait
