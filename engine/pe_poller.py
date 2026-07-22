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
