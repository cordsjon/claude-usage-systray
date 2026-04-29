"""DeepSeek provider — balance from API + per-call attribution from JSONL log."""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

from engine.providers import Consumer, ProviderState, keychain_get

log = logging.getLogger("engine.providers.deepseek")

_BALANCE_URL = "https://api.deepseek.com/user/balance"
_LOG_PATH = Path.home() / ".local" / "share" / "llm-usage" / "deepseek.jsonl"

# Pricing $/1M tokens — published rates as of 2026-04. Off-peak discount ignored.
_PRICING = {
    "deepseek-chat":     {"input": 0.27, "output": 1.10},
    "deepseek-reasoner": {"input": 0.55, "output": 2.19},
    "deepseek-v4":       {"input": 0.27, "output": 1.10},
    "deepseek-v4-flash": {"input": 0.27, "output": 1.10},
    "_default":          {"input": 0.27, "output": 1.10},
}


def _price(model: str, prompt: int, completion: int) -> float:
    base = (model or "").lower()
    rate = _PRICING.get(base) or next(
        (v for k, v in _PRICING.items() if k != "_default" and k in base),
        _PRICING["_default"],
    )
    return (prompt / 1_000_000) * rate["input"] + (completion / 1_000_000) * rate["output"]


def _fetch_balance(api_key: str) -> tuple[float | None, float | None]:
    """Returns (current_balance, total_topped_up_usd)."""
    req = urllib.request.Request(_BALANCE_URL, headers={"Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=4) as r:
        body = json.loads(r.read().decode("utf-8"))
    infos = body.get("balance_infos") or []
    usd = next((i for i in infos if i.get("currency") == "USD"), None)
    if not usd:
        return None, None
    return float(usd.get("total_balance") or 0), float(usd.get("topped_up_balance") or 0)


def _scan_log(since_ts: float) -> tuple[float, dict[str, float], int]:
    """Return (total_cost, by_project_cost, n_calls) from JSONL since since_ts."""
    if not _LOG_PATH.exists():
        return 0.0, {}, 0
    by_project: dict[str, float] = defaultdict(float)
    total = 0.0
    n = 0
    try:
        with open(_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("ts", 0) < since_ts:
                    continue
                cost = _price(
                    rec.get("model", ""),
                    int(rec.get("prompt_tokens") or 0),
                    int(rec.get("completion_tokens") or 0),
                )
                total += cost
                by_project[rec.get("project") or "unknown"] += cost
                n += 1
    except OSError as exc:
        log.warning("could not read %s: %s", _LOG_PATH, exc)
    return total, dict(by_project), n


class DeepSeekProvider:
    name = "deepseek"

    def state(self, range_days: int) -> ProviderState:
        api_key = keychain_get("llm-usage-deepseek")
        balance, topped_up = (None, None)
        err: str | None = None
        if api_key:
            try:
                balance, topped_up = _fetch_balance(api_key)
            except Exception as exc:
                err = f"balance fetch: {exc}"
        else:
            err = "no api key in keychain (llm-usage-deepseek)"

        since = time.time() - range_days * 86400
        spend, by_project, n_calls = _scan_log(since)

        top: list[Consumer] = []
        if spend > 0:
            for name, cost in sorted(by_project.items(), key=lambda kv: kv[1], reverse=True)[:3]:
                top.append(Consumer(
                    name=name, cost_usd=round(cost, 4),
                    share=round(cost / spend, 4),
                ))

        roi = round(spend / n_calls, 6) if n_calls else None

        return ProviderState(
            name=self.name,
            range_days=range_days,
            balance_usd=balance,
            cap_usd=topped_up,
            spend_usd=round(spend, 4),
            daily_avg_usd=round(spend / range_days, 4) if range_days else 0.0,
            top_consumers=top,
            roi_cost_per_call=roi,
            total_calls=n_calls,
            error=err,
        )
