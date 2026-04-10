"""Tests for engine.stats — pure projection math functions."""

import unittest
from datetime import datetime, timedelta, timezone

from engine.stats import (
    burn_rate,
    monthly_rollup,
    recommended_daily_budget,
    rolling_average,
    runway_hours,
    stoppage_detection,
)


class TestBurnRate(unittest.TestCase):
    """burn_rate: OLS slope in %/hr from timestamped utilisation samples."""

    def test_constant_increase_5pct_per_hour(self):
        """12 hourly samples increasing 5%/hr should yield ~5.0."""
        base = datetime(2026, 3, 26, 0, 0, 0, tzinfo=timezone.utc)
        timestamps = [(base + timedelta(hours=i)).isoformat() for i in range(12)]
        utils = [10.0 + 5.0 * i for i in range(12)]
        result = burn_rate(timestamps, utils)
        self.assertAlmostEqual(result, 5.0, places=2)

    def test_no_change(self):
        """Flat utilisation should yield 0."""
        base = datetime(2026, 3, 26, 0, 0, 0, tzinfo=timezone.utc)
        timestamps = [(base + timedelta(hours=i)).isoformat() for i in range(5)]
        utils = [50.0] * 5
        result = burn_rate(timestamps, utils)
        self.assertAlmostEqual(result, 0.0, places=4)

    def test_decreasing(self):
        """Decreasing utilisation should yield a negative slope."""
        base = datetime(2026, 3, 26, 0, 0, 0, tzinfo=timezone.utc)
        timestamps = [(base + timedelta(hours=i)).isoformat() for i in range(6)]
        utils = [80.0 - 3.0 * i for i in range(6)]
        result = burn_rate(timestamps, utils)
        self.assertAlmostEqual(result, -3.0, places=2)

    def test_single_point_returns_zero(self):
        """A single data point cannot define a slope."""
        result = burn_rate(["2026-03-26T00:00:00+00:00"], [42.0])
        self.assertEqual(result, 0.0)

    def test_empty_returns_zero(self):
        """No data should return 0."""
        result = burn_rate([], [])
        self.assertEqual(result, 0.0)


class TestRunwayHours(unittest.TestCase):
    """runway_hours: hours until 100% or reset, whichever comes first."""

    def test_positive_burn_exhausts_before_reset(self):
        """At 80% with 10%/hr burn and 8h to reset, hits 100% in 2h."""
        result = runway_hours(80.0, 10.0, 8.0)
        self.assertAlmostEqual(result, 2.0, places=2)

    def test_zero_burn_returns_hours_to_reset(self):
        result = runway_hours(50.0, 0.0, 6.0)
        self.assertEqual(result, 6.0)

    def test_negative_burn_returns_hours_to_reset(self):
        result = runway_hours(50.0, -2.0, 6.0)
        self.assertEqual(result, 6.0)

    def test_exhaustion_after_reset_capped(self):
        """At 90% with 1%/hr burn and 5h to reset, would exhaust in 10h — capped to 5."""
        result = runway_hours(90.0, 1.0, 5.0)
        self.assertAlmostEqual(result, 5.0, places=2)


class TestStoppageDetection(unittest.TestCase):
    """stoppage_detection: predict whether utilisation will hit 100% before reset."""

    def test_stoppage_detected(self):
        """At 80% with 10%/hr and 4h to reset — projected 120% at reset."""
        result = stoppage_detection(80.0, 10.0, 4.0)
        self.assertTrue(result["stoppage_likely"])
        self.assertGreater(result["hours_short"], 0)
        self.assertGreater(result["projected_util_at_reset"], 100.0)

    def test_no_stoppage(self):
        """At 30% with 2%/hr and 4h to reset — projected 38%."""
        result = stoppage_detection(30.0, 2.0, 4.0)
        self.assertFalse(result["stoppage_likely"])
        self.assertEqual(result["hours_short"], 0.0)
        self.assertLess(result["projected_util_at_reset"], 100.0)

    def test_zero_burn_no_stoppage(self):
        result = stoppage_detection(50.0, 0.0, 8.0)
        self.assertFalse(result["stoppage_likely"])
        self.assertEqual(result["hours_short"], 0.0)
        self.assertAlmostEqual(result["projected_util_at_reset"], 50.0)


class TestRecommendedDailyBudget(unittest.TestCase):
    """recommended_daily_budget: pace to reach 98% at reset."""

    def test_basic_budget(self):
        """At 20% with 48h to reset and 14 active h/day, remaining = 78%."""
        result = recommended_daily_budget(20.0, 48.0)
        self.assertIn("recommended_daily", result)
        self.assertIn("days_remaining", result)
        self.assertIn("active_hours_per_day", result)
        self.assertIn("headroom_hours", result)
        self.assertEqual(result["target_at_reset"], 98)
        self.assertGreater(result["recommended_daily"], 0)
        self.assertAlmostEqual(result["days_remaining"], 2.0, places=1)

    def test_zero_remaining(self):
        """At 98%+ there is nothing left to spend."""
        result = recommended_daily_budget(99.0, 24.0)
        self.assertEqual(result["recommended_daily"], 0.0)

    def test_less_than_one_day(self):
        """With <24h to reset, days_remaining < 1."""
        result = recommended_daily_budget(50.0, 10.0)
        self.assertLess(result["days_remaining"], 1.0)
        self.assertGreater(result["recommended_daily"], 0)


class TestRollingAverage(unittest.TestCase):
    """rolling_average: sliding window mean, same length as input."""

    def test_window_smaller_than_data(self):
        data = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = rolling_average(data, window=3)
        self.assertEqual(len(result), 5)
        # First two elements: partial windows
        self.assertAlmostEqual(result[0], 1.0)
        self.assertAlmostEqual(result[1], 1.5)
        # Full window from index 2 onward
        self.assertAlmostEqual(result[2], 2.0)
        self.assertAlmostEqual(result[3], 3.0)
        self.assertAlmostEqual(result[4], 4.0)

    def test_window_equals_data(self):
        data = [2.0, 4.0, 6.0]
        result = rolling_average(data, window=3)
        self.assertEqual(len(result), 3)
        self.assertAlmostEqual(result[-1], 4.0)

    def test_empty(self):
        result = rolling_average([])
        self.assertEqual(result, [])


class TestMonthlyRollup(unittest.TestCase):
    """monthly_rollup: aggregate cycle data by month."""

    def test_basic_four_cycles_one_month(self):
        cycles = [
            {"cycle_id": "2026-03-01", "peak_util": 95.0, "stoppage": 1},
            {"cycle_id": "2026-03-06", "peak_util": 88.0, "stoppage": 0},
            {"cycle_id": "2026-03-11", "peak_util": 92.0, "stoppage": 1},
            {"cycle_id": "2026-03-16", "peak_util": 80.0, "stoppage": 0},
        ]
        result = monthly_rollup(cycles)
        self.assertIn("2026-03", result)
        month = result["2026-03"]
        self.assertEqual(month["cycles_completed"], 4)
        self.assertAlmostEqual(month["avg_peak"], 88.75)
        self.assertEqual(month["stoppages"], 2)
        self.assertIn("wasted", month)

    def test_empty(self):
        result = monthly_rollup([])
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
