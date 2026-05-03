#!/usr/bin/env bash
#
# restart.sh — restart the claude-usage-systray engine and verify it is fresh.
#
# This mitigates “kill + sleep + curl verify” manual restarts by:
#   1) killing the process listening on ENGINE_PORT
#   2) restarting via engine/launcher.sh (reads token from Keychain)
#   3) polling /api/status until updated_at is recent
#
set -euo pipefail

ENGINE_PORT="${ENGINE_PORT:-17420}"
FRESHNESS_SECONDS="${FRESHNESS_SECONDS:-180}"
START_TIMEOUT_SECONDS="${START_TIMEOUT_SECONDS:-20}"
LOG_PATH="${LOG_PATH:-$HOME/.local/state/claude-usage-engine.log}"

ENGINE_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$ENGINE_DIR/.." && pwd)"
LAUNCHER="$ENGINE_DIR/launcher.sh"

usage() {
  cat <<EOF
Usage:
  engine/restart.sh

Env:
  ENGINE_PORT=$ENGINE_PORT
  FRESHNESS_SECONDS=$FRESHNESS_SECONDS
  START_TIMEOUT_SECONDS=$START_TIMEOUT_SECONDS
  LOG_PATH=$LOG_PATH
EOF
}

pid_by_port() {
  lsof -nP -iTCP:"$ENGINE_PORT" -sTCP:LISTEN -t 2>/dev/null | head -n 1 || true
}

stop_engine() {
  local pid
  pid="$(pid_by_port)"
  if [[ -z "$pid" ]]; then
    return 0
  fi

  echo "[restart] stopping pid=$pid (port $ENGINE_PORT)"
  kill "$pid" 2>/dev/null || true

  local end=$((SECONDS + 10))
  while [[ $SECONDS -lt $end ]]; do
    if ! kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
    sleep 0.2
  done

  echo "[restart] still alive after 10s; sending SIGKILL pid=$pid"
  kill -9 "$pid" 2>/dev/null || true
}

start_engine() {
  mkdir -p "$(dirname "$LOG_PATH")"
  echo "[restart] starting via $LAUNCHER (logs: $LOG_PATH)"
  (
    cd "$ROOT_DIR"
    nohup "$LAUNCHER" >>"$LOG_PATH" 2>&1 &
  )
}

verify_engine() {
  local url="http://127.0.0.1:${ENGINE_PORT}/api/status"
  local end=$((SECONDS + START_TIMEOUT_SECONDS))

  while [[ $SECONDS -lt $end ]]; do
    local body=""
    if body="$(curl -fsS "$url" 2>/dev/null || true)"; then
      if BODY_JSON="$body" FRESHNESS_SECONDS="$FRESHNESS_SECONDS" python3 - <<'PY' 2>/dev/null
import json, os, sys, time
from datetime import datetime, timezone

body = json.loads(os.environ.get("BODY_JSON", "") or "{}")
ts = (body.get("updated_at") or "").strip()
if not ts:
  sys.exit(1)
ts = ts.replace("Z", "+00:00")
dt = datetime.fromisoformat(ts)
if dt.tzinfo is None:
  dt = dt.replace(tzinfo=timezone.utc)
age = time.time() - dt.timestamp()
fresh = int(os.environ.get("FRESHNESS_SECONDS", "180") or "180")
sys.exit(0 if age <= fresh else 1)
PY
      then
        echo "[restart] ok: engine is serving and fresh"
        return 0
      fi
    fi
    sleep 0.5
  done

  echo "[restart] error: engine did not become fresh within ${START_TIMEOUT_SECONDS}s" >&2
  echo "[restart] hint: check $LOG_PATH" >&2
  return 1
}

case "${1:-}" in
  "" ) ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac

stop_engine
start_engine
verify_engine
