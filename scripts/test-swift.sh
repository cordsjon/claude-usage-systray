#!/usr/bin/env bash
#
# test-swift.sh — regenerate the Xcode project and run the Swift unit tests.
#
# Replaces the hand-typed loop
#   cd claude-usage-systray && xcodegen generate && xcodebuild test ... | grep -E ...
# with one command that also propagates xcodebuild's real exit status (a bare
# `... | grep` reports success whenever grep matches the word "failed").
#
# Full unfiltered output is always kept at LOG_PATH for post-mortem.
#
set -euo pipefail

APP_DIR_NAME="${APP_DIR_NAME:-claude-usage-systray}"
SCHEME="${SCHEME:-ClaudeUsageSystrayTests}"
PROJECT="${PROJECT:-ClaudeUsageSystray.xcodeproj}"
DESTINATION="${DESTINATION:-platform=macOS}"
LOG_PATH="${LOG_PATH:-$HOME/.local/state/claude-usage-systray-tests.log}"
FILTER="${FILTER:-(Test Case|Executed .* test|SUCCEEDED|FAILED|error:)}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_DIR="$ROOT_DIR/$APP_DIR_NAME"

SKIP_GEN=0
VERBOSE=0

usage() {
  cat <<EOF
Usage:
  scripts/test-swift.sh [--skip-gen] [--verbose]

Options:
  --skip-gen   do not run \`xcodegen generate\` first (project.yml unchanged)
  --verbose    stream full xcodebuild output instead of the filtered summary
  -h, --help   show this help

Env:
  SCHEME=$SCHEME
  PROJECT=$PROJECT
  DESTINATION=$DESTINATION
  LOG_PATH=$LOG_PATH
  FILTER=$FILTER
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-gen) SKIP_GEN=1; shift ;;
    -v|--verbose) VERBOSE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "[test-swift] unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ ! -d "$APP_DIR" ]]; then
  echo "[test-swift] error: app dir not found: $APP_DIR" >&2
  exit 1
fi

cd "$APP_DIR"

if [[ "$SKIP_GEN" -eq 0 ]]; then
  if ! command -v xcodegen >/dev/null 2>&1; then
    echo "[test-swift] error: xcodegen not on PATH (brew install xcodegen), or pass --skip-gen" >&2
    exit 1
  fi
  echo "[test-swift] xcodegen generate ($APP_DIR_NAME/project.yml)"
  xcodegen generate
fi

mkdir -p "$(dirname "$LOG_PATH")"
echo "[test-swift] xcodebuild test -scheme $SCHEME (full log: $LOG_PATH)"

set +e
if [[ "$VERBOSE" -eq 1 ]]; then
  xcodebuild test \
    -project "$PROJECT" \
    -scheme "$SCHEME" \
    -destination "$DESTINATION" \
    CODE_SIGN_IDENTITY="" CODE_SIGNING_REQUIRED=NO CODE_SIGNING_ALLOWED=NO \
    2>&1 | tee "$LOG_PATH"
  status="${PIPESTATUS[0]}"
else
  xcodebuild test \
    -project "$PROJECT" \
    -scheme "$SCHEME" \
    -destination "$DESTINATION" \
    CODE_SIGN_IDENTITY="" CODE_SIGNING_REQUIRED=NO CODE_SIGNING_ALLOWED=NO \
    >"$LOG_PATH" 2>&1
  status="$?"
  grep -E "$FILTER" "$LOG_PATH" || true
fi
set -e

if [[ "$status" -ne 0 ]]; then
  echo "[test-swift] FAILED (xcodebuild exit $status)" >&2
  echo "[test-swift] hint: full output in $LOG_PATH" >&2
  exit "$status"
fi

echo "[test-swift] ok: tests passed"
