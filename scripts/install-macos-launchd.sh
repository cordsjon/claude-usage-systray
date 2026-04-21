#!/usr/bin/env bash
# Install the prompt-usage-ingest launchd agent.
#
# Substitutes __PROJECT_DIR__ and __LOG_PATH__ in the template plist, writes
# the result to ~/Library/LaunchAgents/, and (re)loads the agent. Idempotent:
# safe to run repeatedly.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE="$SCRIPT_DIR/com.jcords.prompt-usage-ingest.plist"
DEST="$HOME/Library/LaunchAgents/com.jcords.prompt-usage-ingest.plist"
LOG_DIR="$HOME/.local/state"
LOG_PATH="$LOG_DIR/prompt-usage-ingest.log"

if [[ ! -f "$TEMPLATE" ]]; then
  echo "error: template plist not found: $TEMPLATE" >&2
  exit 1
fi

mkdir -p "$LOG_DIR"
mkdir -p "$(dirname "$DEST")"

sed -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    -e "s|__LOG_PATH__|$LOG_PATH|g" \
    "$TEMPLATE" > "$DEST"

# Unload first (tolerate failure — agent may not be loaded yet).
launchctl unload "$DEST" 2>/dev/null || true
launchctl load "$DEST"

echo "loaded: $DEST"
echo "  WorkingDirectory: $PROJECT_DIR"
echo "  Log: $LOG_PATH"
