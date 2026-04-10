#!/bin/bash
# launcher.sh — Standalone engine launcher for launchd
# Reads OAuth token from Keychain, starts the Python engine.
# Exits cleanly if the engine port is already in use.

set -uo pipefail

ENGINE_PORT="${ENGINE_PORT:-17420}"
ENGINE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_PREFIX="[engine-launcher]"

# Check if port is already in use
if lsof -iTCP:"$ENGINE_PORT" -sTCP:LISTEN -P -n >/dev/null 2>&1; then
    echo "$LOG_PREFIX Port $ENGINE_PORT already in use, exiting"
    exit 0
fi

# Read OAuth token from Keychain
KEYCHAIN_JSON=$(security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null)
if [[ -z "$KEYCHAIN_JSON" ]]; then
    echo "$LOG_PREFIX Cannot read Claude Code credentials from Keychain" >&2
    exit 1
fi

TOKEN=$(echo "$KEYCHAIN_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['claudeAiOauth']['accessToken'])" 2>/dev/null)
if [[ -z "$TOKEN" ]]; then
    echo "$LOG_PREFIX Cannot parse OAuth token from Keychain JSON" >&2
    exit 1
fi

echo "$LOG_PREFIX Starting engine on port $ENGINE_PORT"
cd "$ENGINE_DIR"
exec python3 -m engine.server --port "$ENGINE_PORT" --token "$TOKEN"
