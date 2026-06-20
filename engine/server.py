#!/usr/bin/env python3
"""Token Budget Engine — entry point.

Spawned by the Claude Usage Systray Swift app.
Runs a polling thread + HTTP server thread.

Usage:
    python3 -m engine.server --port 17420 --token <OAUTH_TOKEN>
    python3 -m engine.server --port 17420 --token <OAUTH_TOKEN> --db-path /path/to/db
"""

import argparse
import logging
import os
import signal
import sys
import threading
from logging.handlers import RotatingFileHandler

from engine.db import UsageDB
from engine.poller import TokenHolder, poll_loop
from engine.jsonl_rollup import rollup_loop
from engine.api import create_server
from engine.codeburn import get_codeburn_report
from engine.providers import warm_overview_cache
from engine.sessions import get_token_history

DEFAULT_PORT = 17420
DEFAULT_DB_DIR = os.path.expanduser("~/.local/share/token-budget")
LOG_DIR = os.path.expanduser("~/Library/Logs/ClaudeUsageSystray")


def _setup_logging() -> None:
    """Configure rotating file + stderr logging."""
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, "engine.log")

    fmt = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        log_path, maxBytes=2_000_000, backupCount=2, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(fmt)

    root = logging.getLogger("engine")
    root.setLevel(logging.DEBUG)
    root.addHandler(file_handler)
    root.addHandler(stderr_handler)


def _resolve_token(args, log) -> str:
    """Resolve the OAuth token without exposing it on the command line.

    Order of precedence:
      1. --token-file PATH  (read + strip)
      2. CLAUDE_OAUTH_TOKEN environment variable
      3. --token CLI arg    (deprecated: visible in `ps`)
    """
    if args.token_file:
        with open(os.path.expanduser(args.token_file), "r", encoding="utf-8") as fh:
            return fh.read().strip()

    env_token = os.environ.get("CLAUDE_OAUTH_TOKEN")
    if env_token:
        return env_token.strip()

    if args.token:
        log.warning("Token passed via --token is visible in `ps`; "
                    "prefer CLAUDE_OAUTH_TOKEN env or --token-file")
        return args.token

    raise SystemExit(
        "No OAuth token provided. Set CLAUDE_OAUTH_TOKEN, pass --token-file PATH, "
        "or (deprecated) --token."
    )


def main():
    _setup_logging()
    log = logging.getLogger("engine.server")

    parser = argparse.ArgumentParser(description="Token Budget Engine")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--token", default=None,
                        help="Claude OAuth token (DEPRECATED: visible in `ps`; "
                             "prefer CLAUDE_OAUTH_TOKEN env or --token-file)")
    parser.add_argument("--token-file", default=None,
                        help="Path to a file containing the Claude OAuth token")
    parser.add_argument("--db-path", default=None, help="SQLite database path")
    parser.add_argument("--poll-interval", type=int, default=None,
                        help="Seconds between usage API polls (default: poller.POLL_INTERVAL)")
    args = parser.parse_args()

    # Ensure db directory exists
    if args.db_path:
        db_path = args.db_path
    else:
        os.makedirs(DEFAULT_DB_DIR, exist_ok=True)
        db_path = os.path.join(DEFAULT_DB_DIR, "token_budget.db")

    db = UsageDB(db_path)
    token_holder = TokenHolder(_resolve_token(args, log))
    stop_event = threading.Event()

    def shutdown_handler(signum, frame):
        log.info("Shutting down (signal=%s)", signum)
        stop_event.set()
        db.checkpoint()
        db.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    # Snapshot source: Anthropic OAuth API (default) or local JSONL rollup (fallback).
    # TOKEN_BUDGET_USE_API=1 (default): polls /api/oauth/usage — authoritative percentages.
    # TOKEN_BUDGET_USE_API=0: counts tokens from local JSONL transcripts against hardcoded
    #   quota constants (QUOTA_7D, QUOTA_5H). Use only when API is persistently 429-gated.
    #   Note: JSONL rollup and API report different scales and will not agree numerically.
    use_api_poller = os.environ.get("TOKEN_BUDGET_USE_API", "1") == "1"
    if use_api_poller:
        poll_kwargs = {"poll_interval": args.poll_interval} if args.poll_interval else {}
        poller_thread = threading.Thread(
            target=poll_loop, args=(token_holder, db, stop_event), kwargs=poll_kwargs, daemon=True
        )
        poller_thread.start()
        log.info("API poller started (TOKEN_BUDGET_USE_API=1)")
    else:
        rollup_thread = threading.Thread(
            target=rollup_loop, args=(db, stop_event), daemon=True
        )
        rollup_thread.start()
        log.info("JSONL rollup started")

    # Warm codeburn + token history caches in background so first page load is fast
    def _warm_caches():
        try:
            get_codeburn_report(7)
            get_codeburn_report(30)
            get_codeburn_report(364)
            get_codeburn_report(355)
            get_token_history()
            warm_overview_cache([(7, "7d"), (30, "30d"), (355, "all")])
            log.info("Cache warmup complete (codeburn + token-history + overview)")
        except Exception as exc:
            log.warning("Cache warmup failed: %s", exc)

    warmup_thread = threading.Thread(target=_warm_caches, daemon=True)
    warmup_thread.start()

    # Start HTTP server (blocks main thread)
    server = create_server(db, token_holder, port=args.port)
    actual_port = server.server_address[1]
    log.info("Listening on http://127.0.0.1:%d", actual_port)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        shutdown_handler(None, None)


if __name__ == "__main__":
    main()
