"""HTTP API layer for the token budget engine.

Serves JSON endpoints and the dashboard HTML on 127.0.0.1.
"""

import json
import os
import re
import tempfile
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from engine.codeburn import get_codeburn_report
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

# Default on-disk locations for Habits tab artifacts (US-TB-01)
_DEFAULT_CLASSIFICATION_PATH = (
    Path(__file__).parent / "data" / "prompt-classification.json"
)
_DEFAULT_PATTERNS_YAML_PATH = (
    Path.home()
    / ".claude"
    / "projects"
    / "-Users-jcords-macmini-projects"
    / "memory"
    / "prompt-patterns.yaml"
)

# In-memory dashboard cache — avoid re-reading 2500-line HTML on every request
_dashboard_cache: bytes | None = None
_dashboard_mtime: float = 0.0


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


# ── Habits-tab helpers (US-TB-01) ───────────────────────────

def _resolve_skill_dirs():
    """Return existing skill/command dirs for skill-existence lookup."""
    base = [
        Path.home() / ".claude" / "skills",
        Path.home() / ".claude" / "commands",
    ]
    return [p for p in base if p.exists()]


def _load_patterns_info(yaml_path):
    """Return {pattern_id: {intent, type}} — empty on any failure.

    Lazy-imports engine.patterns so a missing/in-flight module does not break
    unrelated endpoints (chunk 2A owns engine.patterns).
    """
    try:
        from engine.patterns import load_patterns
    except ImportError:
        return {}
    try:
        patterns = load_patterns(yaml_path)
    except Exception:
        return {}
    return {
        p["id"]: {"intent": p.get("intent", ""), "type": p.get("type", "")}
        for p in patterns
    }


def _append_pattern_yaml(yaml_path, entry):
    """Append a pattern entry to the YAML file atomically.

    File is created with `{"patterns": []}` if missing.
    """
    import yaml as _yaml
    p = Path(yaml_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        doc = _yaml.safe_load(p.read_text()) or {}
    else:
        doc = {}
    patterns = doc.get("patterns") or []
    patterns.append(entry)
    doc["patterns"] = patterns

    tmp = tempfile.NamedTemporaryFile(
        mode="w", dir=str(p.parent), delete=False, suffix=".tmp"
    )
    try:
        _yaml.safe_dump(doc, tmp, sort_keys=False)
        tmp.flush()
        tmp.close()
        Path(tmp.name).replace(p)
    except Exception:
        Path(tmp.name).unlink(missing_ok=True)
        raise


def _make_handler_class(
    db: UsageDB,
    token_holder: TokenHolder,
    classification_path: Path,
    patterns_yaml_path: Path,
):
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
            elif path == "/api/codeburn":
                self._handle_codeburn(query)
            elif path == "/api/prompts":
                self._handle_prompts()
            else:
                _json_response(self, {"error": "Not found"}, 404)

        def do_POST(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"

            if path == "/api/token":
                self._handle_token_update()
            elif path == "/api/prompts/classify":
                self._handle_classify()
            elif path == "/api/prompts/dry-run":
                self._handle_dry_run()
            elif path == "/api/prompts/pattern":
                self._handle_add_pattern()
            else:
                _json_response(self, {"error": "Not found"}, 404)

        def _serve_dashboard(self):
            global _dashboard_cache, _dashboard_mtime
            try:
                mtime = os.path.getmtime(_DASHBOARD_PATH)
                if _dashboard_cache is None or mtime != _dashboard_mtime:
                    with open(_DASHBOARD_PATH, "r", encoding="utf-8") as f:
                        _dashboard_cache = f.read().encode("utf-8")
                    _dashboard_mtime = mtime
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(_dashboard_cache)))
                self.end_headers()
                self.wfile.write(_dashboard_cache)
            except FileNotFoundError:
                _json_response(self, {"error": "Dashboard not found"}, 404)

        def _handle_health(self):
            uptime = round(time.monotonic() - _START_TIME, 1)
            _json_response(self, {
                "status": "ok",
                "uptime_seconds": uptime,
                "token_needs_refresh": token_holder.needs_refresh,
            })

        def _read_json_body(self):
            """Read & parse a JSON POST body. Returns (data, err_response_sent)."""
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length == 0:
                _json_response(self, {"error": "Missing body"}, 400)
                return None, True
            body = self.rfile.read(content_length)
            try:
                return json.loads(body), False
            except (json.JSONDecodeError, ValueError):
                _json_response(self, {"error": "Invalid JSON"}, 400)
                return None, True

        def _handle_token_update(self):
            data, err = self._read_json_body()
            if err:
                return
            new_token = (data.get("token") or "").strip()
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

        def _handle_codeburn(self, query: dict):
            range_key = query.get("range", ["7d"])[0]
            days = _RANGE_DAYS.get(range_key, 7)
            data = get_codeburn_report(days)
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

        # ── Habits tab endpoints (US-TB-01) ─────────────────

        def _handle_prompts(self):
            """GET /api/prompts — ranked patterns split into everyday/case_by_case.

            Adds `skill_candidate` flag when:
              count_30d >= 3 AND not is_structured AND no matching skill dir.
            """
            from engine.classification import load_classification

            ranked = db.get_ranked_prompts(today=date.today().isoformat())
            cls = load_classification(classification_path)
            patterns_info = _load_patterns_info(patterns_yaml_path)
            skill_dirs = _resolve_skill_dirs()

            everyday, case_by_case = [], []
            for row in ranked:
                info = patterns_info.get(row["pattern_id"], {})
                has_skill = any(
                    (d / row["pattern_id"]).exists()
                    or (d / f"{row['pattern_id']}.md").exists()
                    for d in skill_dirs
                )
                skill_candidate = (
                    row["count_30d"] >= 3
                    and not row["is_structured"]
                    and not has_skill
                )
                item = {
                    "pattern_id": row["pattern_id"],
                    "intent": info.get("intent", ""),
                    "type": "structured" if row["is_structured"] else "unstructured",
                    "count_7d": row["count_7d"],
                    "count_30d": row["count_30d"],
                    "count_all": row["count_all"],
                    "has_skill": has_skill,
                    "skill_candidate": skill_candidate,
                }
                if row["pattern_id"] in cls["everyday"]:
                    everyday.append(item)
                else:
                    case_by_case.append(item)

            _json_response(
                self,
                {
                    "everyday": everyday,
                    "case_by_case": case_by_case,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                },
                200,
            )

        def _handle_classify(self):
            """POST /api/prompts/classify — move pattern between sections."""
            from engine.classification import move_pattern

            data, err = self._read_json_body()
            if err:
                return
            pattern_id = data.get("pattern_id")
            section = data.get("section")
            if not pattern_id:
                _json_response(self, {"error": "missing 'pattern_id'"}, 400)
                return
            if not section:
                _json_response(self, {"error": "missing 'section'"}, 400)
                return
            try:
                result = move_pattern(classification_path, pattern_id, section)
            except ValueError as exc:
                _json_response(self, {"error": str(exc)}, 400)
                return
            _json_response(self, result, 200)

        def _handle_dry_run(self):
            """POST /api/prompts/dry-run — preview regex hits over last 7 days."""
            data, err = self._read_json_body()
            if err:
                return
            regex_src = data.get("regex")
            if not regex_src:
                _json_response(self, {"error": "missing 'regex'"}, 400)
                return
            try:
                regex = re.compile(regex_src)
            except re.error as exc:
                _json_response(self, {"error": f"bad regex: {exc}"}, 400)
                return

            # Last 7 days of unmatched text excerpts
            cutoff = (date.today() - timedelta(days=7)).isoformat()
            rows = db._conn.execute(
                "SELECT text_excerpt FROM prompt_unmatched WHERE date >= ?",
                (cutoff,),
            ).fetchall()

            hit_count = 0
            sample_matches = []
            for r in rows:
                text = r[0] if not isinstance(r, dict) else r["text_excerpt"]
                if regex.search(text):
                    hit_count += 1
                    if len(sample_matches) < 10:
                        sample_matches.append(text)

            _json_response(
                self,
                {
                    "hit_count": hit_count,
                    "sample_matches": sample_matches,
                    "window_days": 7,
                },
                200,
            )

        def _handle_add_pattern(self):
            """POST /api/prompts/pattern — append a pattern to the YAML file."""
            data, err = self._read_json_body()
            if err:
                return
            required = ("id", "intent", "regex", "type", "version")
            missing = [k for k in required if k not in data]
            if missing:
                _json_response(
                    self,
                    {"error": f"missing fields: {missing}"},
                    400,
                )
                return
            # Validate regex compiles
            try:
                re.compile(data["regex"])
            except re.error as exc:
                _json_response(self, {"error": f"bad regex: {exc}"}, 400)
                return
            entry = {
                "id": data["id"],
                "intent": data["intent"],
                "regex": data["regex"],
                "type": data["type"],
                "version": int(data["version"]),
            }
            try:
                _append_pattern_yaml(patterns_yaml_path, entry)
            except Exception as exc:
                _json_response(
                    self, {"error": f"yaml write failed: {exc}"}, 500
                )
                return
            _json_response(self, {"status": "ok", "pattern": entry}, 200)

    return Handler


def create_server(
    db: UsageDB,
    token_holder: TokenHolder,
    port: int = 17420,
    classification_path: Path | None = None,
    patterns_yaml_path: Path | None = None,
) -> HTTPServer:
    """Create an HTTPServer bound to 127.0.0.1 with the given db and token holder.

    Use port=0 to let the OS pick a random available port (useful for tests).
    `classification_path` and `patterns_yaml_path` default to on-disk locations.
    """
    handler_class = _make_handler_class(
        db,
        token_holder,
        classification_path or _DEFAULT_CLASSIFICATION_PATH,
        patterns_yaml_path or _DEFAULT_PATTERNS_YAML_PATH,
    )
    server = HTTPServer(("127.0.0.1", port), handler_class)
    return server
