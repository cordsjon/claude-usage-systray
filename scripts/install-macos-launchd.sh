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

# engine/db.py uses `X | None` annotations (PEP 604) which need Python >= 3.10,
# and engine/patterns.py needs PyYAML. The project venv at .venv/ satisfies both.
# Preference order:
#   1. PROMPT_INGEST_PYTHON env override
#   2. $PROJECT_DIR/.venv/bin/python3
#   3. Homebrew's /opt/homebrew/bin/python3 (requires PyYAML installed globally)
#   4. PATH python3 (requires PyYAML installed globally)
if [[ -n "${PROMPT_INGEST_PYTHON:-}" ]]; then
  PYTHON_BIN="$PROMPT_INGEST_PYTHON"
elif [[ -x "$PROJECT_DIR/.venv/bin/python3" ]]; then
  PYTHON_BIN="$PROJECT_DIR/.venv/bin/python3"
elif [[ -x /opt/homebrew/bin/python3 ]]; then
  PYTHON_BIN="/opt/homebrew/bin/python3"
else
  PYTHON_BIN="$(command -v python3 || true)"
fi
if [[ -z "$PYTHON_BIN" ]] || ! "$PYTHON_BIN" -c 'import sys, yaml; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
  echo "error: need Python >= 3.10 with PyYAML installed; found '$PYTHON_BIN'" >&2
  echo "hint: from $PROJECT_DIR, run: /opt/homebrew/bin/python3 -m venv .venv && .venv/bin/pip install pyyaml" >&2
  exit 1
fi

if [[ ! -f "$TEMPLATE" ]]; then
  echo "error: template plist not found: $TEMPLATE" >&2
  exit 1
fi

mkdir -p "$LOG_DIR"
mkdir -p "$(dirname "$DEST")"

sed -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    -e "s|__LOG_PATH__|$LOG_PATH|g" \
    -e "s|__PYTHON_BIN__|$PYTHON_BIN|g" \
    "$TEMPLATE" > "$DEST"

# Unload first (tolerate failure — agent may not be loaded yet).
launchctl unload "$DEST" 2>/dev/null || true
launchctl load "$DEST"

echo "loaded: $DEST"
echo "  WorkingDirectory: $PROJECT_DIR"
echo "  Python: $PYTHON_BIN"
echo "  Log: $LOG_PATH"
