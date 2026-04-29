"""OpenAI provider — admin API: costs (with project grouping) + usage completions."""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from collections import defaultdict

from engine.providers import Consumer, ProviderState, keychain_get

log = logging.getLogger("engine.providers.openai")

_BASE = "https://api.openai.com/v1/organization"


def _get(api_key: str, path: str, **params) -> dict:
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{_BASE}/{path}?{qs}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def _sum_costs_by_project(buckets: list[dict]) -> tuple[float, dict[str, float]]:
    by_project: dict[str, float] = defaultdict(float)
    total = 0.0
    for b in buckets:
        for r in b.get("results") or []:
            amt = float((r.get("amount") or {}).get("value") or 0)
            name = r.get("project_name") or r.get("project_id") or "—"
            total += amt
            by_project[name] += amt
    return total, dict(by_project)


def _sum_calls(buckets: list[dict]) -> int:
    n = 0
    for b in buckets:
        for r in b.get("results") or []:
            n += int(r.get("num_model_requests") or 0)
    return n


class OpenAIProvider:
    name = "openai"

    def state(self, range_days: int) -> ProviderState:
        api_key = keychain_get("llm-usage-openai-admin")
        if not api_key:
            return ProviderState(
                name=self.name, range_days=range_days,
                error="no api key in keychain (llm-usage-openai-admin)",
            )

        start_ts = int(time.time() - range_days * 86400)

        try:
            costs = _get(
                api_key, "costs",
                start_time=start_ts, bucket_width="1d",
                group_by="project_id", limit=range_days,
            )
            usage = _get(
                api_key, "usage/completions",
                start_time=start_ts, bucket_width="1d", limit=range_days,
            )
        except Exception as exc:
            return ProviderState(
                name=self.name, range_days=range_days,
                error=f"api: {exc}",
            )

        spend, by_project = _sum_costs_by_project(costs.get("data") or [])
        n_calls = _sum_calls(usage.get("data") or [])

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
            balance_usd=None,  # admin API doesn't expose credit balance
            cap_usd=None,
            spend_usd=round(spend, 4),
            daily_avg_usd=round(spend / range_days, 4) if range_days else 0.0,
            top_consumers=top,
            roi_cost_per_call=roi,
            total_calls=n_calls,
            error=None,
        )
