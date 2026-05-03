#!/usr/bin/env bash
# reload-server.sh — kill the running engine server so launchd/Swift watchdog can respawn it
set -euo pipefail

ENGINE_PORT="${ENGINE_PORT:-17420}"

pid="$(lsof -nP -iTCP:"$ENGINE_PORT" -sTCP:LISTEN -t 2>/dev/null | head -n 1 || true)"
if [[ -z "$pid" ]]; then
  echo "engine not running on port $ENGINE_PORT"
  exit 0
fi

echo "killing pid=$pid (port $ENGINE_PORT)"
kill "$pid" 2>/dev/null || true
sleep 0.3

if kill -0 "$pid" 2>/dev/null; then
  echo "still alive; sending SIGKILL pid=$pid"
  kill -9 "$pid" 2>/dev/null || true
fi

echo "done"

