"""Local-file usage rollup — replaces the API poller.

Walks ``~/.claude/projects/<encoded>/*.jsonl`` (Claude Code's session
transcripts), sums weighted token usage per 5h/7d window, divides by
configurable plan quotas, and writes a row into ``usage_snapshots``
that's shape-compatible with the now-defunct API path.

Architectural pivot context: ``/api/oauth/usage`` is UA-gated and
hard-throttles non-claude-code callers (429 / Retry-After up to
3600s observed 2026-05-26). The local JSONL transcripts hold the
same usage signal at the source — see memory entries
`claude-usage-systray-ua-gating` and `local-files-over-vendor-api`.

Quota defaults are best-effort Max20 estimates. Override via:
    TOKEN_BUDGET_QUOTA_5H=<weighted_tokens>
    TOKEN_BUDGET_QUOTA_7D=<weighted_tokens>
Calibrate against a known-good API snapshot once and pin.
"""

import glob
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone

from engine.db import UsageDB
from engine.poller import _update_status, _format_remaining, _hours_until, _seed_from_db
from engine.stats import (
    burn_rate,
    cycle_benchmarks,
    pacing_benchmark,
    recommended_daily_budget,
    runway_hours,
    stoppage_detection,
)

log = logging.getLogger("engine.jsonl_rollup")

JSONL_ROOT = os.path.expanduser("~/.claude/projects")
ROLLUP_INTERVAL = 5 * 60  # 5 minutes — local-file walk is cheap

# Weighted-token quotas (Anthropic accounting basis, approximate).
# Override via env var when calibrating against a known-good snapshot.
QUOTA_5H = int(os.environ.get("TOKEN_BUDGET_QUOTA_5H", 50_000_000))
QUOTA_7D = int(os.environ.get("TOKEN_BUDGET_QUOTA_7D", 1_000_000_000))

# Per-token weights (approximate Anthropic rate-limit accounting).
# Input ×1; cache_create ×~1.5 (mix of 5m/1h TTLs); cache_read ×0.1;
# output ×5 (Opus-dominant — Sonnet would be lower but is unused here).
W_INPUT = 1.0
W_CACHE_CREATE = 1.5
W_CACHE_READ = 0.1
W_OUTPUT_OPUS = 5.0
W_OUTPUT_SONNET = 5.0  # same weighting basket; $-cost differs but rate-limit "claude tokens" is per-model-neutral

# 7-day cycle reset anchor. Read from the latest existing snapshot; bumped
# by 7 days each time we cross it. If no snapshot exists, default to next
# Wednesday 10:00 UTC (matches user's observed historical reset cadence).
_DEFAULT_RESET_DOW = 2  # Wed
_DEFAULT_RESET_HOUR_UTC = 10


def _default_7d_reset(now: datetime) -> datetime:
    """Next Wednesday 10:00 UTC at or after ``now``."""
    target = now.replace(hour=_DEFAULT_RESET_HOUR_UTC, minute=0, second=0, microsecond=0)
    days_ahead = (_DEFAULT_RESET_DOW - target.weekday()) % 7
    target += timedelta(days=days_ahead)
    if target <= now:
        target += timedelta(days=7)
    return target


def _resolve_7d_reset_at(db: UsageDB, now: datetime) -> str:
    """Pick the 7d cycle reset timestamp (ISO string).

    Strategy: read the latest snapshot's ``seven_day_resets_at``. If it's
    still in the future, reuse it. If it's in the past, bump by 7-day
    increments until it's in the future. If no snapshot exists, default
    to the next Wednesday 10:00 UTC.
    """
    row = db.get_latest_snapshot()
    if row is None or not row["seven_day_resets_at"]:
        target = _default_7d_reset(now)
        return target.isoformat()
    try:
        target = datetime.fromisoformat(row["seven_day_resets_at"])
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return _default_7d_reset(now).isoformat()
    while target <= now:
        target += timedelta(days=7)
    return target.isoformat()


def _family(model: str) -> str:
    if not model:
        return "unknown"
    m = model.lower()
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    return m


def _weighted(usage: dict, model: str) -> float:
    """Apply rate-limit accounting weights to a single usage record."""
    if not isinstance(usage, dict):
        return 0.0
    fam = _family(model)
    w_out = W_OUTPUT_SONNET if fam == "sonnet" else W_OUTPUT_OPUS
    return (
        (usage.get("input_tokens", 0) or 0) * W_INPUT
        + (usage.get("cache_creation_input_tokens", 0) or 0) * W_CACHE_CREATE
        + (usage.get("cache_read_input_tokens", 0) or 0) * W_CACHE_READ
        + (usage.get("output_tokens", 0) or 0) * w_out
    )


def _parse_ts(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def compute_snapshot(now: datetime | None = None) -> dict:
    """Walk all JSONL transcripts and roll up weighted token totals.

    Returns a dict shape-compatible with the API response:
        {
          "five_hour_util": float (percent 0–100),
          "seven_day_util": float (percent 0–100),
          "sonnet_util": float | None,
          "five_hour_resets_at": iso-string | None,
          "seven_day_resets_at": iso-string,
          "messages_5h": int (debug),
          "messages_7d": int (debug),
        }
    """
    now = now or datetime.now(timezone.utc)
    cutoff_5h = now - timedelta(hours=5)
    cutoff_7d = now - timedelta(days=7)

    weighted_5h = 0.0
    weighted_7d = 0.0
    sonnet_weighted_7d = 0.0
    msgs_5h = 0
    msgs_7d = 0
    oldest_in_5h: datetime | None = None

    pattern = os.path.join(JSONL_ROOT, "*", "*.jsonl")
    for path in glob.glob(pattern):
        try:
            with open(path) as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(rec, dict) or rec.get("type") != "assistant":
                        continue
                    msg = rec.get("message") or {}
                    usage = msg.get("usage")
                    if not usage:
                        continue
                    ts = _parse_ts(rec.get("timestamp"))
                    if ts is None or ts < cutoff_7d:
                        continue
                    model = msg.get("model", "")
                    w = _weighted(usage, model)
                    weighted_7d += w
                    msgs_7d += 1
                    if _family(model) == "sonnet":
                        sonnet_weighted_7d += w
                    if ts >= cutoff_5h:
                        weighted_5h += w
                        msgs_5h += 1
                        if oldest_in_5h is None or ts < oldest_in_5h:
                            oldest_in_5h = ts
        except OSError as exc:
            log.warning("Skipping unreadable JSONL %s: %s", path, exc)
            continue

    five_hour_util = min(weighted_5h / QUOTA_5H * 100.0, 999.0)
    seven_day_util = min(weighted_7d / QUOTA_7D * 100.0, 999.0)
    sonnet_util = (sonnet_weighted_7d / QUOTA_7D * 100.0) if sonnet_weighted_7d else 0.0

    five_hour_resets_at = None
    if oldest_in_5h is not None:
        five_hour_resets_at = (oldest_in_5h + timedelta(hours=5)).isoformat()

    return {
        "five_hour_util": round(five_hour_util, 2),
        "seven_day_util": round(seven_day_util, 2),
        "sonnet_util": round(sonnet_util, 2),
        "five_hour_resets_at": five_hour_resets_at,
        "messages_5h": msgs_5h,
        "messages_7d": msgs_7d,
        "weighted_5h": int(weighted_5h),
        "weighted_7d": int(weighted_7d),
    }


def _daily_avg_this_cycle(db: UsageDB, seven_day_resets_at: str) -> float:
    """Average daily utilisation increase for the current cycle (mirrors poller)."""
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
    return max((last["seven_day_util"] - first["seven_day_util"]) / days_elapsed, 0.0)


def _persist_and_publish(db: UsageDB, snap: dict, now: datetime) -> None:
    """Write the snapshot to the DB and update the in-memory status dict."""
    seven_day_resets_at = _resolve_7d_reset_at(db, now)
    five_hour_resets_at = snap["five_hour_resets_at"]
    sonnet_util = snap["sonnet_util"] if snap["sonnet_util"] > 0 else None

    db.insert_snapshot(
        timestamp=now.isoformat(),
        five_hour_util=snap["five_hour_util"],
        seven_day_util=snap["seven_day_util"],
        sonnet_util=sonnet_util,
        five_hour_resets_at=five_hour_resets_at,
        seven_day_resets_at=seven_day_resets_at,
    )
    db.prune()

    # Projections (mirror poller_loop block; let the dashboard render the HUD).
    recent = db.get_recent_snapshots(limit=50)
    timestamps = [r["timestamp"] for r in reversed(recent)]
    five_utils = [r["five_hour_util"] for r in reversed(recent)]
    seven_utils = [r["seven_day_util"] for r in reversed(recent)]
    br_5h = burn_rate(timestamps, five_utils)
    br_7d = burn_rate(timestamps, seven_utils)

    five_h_remaining = _hours_until(five_hour_resets_at)
    seven_d_remaining = _hours_until(seven_day_resets_at)
    effective_remaining = five_h_remaining if five_hour_resets_at else seven_d_remaining
    rw = runway_hours(snap["five_hour_util"], br_5h, effective_remaining)
    cycle_rw = runway_hours(snap["seven_day_util"], br_7d, seven_d_remaining)
    sd_7d = stoppage_detection(snap["seven_day_util"], br_7d, seven_d_remaining)
    budget = recommended_daily_budget(snap["seven_day_util"], seven_d_remaining)
    pacing = pacing_benchmark(snap["seven_day_util"], seven_d_remaining, 168.0)
    raw_cycles = [
        {"peak_seven_day": r["peak_seven_day"], "stoppage": r["stoppage"]}
        for r in db.get_cycle_peaks()
    ]
    history_bench = cycle_benchmarks(raw_cycles)

    status = {
        "version": 2,
        "current": {
            "five_hour_util": snap["five_hour_util"],
            "seven_day_util": snap["seven_day_util"],
            "sonnet_util": sonnet_util,
            "five_hour_resets_at": five_hour_resets_at,
            "five_hour_resets_in": _format_remaining(five_hour_resets_at),
            "seven_day_resets_at": seven_day_resets_at,
            "seven_day_resets_in": _format_remaining(seven_day_resets_at),
        },
        "projection": {
            "runway_hours": rw,
            "cycle_runway_hours": cycle_rw,
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
        "updated_at": now.isoformat(),
        "source": "jsonl_rollup",  # provenance flag for debugging
    }
    _update_status(status)
    log.info(
        "Rollup: 5h=%.2f%% 7d=%.2f%% msgs=%d/%d (weighted 5h=%d, 7d=%d)",
        snap["five_hour_util"], snap["seven_day_util"],
        snap["messages_5h"], snap["messages_7d"],
        snap["weighted_5h"], snap["weighted_7d"],
    )


def rollup_loop(db: UsageDB, stop_event: threading.Event, interval: int = ROLLUP_INTERVAL) -> None:
    """Periodic rollup loop — runs until ``stop_event`` is set."""
    log.info(
        "JSONL rollup loop starting (interval=%ds, QUOTA_5H=%d, QUOTA_7D=%d)",
        interval, QUOTA_5H, QUOTA_7D,
    )
    # Populate in-memory status from the latest snapshot so /api/status returns
    # immediately while the first compute_snapshot is running (~15-80s).
    _seed_from_db(db)
    while not stop_event.is_set():
        try:
            now = datetime.now(timezone.utc)
            snap = compute_snapshot(now)
            _persist_and_publish(db, snap, now)
        except Exception as exc:  # noqa: BLE001 — keep the loop alive
            log.exception("Rollup iteration failed: %s", exc)
        stop_event.wait(interval)
