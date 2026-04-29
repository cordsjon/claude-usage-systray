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


def main():
    _setup_logging()
    log = logging.getLogger("engine.server")

    parser = argparse.ArgumentParser(description="Token Budget Engine")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--token", required=True, help="Claude OAuth access token")
    parser.add_argument("--db-path", default=None, help="SQLite database path")
    args = parser.parse_args()

    # Ensure db directory exists
    if args.db_path:
        db_path = args.db_path
    else:
        os.makedirs(DEFAULT_DB_DIR, exist_ok=True)
        db_path = os.path.join(DEFAULT_DB_DIR, "token_budget.db")

    db = UsageDB(db_path)
    token_holder = TokenHolder(args.token)
    stop_event = threading.Event()

    def shutdown_handler(signum, frame):
        log.info("Shutting down (signal=%s)", signum)
        stop_event.set()
        db.checkpoint()
        db.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    # Start poller thread
    poller_thread = threading.Thread(
        target=poll_loop, args=(token_holder, db, stop_event), daemon=True
    )
    poller_thread.start()
    log.info("Poller started")

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
