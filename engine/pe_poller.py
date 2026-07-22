# engine/pe_poller.py
"""PosterEngine (PE) supervisor poller.

Polls both configured PE instances for job-queue health and router spend,
persists snapshots, computes stall/budget alerts, and exposes thread-safe
shared state for engine/api.py's /pe/status route.

Clones the fetch/poll-loop shape of engine/poller.py (stdlib urllib,
two-tuple (data, error) fetch contract, stop_event-interruptible sleep).
"""

import json
import logging
import urllib.error
import urllib.request

from engine.pe_config import PEInstance

log = logging.getLogger("engine.pe_poller")

JOBS_SUMMARY_TIMEOUT = 5
ROUTER_METRICS_TIMEOUT = 5


def _fetch_json(url: str, token: str, timeout: int):
    """GET url with a Bearer token. Returns (parsed_json_or_None, error_str_or_None)."""
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}, method="GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        log.warning("PE fetch %s -> HTTP %s", url, e.code)
        return None, f"http_{e.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("PE fetch %s -> %s", url, e)
        return None, str(e)
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("PE fetch %s -> bad JSON: %s", url, e)
        return None, "bad_json"


def fetch_jobs_summary(instance: PEInstance, token: str, timeout: int = JOBS_SUMMARY_TIMEOUT):
    return _fetch_json(f"{instance.base_url}/api/jobs/summary", token, timeout)


def fetch_router_metrics(instance: PEInstance, token: str, timeout: int = ROUTER_METRICS_TIMEOUT):
    return _fetch_json(f"{instance.base_url}/api/admin/router-metrics", token, timeout)


import threading
from datetime import datetime, timezone

STALL_THRESHOLD_S = 180
BUDGET_REARM_FRACTION = 0.90
PE_POLL_INTERVAL_JOBS_S = 30
PE_POLL_INTERVAL_ROUTER_S = 60
UNREACHABLE_MISS_THRESHOLD = 3

_pe_status_lock = threading.Lock()
_current_pe_status: dict = {}
_miss_counts: dict = {}


def get_current_pe_status() -> dict:
    with _pe_status_lock:
        return dict(_current_pe_status)


def _update_pe_status(instance_name: str, status: dict) -> None:
    with _pe_status_lock:
        _current_pe_status[instance_name] = status


def compute_stalled(oldest_claimable_queued_s: int, running: int) -> bool:
    return oldest_claimable_queued_s > STALL_THRESHOLD_S and running == 0


def compute_budget_crossed(cost_24h_usd: float, budget_24h_usd: float, currently_crossed: bool) -> bool:
    """Edge-triggered with hysteresis: activates at >= target, re-arms below 90% of target."""
    if not currently_crossed:
        return cost_24h_usd >= budget_24h_usd
    rearm_floor = budget_24h_usd * BUDGET_REARM_FRACTION
    return cost_24h_usd >= rearm_floor


def make_alert_id(kind: str, instance: str, first_seen: str | None = None, job_id: str | None = None) -> str:
    if kind == "dead" or kind == "op_failed":
        return f"{kind}:{instance}:{job_id}"
    return f"{kind}:{instance}:{first_seen}"


def pe_poll_once(
    instance: PEInstance, db, token: str, now_iso: str | None = None,
    fetch_router: bool = True,
) -> None:
    """Run one poll iteration for a single instance.

    Split out from the threaded loop so tests can call it directly without
    threading or real sleeps. `fetch_router=False` skips the router-metrics
    call (used by pe_poll_loop to honor the 30s/60s split cadence — jobs
    summary polls every iteration, router metrics only every Nth) while still
    updating status/stall state from the jobs summary alone. When skipped,
    the previously-persisted cost figures are NOT touched — /pe/status keeps
    showing the last real reading rather than zeroing it out between router
    polls.
    """
    now = now_iso or datetime.now(timezone.utc).isoformat()

    summary, summary_err = fetch_jobs_summary(instance, token)
    metrics, metrics_err = fetch_router_metrics(instance, token) if fetch_router else (None, None)

    if summary is None:
        _miss_counts[instance.name] = _miss_counts.get(instance.name, 0) + 1
    else:
        _miss_counts[instance.name] = 0

    reachable = _miss_counts.get(instance.name, 0) < UNREACHABLE_MISS_THRESHOLD

    if metrics is not None:
        db.insert_pe_cost_snapshot(
            ts=now, instance=instance.name,
            cost_24h_usd=metrics.get("cost_24h_usd", 0.0) if metrics.get("available") else 0.0,
            calls=metrics.get("calls", 0) if metrics.get("available") else 0,
            available=bool(metrics.get("available")),
        )
        cost_24h_display = metrics.get("cost_24h_usd", 0.0) if metrics.get("available") else 0.0
        calls_display = metrics.get("calls", 0) if metrics.get("available") else 0
        available_display = bool(metrics.get("available"))
    else:
        # Router wasn't polled this iteration (fetch_router=False) or the
        # fetch failed — fall back to the last persisted snapshot instead of
        # showing 0/unavailable, which would flicker the popover every cycle
        # the router isn't due to be polled.
        last_snapshot = db.get_latest_pe_cost_snapshot(instance.name)
        cost_24h_display = last_snapshot["cost_24h_usd"] if last_snapshot else 0.0
        calls_display = last_snapshot["calls"] if last_snapshot else 0
        available_display = bool(last_snapshot["available"]) if last_snapshot else False

    counts = (summary or {}).get("counts", {})
    oldest_claimable = (summary or {}).get("oldest_claimable_queued_s", 0)
    running = counts.get("running", 0)
    stalled = compute_stalled(oldest_claimable, running) if summary is not None else False

    status = {
        "reachable": reachable,
        "counts": counts,
        "oldest_claimable_queued_s": oldest_claimable,
        "stalled": stalled,
        "recent_terminal": (summary or {}).get("recent_terminal", []),
        "cost": {
            "d24h_usd": cost_24h_display,
            "calls": calls_display,
            "available": available_display,
        },
        "budget": {
            "target_24h_usd": instance.budget_24h_usd,
            "crossed": False,  # computed by caller with persisted currently_crossed state
        },
        "last_poll": now,
    }
    _update_pe_status(instance.name, status)
