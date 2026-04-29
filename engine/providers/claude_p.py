"""Claude provider — wraps existing codeburn + poller status."""

from __future__ import annotations

import logging
from typing import Optional

from engine.codeburn import get_codeburn_report
from engine.poller import get_current_status
from engine.providers import Consumer, ProviderState

log = logging.getLogger("engine.providers.claude")


class ClaudeProvider:
    name = "claude"

    def state(self, range_days: int) -> ProviderState:
        report = get_codeburn_report(range_days) or {}
        spend = float(report.get("total_cost_usd") or 0.0)
        total_turns = int(report.get("total_turns") or 0)
        projects = report.get("projects") or []

        top: list[Consumer] = []
        if spend > 0:
            for p in projects[:3]:
                cost = float(p.get("cost_usd") or 0)
                top.append(Consumer(
                    name=p.get("name") or "unknown",
                    cost_usd=cost,
                    share=round(cost / spend, 4) if spend else 0.0,
                ))

        # Balance/cap: Claude Code subscriptions don't expose a $ cap; we
        # surface weekly utilization as percent-of-cap proxy. Cap_usd stays
        # None — UI shows utilization gauge instead.
        status = get_current_status() or {}
        weekly_pct: Optional[float] = None
        try:
            weekly_pct = float(status.get("weekly_pct"))
        except (TypeError, ValueError):
            weekly_pct = None

        roi = round(spend / total_turns, 4) if total_turns else None

        return ProviderState(
            name=self.name,
            range_days=range_days,
            balance_usd=None,
            cap_usd=None,
            spend_usd=round(spend, 2),
            daily_avg_usd=round(spend / range_days, 2) if range_days else 0.0,
            top_consumers=top,
            roi_cost_per_call=roi,
            total_calls=total_turns,
            error=None if status else "no poller status yet",
        )
