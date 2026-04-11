"""Session JSONL scanner for token usage aggregation.

Scans Claude Code session files to extract per-day input/output token counts.
Results are cached in memory with a configurable TTL.
"""

import glob
import json
import logging
import os
import threading
import time
from collections import defaultdict
from datetime import datetime

log = logging.getLogger("engine.sessions")

_SESSIONS_BASE = os.path.expanduser("~/.claude/projects")
_CACHE_TTL = 3600  # 1 hour

_cache_lock = threading.Lock()
_cached_data: dict | None = None
_cached_at: float = 0.0


def _scan_sessions() -> dict:
    """Scan all session JSONL files and aggregate daily token usage.

    Returns:
        {"days": [{"date": "YYYY-MM-DD", "input": int, "output": int,
                    "cache_create": int, "cache_read": int, "sessions": int}],
         "totals": {"input": int, "output": int, "cache_create": int,
                     "cache_read": int, "sessions": int},
         "scanned_at": str}
    """
    daily: dict[str, dict] = defaultdict(
        lambda: {"input": 0, "output": 0, "cache_create": 0, "cache_read": 0, "sessions": 0}
    )

    files = glob.glob(os.path.join(_SESSIONS_BASE, "**/*.jsonl"), recursive=True)
    log.info("Scanning %d session files...", len(files))

    for fpath in files:
        session_day: str | None = None
        si, so, scc, scr = 0, 0, 0, 0
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue

                    # Extract date from first timestamp in session
                    if not session_day:
                        ts = obj.get("timestamp")
                        if ts:
                            try:
                                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                                if dt.year >= 2026:
                                    session_day = dt.strftime("%Y-%m-%d")
                            except (ValueError, TypeError):
                                pass

                    # Extract usage data from assistant messages
                    msg = obj.get("message")
                    if not isinstance(msg, dict):
                        continue
                    usage = msg.get("usage", {})
                    if usage:
                        si += usage.get("input_tokens", 0)
                        so += usage.get("output_tokens", 0)
                        scc += usage.get("cache_creation_input_tokens", 0)
                        scr += usage.get("cache_read_input_tokens", 0)

            if session_day and (si > 0 or so > 0):
                daily[session_day]["input"] += si
                daily[session_day]["output"] += so
                daily[session_day]["cache_create"] += scc
                daily[session_day]["cache_read"] += scr
                daily[session_day]["sessions"] += 1
        except OSError:
            continue

    # Build sorted list
    days = []
    for date_str in sorted(daily.keys()):
        d = daily[date_str]
        days.append({
            "date": date_str,
            "input": d["input"],
            "output": d["output"],
            "cache_create": d["cache_create"],
            "cache_read": d["cache_read"],
            "sessions": d["sessions"],
        })

    totals = {
        "input": sum(d["input"] for d in days),
        "output": sum(d["output"] for d in days),
        "cache_create": sum(d["cache_create"] for d in days),
        "cache_read": sum(d["cache_read"] for d in days),
        "sessions": sum(d["sessions"] for d in days),
    }

    log.info("Scanned %d sessions across %d days", totals["sessions"], len(days))

    return {
        "days": days,
        "totals": totals,
        "scanned_at": datetime.utcnow().isoformat() + "Z",
    }


def get_token_history() -> dict:
    """Return cached daily token usage, refreshing if stale."""
    global _cached_data, _cached_at

    with _cache_lock:
        now = time.monotonic()
        if _cached_data is not None and (now - _cached_at) < _CACHE_TTL:
            return _cached_data

    # Scan outside the lock (slow operation)
    data = _scan_sessions()

    with _cache_lock:
        _cached_data = data
        _cached_at = time.monotonic()

    return data
