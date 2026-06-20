"""Pure projection math for the token budget engine.

All functions are pure: input -> output, no side effects, no I/O.
Stdlib only (datetime, math).
"""

from datetime import datetime, timezone


def burn_rate(
    timestamps: list[str],
    utils: list[float],
    window_hours: float = 2.0,
    half_life_hours: float = 0.75,
) -> float:
    """Compute burn rate in %/hr via recency-weighted, time-windowed regression.

    Plain equal-weight OLS over a fixed sample count badly lags a bursty
    signal: a long flat plateau (and repeated identical polls) out-votes a
    recent spike by sheer point count, so the slope understates current burn.
    This estimator is robust to that plateau-then-spike shape:

      1. Collapse runs of identical consecutive values — stale repeated polls
         where upstream returned an unchanged number — to one point each, so
         "no new data" stops masquerading as "evidence the rate is flat".
      2. Keep only points within ``window_hours`` of the latest sample — a
         fixed *time* lookback, not a fixed row count — so an irregular poll
         cadence can't stretch the regression across hours-old history.
      3. Fit a weighted least-squares line, weighting each point by recency
         with an exponential ``half_life_hours`` decay.

    Trade-off: a shorter half-life / window tracks bursts faster but
    whipsaws on transient spikes; longer is steadier but lags regime changes.
    Defaults (2h window, 45-min half-life) favour responsiveness since the
    forecast's job is to warn about the *current* trajectory.

    Args:
        timestamps: ISO 8601 UTC strings, chronological.
        utils: Corresponding utilisation percentages.
        window_hours: Time lookback from the most recent sample.
        half_life_hours: Recency-weight half-life.

    Returns:
        Slope in %/hr. Negative means decreasing. 0 if fewer than 2 usable
        points after de-duplication.
    """
    if len(timestamps) < 2:
        return 0.0

    times = [datetime.fromisoformat(ts) for ts in timestamps]

    # 1. Collapse consecutive duplicates (keep the first sample of each run,
    #    so the slope reflects when a value actually changed).
    pts = [(times[0], utils[0])]
    for t, u in zip(times[1:], utils[1:]):
        if u != pts[-1][1]:
            pts.append((t, u))
    if len(pts) < 2:
        return 0.0

    # 2. Fixed time-window lookback from the most recent sample.
    latest = pts[-1][0]
    windowed = [
        (t, u)
        for t, u in pts
        if (latest - t).total_seconds() / 3600.0 <= window_hours
    ]
    if len(windowed) < 2:
        windowed = pts  # window too tight — fall back to full deduped series

    # 3. Recency-weighted least squares: slope =
    #    (Sw*Swxy - Swx*Swy) / (Sw*Swxx - Swx^2)
    t0 = windowed[0][0]
    xs = [(t - t0).total_seconds() / 3600.0 for t, _ in windowed]
    ys = [u for _, u in windowed]
    weights = [
        0.5 ** (((latest - t).total_seconds() / 3600.0) / half_life_hours)
        for t, _ in windowed
    ]

    sw = sum(weights)
    swx = sum(w * x for w, x in zip(weights, xs))
    swy = sum(w * y for w, y in zip(weights, ys))
    swxy = sum(w * x * y for w, x, y in zip(weights, xs, ys))
    swxx = sum(w * x * x for w, x in zip(weights, xs))

    denom = sw * swxx - swx * swx
    if denom == 0:
        return 0.0

    return (sw * swxy - swx * swy) / denom


def runway_hours(current_util: float, burn_rate_per_hour: float, hours_to_reset: float) -> float:
    """Hours until utilisation hits 100% or reset, whichever comes first.

    Args:
        current_util: Current utilisation percentage.
        burn_rate_per_hour: Rate of change in %/hr.
        hours_to_reset: Hours until the next reset window.

    Returns:
        min(hours_to_100%, hours_to_reset). If burn_rate <= 0, returns hours_to_reset.
        Returns 0.0 if already at or over 100%.
    """
    if current_util >= 100.0:
        return 0.0
    if burn_rate_per_hour <= 0:
        return hours_to_reset

    remaining = 100.0 - current_util
    hours_to_exhaust = remaining / burn_rate_per_hour
    return min(hours_to_exhaust, hours_to_reset)


def stoppage_detection(
    current_util: float,
    burn_rate_per_hour: float,
    hours_to_reset: float,
    active_hours_per_day: int = 14,
) -> dict:
    """Predict whether utilisation will exceed 100% before reset.

    The projection extrapolates over remaining *active* hours, not 24/7
    wall-clock time: ``burn_rate_per_hour`` is the rate measured during
    actual usage, so multiplying it by the full calendar window assumes you
    burn tokens while asleep and produces wildly inflated figures. We instead
    spread usage over ``active_hours_per_day`` (matching recommended_daily_budget).

    ``hours_short`` is kept in wall-clock hours (the UI frames it as "Xh
    before the weekly reset"), converting the active-hours exhaust point back
    to calendar time via the active/24 duty cycle.

    Returns:
        {"stoppage_likely": bool, "hours_short": float, "projected_util_at_reset": float}
    """
    # Already over limit — stoppage is now, not future
    if current_util >= 100.0:
        days_remaining = hours_to_reset / 24.0
        active_hours_remaining = days_remaining * active_hours_per_day
        projected = current_util + burn_rate_per_hour * active_hours_remaining
        return {
            "stoppage_likely": True,
            "hours_short": hours_to_reset,
            "projected_util_at_reset": projected,
        }

    days_remaining = hours_to_reset / 24.0
    active_hours_remaining = days_remaining * active_hours_per_day
    projected = current_util + burn_rate_per_hour * active_hours_remaining
    stoppage = projected > 100.0

    if stoppage and burn_rate_per_hour > 0:
        active_hours_to_exhaust = (100.0 - current_util) / burn_rate_per_hour
        # Convert active hours back to wall-clock for the "before reset" framing.
        if active_hours_per_day > 0:
            wallclock_to_exhaust = active_hours_to_exhaust * 24.0 / active_hours_per_day
        else:
            wallclock_to_exhaust = active_hours_to_exhaust
        hours_short = hours_to_reset - wallclock_to_exhaust
    else:
        hours_short = 0.0

    return {
        "stoppage_likely": stoppage,
        "hours_short": max(hours_short, 0.0),
        "projected_util_at_reset": projected,
    }


def recommended_daily_budget(
    current_util: float,
    hours_to_reset: float,
    active_hours_per_day: int = 14,
) -> dict:
    """Pace to reach 98% at reset (not 100%).

    Returns:
        {"recommended_daily": float, "days_remaining": float,
         "active_hours_per_day": int, "headroom_hours": float,
         "target_at_reset": int}
    """
    target = 98
    remaining_util = max(target - current_util, 0.0)
    days_remaining = hours_to_reset / 24.0

    if days_remaining <= 0 or remaining_util <= 0:
        return {
            "recommended_daily": 0.0,
            "days_remaining": days_remaining,
            "active_hours_per_day": active_hours_per_day,
            "headroom_hours": 0.0,
            "target_at_reset": target,
        }

    recommended_daily = remaining_util / days_remaining
    # Headroom: hours in the period that are NOT active usage hours
    total_active_hours = days_remaining * active_hours_per_day
    headroom_hours = hours_to_reset - total_active_hours

    return {
        "recommended_daily": recommended_daily,
        "days_remaining": days_remaining,
        "active_hours_per_day": active_hours_per_day,
        "headroom_hours": headroom_hours,
        "target_at_reset": target,
    }


def pacing_benchmark(
    current_util: float,
    hours_to_reset: float,
    cycle_duration_hours: float = 168.0,
    target: float = 98.0,
) -> dict:
    """Compare current utilisation against the optimal linear ramp.

    The optimal strategy is a linear ramp from 0% to target% over the cycle.
    At any point, optimal_util = (elapsed / total) * target.

    Returns:
        {"optimal_util": float, "delta": float, "pacing": str,
         "efficiency_pct": float, "grade": str}

    pacing: "ahead" | "behind" | "on_pace"
    delta: current - optimal (positive = ahead, negative = behind)
    efficiency_pct: how close current is to optimal (100 = perfect)
    grade: A-F letter grade
    """
    elapsed = max(cycle_duration_hours - hours_to_reset, 0.0)
    if cycle_duration_hours <= 0:
        optimal = target
    else:
        optimal = (elapsed / cycle_duration_hours) * target

    delta = current_util - optimal

    if abs(delta) < 2.0:
        pacing = "on_pace"
    elif delta > 0:
        pacing = "ahead"
    else:
        pacing = "behind"

    # Efficiency: 100% when exactly on pace, drops as you deviate
    if optimal > 0:
        efficiency = max(0.0, 100.0 - abs(delta) / optimal * 100.0)
    else:
        efficiency = 100.0 if current_util == 0 else 0.0

    # Grade based on delta from optimal
    abs_delta = abs(delta)
    if abs_delta < 3.0:
        grade = "A"
    elif abs_delta < 8.0:
        grade = "B"
    elif abs_delta < 15.0:
        grade = "C"
    elif abs_delta < 25.0:
        grade = "D"
    else:
        grade = "F"

    return {
        "optimal_util": round(optimal, 1),
        "delta": round(delta, 1),
        "pacing": pacing,
        "efficiency_pct": round(efficiency, 1),
        "grade": grade,
    }


def cycle_benchmarks(cycles: list[dict], target: float = 98.0) -> dict:
    """Compute personal benchmarks from historical cycle data.

    Args:
        cycles: [{"cycle_id": str, "peak_five_hour": float,
                  "peak_seven_day": float, "stoppage": int}]
        target: ideal peak utilisation at reset.

    Returns:
        {"avg_peak": float, "best_peak": float, "stoppage_rate": float,
         "cycles_total": int, "stoppages": int,
         "wasted_avg": float, "overall_grade": str}

    wasted_avg: average unused capacity per cycle (target - avg_peak).
    """
    if not cycles:
        return {
            "avg_peak": 0.0, "best_peak": 0.0, "stoppage_rate": 0.0,
            "cycles_total": 0, "stoppages": 0,
            "wasted_avg": target, "overall_grade": "N/A",
        }

    n = len(cycles)
    peaks = [c.get("peak_seven_day", 0) for c in cycles]
    stoppages = sum(1 for c in cycles if c.get("stoppage", 0))

    avg_peak = sum(peaks) / n
    best_peak = max(peaks)
    stoppage_rate = stoppages / n * 100.0
    wasted_avg = max(target - avg_peak, 0.0)

    # Overall grade: penalise stoppages and underuse equally
    # Sweet spot = high avg_peak, zero stoppages
    waste_penalty = wasted_avg / target * 50  # 0-50 points lost for waste
    stoppage_penalty = stoppage_rate / 100 * 50  # 0-50 points lost for stoppages
    score = max(0.0, 100.0 - waste_penalty - stoppage_penalty)

    if score >= 85:
        grade = "A"
    elif score >= 70:
        grade = "B"
    elif score >= 55:
        grade = "C"
    elif score >= 40:
        grade = "D"
    else:
        grade = "F"

    return {
        "avg_peak": round(avg_peak, 1),
        "best_peak": round(best_peak, 1),
        "stoppage_rate": round(stoppage_rate, 1),
        "cycles_total": n,
        "stoppages": stoppages,
        "wasted_avg": round(wasted_avg, 1),
        "overall_grade": grade,
    }


def rolling_average(data: list[float], window: int = 144) -> list[float]:
    """Sliding window average, same length as input.

    For positions where fewer than `window` elements precede,
    the average uses all available elements from the start.
    """
    if not data:
        return []

    result = []
    running_sum = 0.0
    for i, val in enumerate(data):
        running_sum += val
        if i >= window:
            running_sum -= data[i - window]
        count = min(i + 1, window)
        result.append(running_sum / count)
    return result


def monthly_rollup(cycles: list[dict]) -> dict:
    """Aggregate cycle data by YYYY-MM.

    Input:  [{"cycle_id": "YYYY-MM-DD", "peak_util": float, "stoppage": int}]
    Output: {"YYYY-MM": {"cycles_completed": int, "avg_peak": float,
             "stoppages": int, "wasted": float}}

    wasted = sum of (100 - peak_util) for each cycle, representing
    unused capacity that could have been consumed.
    """
    if not cycles:
        return {}

    months: dict[str, dict] = {}
    for cycle in cycles:
        month_key = cycle["cycle_id"][:7]  # "YYYY-MM"
        if month_key not in months:
            months[month_key] = {
                "cycles_completed": 0,
                "peak_sum": 0.0,
                "stoppages": 0,
                "wasted": 0.0,
            }
        bucket = months[month_key]
        bucket["cycles_completed"] += 1
        bucket["peak_sum"] += cycle["peak_util"]
        bucket["stoppages"] += cycle["stoppage"]
        bucket["wasted"] += 100.0 - cycle["peak_util"]

    # Finalize: compute avg_peak, drop internal peak_sum
    result = {}
    for key, bucket in months.items():
        result[key] = {
            "cycles_completed": bucket["cycles_completed"],
            "avg_peak": bucket["peak_sum"] / bucket["cycles_completed"],
            "stoppages": bucket["stoppages"],
            "wasted": bucket["wasted"],
        }
    return result
