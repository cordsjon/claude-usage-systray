"""Tests for pure functions in engine.eval_label (US-TB-01 AC-16a).

The interactive TUI loop is not unit-tested; only the pure helpers
(``compute_precision`` and ``build_stratified_sample``) are exercised here.
"""

import unittest

from engine.db import UsageDB
from engine.eval_label import build_stratified_sample, compute_precision


class TestComputePrecision(unittest.TestCase):
    """Verify precision scorer over a list of booleans."""

    def test_compute_precision_empty_returns_none(self):
        self.assertIsNone(compute_precision([]))

    def test_compute_precision_all_true(self):
        self.assertEqual(compute_precision([True, True, True, True]), 1.0)

    def test_compute_precision_mixed(self):
        # 4 true, 1 false => 0.8
        self.assertAlmostEqual(
            compute_precision([True, True, True, False, True]), 0.8
        )


class TestBuildStratifiedSample(unittest.TestCase):
    """Verify the stratified sampler caps per-pattern and surfaces negatives."""

    def setUp(self):
        self.db = UsageDB(":memory:")

    def tearDown(self):
        self.db.close()

    def test_build_stratified_sample_caps_per_pattern(self):
        """30 'produce' rows within window should sample down to 20."""
        for i in range(30):
            self.db.insert_prompt_usage(
                date="2026-04-20",
                session_id=f"s{i}",
                project_dir="/p",
                pattern_id="produce",
                pattern_version=1,
                is_structured=0,
                matched_text=f"produce animals s{i}",
                message_ordinal=i,
            )
        sample = build_stratified_sample(
            self.db, today_iso="2026-04-21", per_pattern_cap=20
        )
        self.assertIn("produce", sample)
        self.assertLessEqual(len(sample["produce"]), 20)
        self.assertEqual(len(sample["produce"]), 20)
        # Each row has the expected shape.
        first = sample["produce"][0]
        self.assertIn("message_id", first)
        self.assertIn("matched_text", first)
        self.assertIn("pattern_version", first)

    def test_build_stratified_sample_includes_negatives_key(self):
        """_negatives bucket is always present and capped at negative_cap."""
        for i in range(5):
            self.db.insert_prompt_unmatched(
                date="2026-04-20",
                session_id=f"s{i}",
                text_excerpt=f"novel prompt {i}",
                message_ordinal=i,
            )
        sample = build_stratified_sample(
            self.db,
            today_iso="2026-04-21",
            per_pattern_cap=20,
            negative_cap=3,
        )
        self.assertIn("_negatives", sample)
        self.assertEqual(len(sample["_negatives"]), 3)
        neg = sample["_negatives"][0]
        self.assertIn("message_id", neg)
        self.assertIn("text_excerpt", neg)


if __name__ == "__main__":
    unittest.main()
