"""Tests for YAML pattern loader + classify_message (US-TB-01 AC-02)."""

import unittest
from pathlib import Path

from engine.patterns import classify_message, load_patterns


FIXT = Path(__file__).parent / "fixtures" / "patterns_test.yaml"


class TestPatterns(unittest.TestCase):
    def setUp(self):
        self.patterns = load_patterns(FIXT)

    def test_structured_slash_command(self):
        p = classify_message("/lightsout", self.patterns)
        self.assertEqual(p["pattern_id"], "lightsout")
        self.assertTrue(p["is_structured"])

    def test_slash_command_strips_args(self):
        p = classify_message("/sh:spec-panel arg1 arg2", self.patterns)
        self.assertEqual(p["pattern_id"], "sh:spec-panel")

    def test_unstructured_match(self):
        p = classify_message("produce animals s1", self.patterns)
        self.assertEqual(p["pattern_id"], "produce")
        self.assertFalse(p["is_structured"])

    def test_unmatched_returns_none_id(self):
        p = classify_message("no idea what this is", self.patterns)
        self.assertIsNone(p["pattern_id"])


if __name__ == "__main__":
    unittest.main()
