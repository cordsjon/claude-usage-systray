"""API polling loop for the token budget engine.

Fetches usage data from the Anthropic OAuth endpoint, persists snapshots,
computes projections and budget recommendations, and exposes thread-safe
shared state for the systray to read.
"""

import json
import logging
import subprocess
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone

from engine.db import UsageDB
from engine.stats import (
    burn_rate,
    cycle_benchmarks,
    pacing_benchmark,
    recommended_daily_budget,
    runway_hours,
    stoppage_detection,
)

log = logging.getLogger("engine.poller")

# ── Thread-safe shared state ─────────────────────────────────────────

_status_lock = threading.Lock()
_current_status: dict = {}

POLL_INTERVAL = 60             # 1 minute — keeps display within ~1% of Anthropic's live banner
BACKOFF_INTERVAL = 15 * 60    # 15 minutes on failure
ZERO_STREAK_THRESHOLD = 3     # consecutive zero responses before requesting refresh


class TokenHolder:
    """Thread-safe mutable token container.

    The Swift parent process can hot-swap the token via POST /api/token
    without restarting the engine.
    """

    def __init__(self, initial_token: str) -> None:
        self._lock = threading.Lock()
        self._token = initial_token
        self._needs_refresh = False

    @property
    def token(self) -> str:
        with self._lock:
            return self._token

    @token.setter
    def token(self, value: str) -> None:
        with self._lock:
            self._token = value
            self._needs_refresh = False
            log.info("Token hot-swapped successfully")

    @property
    def needs_refresh(self) -> bool:
        with self._lock:
            return self._needs_refresh

    def request_refresh(self) -> None:
        with self._lock:
            self._needs_refresh = True

API_URL = "https://api.anthropic.com/api/oauth/usage"


def _read_keychain_token() -> str | None:
    """Read a fresh OAuth token directly from the macOS Keychain.

    This is the same mechanism launcher.sh uses at startup, but available
    at runtime so the poller can self-heal on 401 without waiting for
    an external actor to hot-swap the token.
    """
    try:
        raw = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if raw.returncode != 0 or not raw.stdout.strip():
            log.warning("Keychain read failed (rc=%d)", raw.returncode)
            return None
        creds = json.loads(raw.stdout.strip())
        token = creds.get("claudeAiOauth", {}).get("accessToken")
        if token:
            log.info("Read fresh token from Keychain")
        return token
    except (json.JSONDecodeError, subprocess.TimeoutExpired, OSError) as exc:
        log.warning("Keychain self-heal failed: %s", exc)
        return None


def get_current_status() -> dict:
    """Return the latest status dict (thread-safe copy)."""
    with _status_lock:
        return dict(_current_status)


def _update_status(status: dict) -> None:
    """Replace the shared status dict (thread-safe)."""
    global _current_status
    with _status_lock:
        _current_status = status


# ── Helpers ───────────────────────────────────────────────────────────

def _hours_until(resets_at: str | None) -> float:
    """Return hours from now until an ISO 8601 reset timestamp.

    Returns 0.0 if resets_at is None or already past.
    """
    if not resets_at:
        return 0.0
    try:
        target = datetime.fromisoformat(resets_at)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        delta = (target - datetime.now(timezone.utc)).total_seconds() / 3600.0
        return max(delta, 0.0)
    except (ValueError, TypeError):
        return 0.0


def _format_remaining(resets_at: str | None) -> str | None:
    """Convert an ISO timestamp to a human-readable remaining duration.

    Examples: "3d 2h", "45m", "0m".  Returns None if resets_at is None.
    """
    if not resets_at:
        return None
    hours = _hours_until(resets_at)
    total_minutes = int(hours * 60)
    if total_minutes <= 0:
        return "0m"
    days = total_minutes // (24 * 60)
    remaining_minutes = total_minutes % (24 * 60)
    h = remaining_minutes // 60
    m = remaining_minutes % 60
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if h:
        parts.append(f"{h}h")
    if m or not parts:
        parts.append(f"{m}m")
    return " ".join(parts)


# ── API fetch ─────────────────────────────────────────────────────────

def fetch_usage(token_holder: TokenHolder) -> dict | None:
    """Call the Anthropic OAuth usage endpoint.

    Returns parsed JSON on success, None on any failure.
    Sets token_holder.needs_refresh on 401/403 so the parent can
    hot-swap the token via POST /api/token.
    """
    req = urllib.request.Request(
        API_URL,
        headers={
            "Authorization": f"Bearer {token_holder.token}",
            "anthropic-beta": "oauth-2025-04-20",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            log.warning("HTTP %d — attempting Keychain self-heal", exc.code)
            fresh = _read_keychain_token()
            if fresh and fresh != token_holder.token:
                token_holder.token = fresh
                log.info("Self-healed with fresh Keychain token, retrying")
                return fetch_usage(token_holder)  # one retry with new token
            token_holder.request_refresh()
        elif exc.code == 429:
            log.warning("HTTP 429 — rate limited, backing off")
        else:
            log.error("HTTP %d: %s", exc.code, exc.reason)
        return None
    except Exception as exc:  # noqa: BLE001
        log.error("Fetch error: %s", exc)
        return None


# ── Main loop ─────────────────────────────────────────────────────────

def _seed_from_db(db: UsageDB) -> None:
    """Restore last-known status from DB so restarts don't show 'No data yet'."""
    row = db.get_latest_snapshot()
    if row is None:
        return

    five_hour_util = row["five_hour_util"]
    seven_day_util = row["seven_day_util"]
    sonnet_util = row["sonnet_util"]
    five_hour_resets_at = row["five_hour_resets_at"]
    seven_day_resets_at = row["seven_day_resets_at"]
    timestamp = row["timestamp"]

    status = {
        "version": 2,
        "current": {
            "five_hour_util": five_hour_util,
            "seven_day_util": seven_day_util,
            "sonnet_util": sonnet_util,
            "five_hour_resets_at": five_hour_resets_at,
            "five_hour_resets_in": _format_remaining(five_hour_resets_at),
            "seven_day_resets_at": seven_day_resets_at,
            "seven_day_resets_in": _format_remaining(seven_day_resets_at),
        },
        "projection": {
            "runway_hours": 0.0,
            "burn_rate_per_hour": 0.0,
            "stoppage_likely": False,
            "hours_short": 0.0,
            "projected_util_at_reset": 0.0,
        },
        "budget": {
            "daily_avg_this_cycle": 0.0,
            "recommended_daily": 0.0,
            "days_remaining": 0.0,
            "active_hours_per_day": 0.0,
            "headroom_hours": 0.0,
            "target_at_reset": 0.0,
        },
        "pacing": {},
        "benchmarks": {},
        "updated_at": timestamp,
        "restored_from_db": True,
    }
    _update_status(status)
    log.info(
        "Seeded from DB: 5h=%.1f%% 7d=%.1f%% (snapshot from %s)",
        five_hour_util, seven_day_util, timestamp,
    )


def poll_loop(token_holder: TokenHolder, db: UsageDB, stop_event: threading.Event) -> None:
    """Poll the API until stop_event is set.

    Each iteration fetches usage, persists a snapshot, computes projections,
    and updates the shared status dict.  Backs off on failure.
    """
    _seed_from_db(db)
    zero_streak = 0

    while not stop_event.is_set():
        data = fetch_usage(token_holder)

        if data is None:
            stop_event.wait(BACKOFF_INTERVAL)
            continue

        now = datetime.now(timezone.utc).isoformat()

        # ── Parse API response (supports both nested and flat formats) ──
        five_hour = data.get("five_hour") or {}
        seven_day = data.get("seven_day") or {}
        sonnet_bucket = data.get("seven_day_sonnet") or {}

        if isinstance(five_hour, dict):
            # Nested format: {five_hour: {utilization, resets_at}, ...}
            five_hour_util = five_hour.get("utilization", 0.0) or 0.0
            five_hour_resets_at = five_hour.get("resets_at")
            seven_day_util = seven_day.get("utilization", 0.0) or 0.0
            seven_day_resets_at = seven_day.get("resets_at", now)
            sonnet_util = sonnet_bucket.get("utilization") if sonnet_bucket else None
        else:
            # Legacy flat format: {five_hour_util, seven_day_util, ...}
            five_hour_util = data.get("five_hour_util", 0.0)
            seven_day_util = data.get("seven_day_util", 0.0)
            sonnet_util = data.get("sonnet_util")
            five_hour_resets_at = data.get("five_hour_resets_at")
            seven_day_resets_at = data.get("seven_day_resets_at", now)

        # ── Stale-token detection ──────────────────────────────────
        # When the token expires, the API may return 200 with all
        # zeros instead of a 401.  After ZERO_STREAK_THRESHOLD
        # consecutive all-zero responses, signal for a token refresh.
        # A legitimate Anthropic reset also returns 0% util but always
        # keeps a valid seven_day_resets_at timestamp — so we only treat
        # all-zero as stale when both reset fields are absent.
        all_zero = (
            five_hour_util == 0.0
            and seven_day_util == 0.0
            and not five_hour_resets_at
            and not seven_day_resets_at
        )
        if all_zero:
            zero_streak += 1
            if zero_streak >= ZERO_STREAK_THRESHOLD:
                log.warning(
                    "%d consecutive zero responses — likely stale token",
                    zero_streak,
                )
                token_holder.request_refresh()
                zero_streak = 0
                stop_event.wait(BACKOFF_INTERVAL)
                continue
        else:
            zero_streak = 0

        # ── Persist ───────────────────────────────────────────────
        db.insert_snapshot(
            timestamp=now,
            five_hour_util=five_hour_util,
            seven_day_util=seven_day_util,
            sonnet_util=sonnet_util,
            five_hour_resets_at=five_hour_resets_at,
            seven_day_resets_at=seven_day_resets_at,
        )
        db.prune()

        # ── Compute projections ───────────────────────────────────
        recent = db.get_recent_snapshots(limit=50)
        timestamps = [row["timestamp"] for row in reversed(recent)]
        five_utils = [row["five_hour_util"] for row in reversed(recent)]
        seven_utils = [row["seven_day_util"] for row in reversed(recent)]

        # 5-hour burn rate: drives session runway and stoppage detection
        br_5h = burn_rate(timestamps, five_utils)
        # 7-day burn rate: drives the weekly chart projection
        br_7d = burn_rate(timestamps, seven_utils)

        five_h_remaining = _hours_until(five_hour_resets_at)
        seven_d_remaining = _hours_until(seven_day_resets_at)

        # When the 5-hour window is inactive (no resets_at), fall back
        # to the 7-day remaining time so runway doesn't collapse to 0.
        effective_remaining = five_h_remaining if five_hour_resets_at else seven_d_remaining
        rw = runway_hours(five_hour_util, br_5h, effective_remaining)
        _ = stoppage_detection(five_hour_util, br_5h, five_h_remaining)  # 5h stoppage unused; 7d used below
        sd_7d = stoppage_detection(seven_day_util, br_7d, seven_d_remaining)
        budget = recommended_daily_budget(seven_day_util, seven_d_remaining)

        # ── Benchmarks ────────────────────────────────────────────
        cycle_duration = 168.0  # 7 days in hours
        pacing = pacing_benchmark(
            seven_day_util, seven_d_remaining, cycle_duration
        )

        raw_cycles = [
            {
                "peak_seven_day": row["peak_seven_day"],
                "stoppage": row["stoppage"],
            }
            for row in db.get_cycle_peaks()
        ]
        history_bench = cycle_benchmarks(raw_cycles)

        # ── Build status dict ─────────────────────────────────────
        status = {
            "version": 2,
            "current": {
                "five_hour_util": five_hour_util,
                "seven_day_util": seven_day_util,
                "sonnet_util": sonnet_util,
                "five_hour_resets_at": five_hour_resets_at,
                "five_hour_resets_in": _format_remaining(five_hour_resets_at),
                "seven_day_resets_at": seven_day_resets_at,
                "seven_day_resets_in": _format_remaining(seven_day_resets_at),
            },
            "projection": {
                "runway_hours": rw,
                "burn_rate_per_hour": br_7d,
                "stoppage_likely": sd_7d["stoppage_likely"],
                "hours_short": sd_7d["hours_short"],
                "projected_util_at_reset": sd_7d["projected_util_at_reset"],
            },
            "budget": {
                "daily_avg_this_cycle": _daily_avg_this_cycle(db, seven_day_resets_at),
                "recommended_daily": budget["recommended_daily"],
                "days_remaining": budget["days_remaining"],
                "active_hours_per_day": budget["active_hours_per_day"],
                "headroom_hours": budget["headroom_hours"],
                "target_at_reset": budget["target_at_reset"],
            },
            "pacing": pacing,
            "benchmarks": history_bench,
            "updated_at": now,
        }

        _update_status(status)
        log.debug(
            "Poll OK: 5h=%.1f%% 7d=%.1f%% burn=%.2f%%/h runway=%.1fh daily=%.1f%%",
            five_hour_util, seven_day_util, br_5h, rw,
            budget["recommended_daily"],
        )
        stop_event.wait(POLL_INTERVAL)


def _daily_avg_this_cycle(db: UsageDB, seven_day_resets_at: str) -> float:
    """Compute the average daily utilisation increase for the current cycle."""
    cycle_id = seven_day_resets_at[:10]
    snapshots = db.get_snapshots_by_cycle(cycle_id)
    if len(snapshots) < 2:
        return 0.0
    first = snapshots[0]
    last = snapshots[-1]
    t0 = datetime.fromisoformat(first["timestamp"])
    t1 = datetime.fromisoformat(last["timestamp"])
    days_elapsed = (t1 - t0).total_seconds() / 86400.0
    if days_elapsed <= 0:
        return 0.0
    util_delta = last["seven_day_util"] - first["seven_day_util"]
    return max(util_delta / days_elapsed, 0.0)
