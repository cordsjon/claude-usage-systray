"""HTTP API layer for the token budget engine.

Serves JSON endpoints and the dashboard HTML on 127.0.0.1.
"""

import json
import os
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

from engine.db import UsageDB
from engine.poller import TokenHolder, get_current_status
from engine.sessions import get_token_history

_START_TIME = time.monotonic()

_RANGE_DAYS = {
    "7d": 7,
    "30d": 30,
    "52w": 364,
    "all": 355,
}

_DASHBOARD_PATH = os.path.join(os.path.dirname(__file__), "dashboard.html")


def _row_to_dict(row) -> dict:
    """Convert a sqlite3.Row to a plain dict."""
    return {k: row[k] for k in row.keys()}


def _json_response(handler: BaseHTTPRequestHandler, data: dict, status: int = 200) -> None:
    """Send a JSON response with CORS headers."""
    body = json.dumps(data).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _make_handler_class(db: UsageDB, token_holder: TokenHolder):
    """Create a Handler class with a reference to the database and token holder."""

    class Handler(BaseHTTPRequestHandler):

        def log_message(self, format, *args):  # noqa: A002
            """Suppress default stderr logging."""

        def do_GET(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = urllib.parse.parse_qs(parsed.query)

            if path == "/":
                self._serve_dashboard()
            elif path == "/api/health":
                self._handle_health()
            elif path == "/api/status":
                self._handle_status()
            elif path == "/api/history":
                self._handle_history(query)
            elif path == "/api/token-history":
                self._handle_token_history()
            else:
                _json_response(self, {"error": "Not found"}, 404)

        def do_POST(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"

            if path == "/api/token":
                self._handle_token_update()
            else:
                _json_response(self, {"error": "Not found"}, 404)

        def _serve_dashboard(self):
            try:
                with open(_DASHBOARD_PATH, "r", encoding="utf-8") as f:
                    html = f.read().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(html)))
                self.end_headers()
                self.wfile.write(html)
            except FileNotFoundError:
                _json_response(self, {"error": "Dashboard not found"}, 404)

        def _handle_health(self):
            uptime = round(time.monotonic() - _START_TIME, 1)
            _json_response(self, {
                "status": "ok",
                "uptime_seconds": uptime,
                "token_needs_refresh": token_holder.needs_refresh,
            })

        def _handle_token_update(self):
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length == 0:
                _json_response(self, {"error": "Missing body"}, 400)
                return
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                _json_response(self, {"error": "Invalid JSON"}, 400)
                return
            new_token = data.get("token", "").strip()
            if not new_token:
                _json_response(self, {"error": "Missing 'token' field"}, 400)
                return
            token_holder.token = new_token
            _json_response(self, {"status": "ok", "token_refreshed": True})

        def _handle_status(self):
            status = get_current_status()
            if not status:
                _json_response(self, {"error": "No data yet"}, 503)
                return
            _json_response(self, status)

        def _handle_token_history(self):
            data = get_token_history()
            _json_response(self, data)

        def _handle_history(self, query: dict):
            range_key = query.get("range", ["7d"])[0]
            days = _RANGE_DAYS.get(range_key, 7)
            since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

            snapshots = [_row_to_dict(r) for r in db.get_snapshots_since(since)]

            # Normalize cycle peaks: JS expects peak_util (use seven_day peak)
            raw_cycles = [_row_to_dict(r) for r in db.get_cycle_peaks()]
            cycles = []
            for c in raw_cycles:
                c["peak_util"] = c.get("peak_seven_day", 0)
                cycles.append(c)

            # Normalize weekday averages: JS expects {Mon: <number>, ...}
            # SQLite strftime('%w') returns 0=Sun, 1=Mon, ..., 6=Sat
            _WEEKDAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
            weekday_rows = db.get_weekday_averages(since)
            weekday_avg = {}
            for r in weekday_rows:
                day_name = _WEEKDAY_NAMES[r["weekday"]]
                weekday_avg[day_name] = r["avg_seven_day"]

            _json_response(self, {
                "version": 1,
                "snapshots": snapshots,
                "cycles": cycles,
                "weekday_avg": weekday_avg,
            })

    return Handler


def create_server(db: UsageDB, token_holder: TokenHolder, port: int = 17420) -> HTTPServer:
    """Create an HTTPServer bound to 127.0.0.1 with the given db and token holder.

    Use port=0 to let the OS pick a random available port (useful for tests).
    """
    handler_class = _make_handler_class(db, token_holder)
    server = HTTPServer(("127.0.0.1", port), handler_class)
    return server
