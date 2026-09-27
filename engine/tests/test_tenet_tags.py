"""Tests for engine.codeburn [TENET: ...] tag normalization.

Real tag bodies seen in transcripts (2026-09-27 audit): compound lists,
trailing rationale after an em dash, and case/spacing variants of one rule.
"""

import tempfile
import unittest
from pathlib import Path

from engine.codeburn import _load_known_tenets, _normalize_tenet_tag

KNOWN = ["mva", "do-no-harm", "abstract-on-third", "fix-what-you-find",
         "happy-path-first", "source-transparency"]


class TestNormalizeTenetTag(unittest.TestCase):
    def test_compound_list_yields_each_known_tenet(self):
        self.assertEqual(_normalize_tenet_tag("do-no-harm, mva", KNOWN),
                         ["do-no-harm", "mva"])
        self.assertEqual(_normalize_tenet_tag("`do-no-harm` / `abstract-on-third`", KNOWN),
                         ["do-no-harm", "abstract-on-third"])

    def test_rationale_is_dropped(self):
        self.assertEqual(
            _normalize_tenet_tag("abstract-on-third — five call sites is past the threshold", KNOWN),
            ["abstract-on-third"])
        self.assertEqual(_normalize_tenet_tag("happy-path-first, but the race is the requirement", KNOWN),
                         ["happy-path-first"])

    def test_placeholders_are_not_citations(self):
        self.assertEqual(_normalize_tenet_tag("...", KNOWN), [])
        self.assertEqual(_normalize_tenet_tag("<name>", KNOWN), [])

    def test_case_folds_to_known_slug(self):
        self.assertEqual(_normalize_tenet_tag("MVA", KNOWN), ["mva"])

    def test_unknown_rule_spacing_variants_merge(self):
        a = _normalize_tenet_tag("never mock the seam under test", KNOWN)
        b = _normalize_tenet_tag("never-mock-the-seam-under-test", KNOWN)
        self.assertEqual(a, b)
        self.assertEqual(a, ["never-mock-the-seam-under-test"])

    def test_unknown_rule_rationale_is_dropped(self):
        self.assertEqual(
            _normalize_tenet_tag("never mock the seam under test — imported the real package", KNOWN),
            ["never-mock-the-seam-under-test"])


class TestLoadKnownTenets(unittest.TestCase):
    def test_reads_numbered_and_bulleted_bold_slugs(self):
        d = Path(tempfile.mkdtemp())
        (d / "TENETS.md").write_text("1. **architecture-first** — design.\n2. **MVA** — small.\n")
        (d / "PHILOSOPHY.md").write_text("- **delete-first** — x.\n- **Distill, don't shotgun** — y.\n")
        self.assertEqual(sorted(_load_known_tenets(d)),
                         ["architecture-first", "delete-first", "mva"])

    def test_missing_files_give_empty_set(self):
        self.assertEqual(_load_known_tenets(Path(tempfile.mkdtemp())), set())


if __name__ == "__main__":
    unittest.main()
