"""Provider abstraction for the multi-LLM Overview tab.

Each provider returns a uniform ProviderState so the dashboard can render
Claude / OpenAI / DeepSeek (and future additions) in one grid.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol

log = logging.getLogger("engine.providers")

# Cache: in-memory TTL + on-disk fallback (survives engine restart)
_OVERVIEW_TTL = 60.0          # in-memory freshness
_DISK_TTL = 600.0             # 10 min — disk fallback used if in-memory missing
_DISK_DIR = Path.home() / ".cache" / "llm-overview"

_cache_lock = threading.Lock()
_mem_cache: dict[int, tuple[float, dict]] = {}     # days -> (expires_at, payload)
_refresh_in_progress: set[int] = set()


@dataclass
class Consumer:
    name: str
    cost_usd: float
    share: float


@dataclass
class ProviderState:
    name: str
    range_days: int
    balance_usd: Optional[float] = None
    cap_usd: Optional[float] = None
    spend_usd: float = 0.0
    daily_avg_usd: float = 0.0
    top_consumers: list[Consumer] = field(default_factory=list)
    roi_cost_per_call: Optional[float] = None
    total_calls: int = 0
    updated_at: float = 0.0
    error: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["top_consumers"] = [asdict(c) for c in self.top_consumers]
        return d


class Provider(Protocol):
    name: str

    def state(self, range_days: int) -> ProviderState: ...


def keychain_get(service: str) -> Optional[str]:
    """Read a generic password from macOS Keychain. Returns None on miss."""
    try:
        out = subprocess.check_output(
            ["security", "find-generic-password", "-s", service, "-w"],
            stderr=subprocess.DEVNULL,
            timeout=3,
        )
        return out.decode("utf-8").strip() or None
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None


def fetch_all(providers: list[Provider], range_days: int, timeout: float = 12.0) -> list[ProviderState]:
    """Fetch all providers in parallel. Each provider that times out or
    raises is degraded to a ProviderState with `error` set; never bubbles up."""
    results: list[ProviderState] = []

    def _safe(p: Provider) -> ProviderState:
        try:
            s = p.state(range_days)
            s.updated_at = time.time()
            return s
        except Exception as exc:
            log.warning("Provider %s failed: %s", p.name, exc)
            return ProviderState(
                name=p.name, range_days=range_days,
                error=f"{type(exc).__name__}: {exc}",
                updated_at=time.time(),
            )

    with ThreadPoolExecutor(max_workers=len(providers) or 1) as ex:
        futures = {ex.submit(_safe, p): p for p in providers}
        for fut, p in futures.items():
            try:
                results.append(fut.result(timeout=timeout))
            except FuturesTimeout:
                results.append(ProviderState(
                    name=p.name, range_days=range_days,
                    error=f"timeout after {timeout}s",
                    updated_at=time.time(),
                ))
    return results


# ---------------------------------------------------------------------------
# Cached overview report (in-memory + disk + background refresh)
# ---------------------------------------------------------------------------


def _disk_path(days: int) -> Path:
    return _DISK_DIR / f"overview-{days}.json"


def _disk_write(days: int, payload: dict) -> None:
    try:
        _DISK_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=_DISK_DIR, suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        Path(tmp).replace(_disk_path(days))
    except OSError as exc:
        log.warning("disk write failed for %dd: %s", days, exc)


def _disk_read(days: int) -> Optional[tuple[float, dict]]:
    """Return (mtime, payload) if file exists and is readable; else None."""
    p = _disk_path(days)
    try:
        mtime = p.stat().st_mtime
        with open(p, "r", encoding="utf-8") as f:
            return mtime, json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _build_overview(days: int, range_key: str) -> dict:
    """Synchronously fetch all providers and build the overview payload."""
    # Lazy imports to avoid circular: providers/__init__ ← provider modules ← engine.codeburn etc.
    from engine.providers.claude_p import ClaudeProvider
    from engine.providers.deepseek_p import DeepSeekProvider
    from engine.providers.openai_p import OpenAIProvider

    providers = [ClaudeProvider(), OpenAIProvider(), DeepSeekProvider()]
    states = fetch_all(providers, days, timeout=12.0)
    return {
        "version": 1,
        "range": range_key,
        "range_days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "providers": [s.to_dict() for s in states],
    }


def _refresh_async(days: int, range_key: str) -> None:
    """Background-refresh the cache for `days` if not already in flight."""
    with _cache_lock:
        if days in _refresh_in_progress:
            return
        _refresh_in_progress.add(days)

    def _run():
        try:
            payload = _build_overview(days, range_key)
            with _cache_lock:
                _mem_cache[days] = (time.time() + _OVERVIEW_TTL, payload)
            _disk_write(days, payload)
            log.info("overview %dd refreshed in background", days)
        except Exception as exc:
            log.warning("overview %dd refresh failed: %s", days, exc)
        finally:
            with _cache_lock:
                _refresh_in_progress.discard(days)

    threading.Thread(target=_run, daemon=True).start()


def get_overview(days: int, range_key: str) -> dict:
    """Cached multi-provider overview. Mirrors get_codeburn_report():
    fast in-memory hit -> stale-but-served disk hit (kicks off background refresh)
    -> synchronous fetch as last resort."""
    now = time.time()

    with _cache_lock:
        cached = _mem_cache.get(days)
    if cached and cached[0] > now:
        return cached[1]

    # Disk fallback — serve immediately and refresh in background
    disk = _disk_read(days)
    if disk is not None:
        mtime, payload = disk
        if now - mtime < _DISK_TTL:
            with _cache_lock:
                _mem_cache[days] = (now + _OVERVIEW_TTL, payload)
            return payload
        # Stale disk — serve it but trigger refresh
        _refresh_async(days, range_key)
        return payload

    # Cold path — synchronous fetch
    payload = _build_overview(days, range_key)
    with _cache_lock:
        _mem_cache[days] = (now + _OVERVIEW_TTL, payload)
    _disk_write(days, payload)
    return payload


def warm_overview_cache(ranges: list[tuple[int, str]]) -> None:
    """Populate the cache for given (days, range_key) pairs at startup."""
    for days, key in ranges:
        try:
            get_overview(days, key)
            log.info("overview cache warmed: %dd", days)
        except Exception as exc:
            log.warning("overview warmup %dd failed: %s", days, exc)
