#!/usr/bin/env bash
# Install the claude-usage engine launchd agent (com.claude-usage-engine).
#
# Renders scripts/com.claude-usage-engine.plist into ~/Library/LaunchAgents/,
# baking in absolute paths and the token-quota calibration, then reloads via
# bootout+bootstrap. The reload sequence matters: launchd only re-reads a job's
# EnvironmentVariables at bootstrap time — `launchctl kickstart` restarts the
# process but reuses the already-loaded env, so a quota change would not take.
# Idempotent: safe to run repeatedly.
#
# Quotas default to the last known-good calibration (see the constants below
# and TOKEN-QUOTA-CALIBRATION.md). Override per-run with environment variables:
#
#   TOKEN_BUDGET_QUOTA_7D=<n> TOKEN_BUDGET_QUOTA_5H=<n> scripts/install-engine-launchd.sh
#
# Calibration is account/plan/time-specific. After a plan change or an upstream
# weight shift, backsolve a fresh value (recipe in TOKEN-QUOTA-CALIBRATION.md)
# and either re-run with the env override or update the defaults below.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE="$SCRIPT_DIR/com.claude-usage-engine.plist"
LABEL="com.claude-usage-engine"
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/ClaudeUsageSystray"
LOG_PATH="$LOG_DIR/engine-launchd.log"

# Last known-good calibration (Max20, 2026-05-31, backsolved against Anthropic
# /usage 76% weekly ground truth). Override via the env vars of the same name.
QUOTA_7D="${TOKEN_BUDGET_QUOTA_7D:-1559617554}"
QUOTA_5H="${TOKEN_BUDGET_QUOTA_5H:-77980500}"
# 1 = use Anthropic OAuth API (authoritative, default); 0 = local JSONL rollup (fallback)
USE_API="${TOKEN_BUDGET_USE_API:-1}"

DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help)
      cat <<EOF
Usage:
  scripts/install-engine-launchd.sh [--dry-run]

--dry-run:
  Prints the rendered plist + resolved quotas, but does not write/load.

Env overrides:
  TOKEN_BUDGET_QUOTA_7D   weekly weighted-token quota   (default $QUOTA_7D)
  TOKEN_BUDGET_QUOTA_5H   5-hour weighted-token quota   (default $QUOTA_5H)
EOF
      exit 0
      ;;
    *) echo "error: unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ ! -f "$TEMPLATE" ]]; then
  echo "error: template plist not found: $TEMPLATE" >&2
  exit 1
fi

RENDERED="$(sed \
    -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    -e "s|__HOME__|$HOME|g" \
    -e "s|__LOG_PATH__|$LOG_PATH|g" \
    -e "s|__QUOTA_7D__|$QUOTA_7D|g" \
    -e "s|__QUOTA_5H__|$QUOTA_5H|g" \
    -e "s|__USE_API__|$USE_API|g" \
    "$TEMPLATE")"

if [[ "$DRY_RUN" == "1" ]]; then
  echo "--- rendered plist (dry-run) ---"
  echo "$RENDERED"
  echo "---"
  echo "[dry-run] would write: $DEST"
  echo "[dry-run] QUOTA_7D=$QUOTA_7D  QUOTA_5H=$QUOTA_5H"
  echo "[dry-run] would reload via launchctl bootout + bootstrap"
  exit 0
fi

mkdir -p "$LOG_DIR"
mkdir -p "$(dirname "$DEST")"

echo "$RENDERED" > "$DEST"

UID_NUM="$(id -u)"
# bootout tolerates "not loaded"; killing the old process also frees the port
# so launcher.sh's in-use guard does not short-circuit the fresh start.
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true

# bootout is async — bootstrapping before the old job fully unregisters fails
# with "5: Input/output error". Poll until the label is gone (max ~5s).
for _ in $(seq 1 25); do
  launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1 || break
  sleep 0.2
done

launchctl bootstrap "gui/$UID_NUM" "$DEST"

echo "loaded: $DEST"
echo "  WorkingDirectory: $PROJECT_DIR"
echo "  QUOTA_7D=$QUOTA_7D  QUOTA_5H=$QUOTA_5H  USE_API=$USE_API"
echo "  Log: $LOG_PATH"
