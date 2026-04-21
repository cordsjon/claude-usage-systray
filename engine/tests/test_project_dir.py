"""Tests for decode_project_dir helper (US-TB-01 Definitions)."""

import tempfile
import unittest
from pathlib import Path

from engine.ingest_prompts import decode_project_dir


class TestDecodeProjectDir(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # Mirror a path with hyphens in BOTH the user and the project segment
        (self.tmp / "Users" / "jcords-macmini" / "projects" / "30_SVG-PAINT").mkdir(parents=True)
        (self.tmp / "home" / "alice" / "work").mkdir(parents=True)

    def test_hyphenated_user_and_project(self):
        self.assertEqual(
            decode_project_dir(
                "-Users-jcords-macmini-projects-30_SVG-PAINT", fs_root=self.tmp
            ),
            "/Users/jcords-macmini/projects/30_SVG-PAINT",
        )

    def test_simple_unix_path(self):
        self.assertEqual(
            decode_project_dir("-home-alice-work", fs_root=self.tmp),
            "/home/alice/work",
        )

    def test_live_filesystem_real_project(self):
        """Smoke test against the actual Mac Mini filesystem — only run if path exists."""
        real_root = Path("/Users/jcords-macmini/projects")
        if not real_root.is_dir():
            self.skipTest("live filesystem path not present")
        self.assertEqual(
            decode_project_dir("-Users-jcords-macmini-projects-claude-usage-systray"),
            "/Users/jcords-macmini/projects/claude-usage-systray",
        )

    def test_unknown_prefix_falls_back(self):
        # fs_root points at a dir where '/xyz' does not exist → fallback branch
        out = decode_project_dir("-xyz-something", fs_root=self.tmp)
        self.assertEqual(out, "/xyz/something")


if __name__ == "__main__":
    unittest.main()
