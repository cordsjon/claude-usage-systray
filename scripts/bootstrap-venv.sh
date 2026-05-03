#!/usr/bin/env bash
# bootstrap-venv.sh — create/update local .venv with minimal deps for ingest_prompts
set -euo pipefail

die() { echo "error: $*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"

PYTHON_BIN="${PYTHON_BIN:-/opt/homebrew/bin/python3}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3 || true)"
fi
[[ -n "${PYTHON_BIN:-}" ]] || die "python3 not found"

if ! "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
  die "need Python >= 3.10 (found: $("$PYTHON_BIN" -V 2>&1 || true))"
fi

if [[ ! -x "$VENV_DIR/bin/python3" ]]; then
  echo "[bootstrap] creating venv: $VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

echo "[bootstrap] upgrading pip"
"$VENV_DIR/bin/python3" -m pip install --upgrade pip

echo "[bootstrap] installing minimal deps (pyyaml)"
"$VENV_DIR/bin/python3" -m pip install "pyyaml>=6.0.2"

echo "ok: $VENV_DIR"

