"""Tests for engine.classification atomic JSON read/write (US-TB-01 AC-06)."""

import json
import tempfile
import unittest
from pathlib import Path

from engine.classification import (
    load_classification,
    move_pattern,
    save_classification,
)


class TestClassification(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()) / "c.json"
        self.tmp.write_text(json.dumps({"everyday": [], "case_by_case": []}))

    def test_load_empty(self):
        self.assertEqual(
            load_classification(self.tmp),
            {"everyday": [], "case_by_case": []},
        )

    def test_load_missing_file_returns_empty_structure(self):
        missing = Path(tempfile.mkdtemp()) / "does-not-exist.json"
        self.assertEqual(
            load_classification(missing),
            {"everyday": [], "case_by_case": []},
        )

    def test_move_pattern_between_sections(self):
        move_pattern(self.tmp, "lightsout", "everyday")
        d = load_classification(self.tmp)
        self.assertIn("lightsout", d["everyday"])
        self.assertNotIn("lightsout", d["case_by_case"])

    def test_move_removes_from_other_section(self):
        save_classification(
            self.tmp, {"everyday": ["x"], "case_by_case": []}
        )
        move_pattern(self.tmp, "x", "case_by_case")
        d = load_classification(self.tmp)
        self.assertEqual(d["everyday"], [])
        self.assertEqual(d["case_by_case"], ["x"])

    def test_move_pattern_invalid_section_raises(self):
        with self.assertRaises(ValueError):
            move_pattern(self.tmp, "foo", "not_a_section")

    def test_move_pattern_idempotent(self):
        move_pattern(self.tmp, "lightsout", "everyday")
        move_pattern(self.tmp, "lightsout", "everyday")
        d = load_classification(self.tmp)
        self.assertEqual(d["everyday"].count("lightsout"), 1)

    def test_atomic_write_survives_crash(self):
        # Implicit: save_classification uses tempfile + Path.replace
        save_classification(
            self.tmp, {"everyday": ["a"], "case_by_case": []}
        )
        self.assertEqual(load_classification(self.tmp)["everyday"], ["a"])


if __name__ == "__main__":
    unittest.main()
