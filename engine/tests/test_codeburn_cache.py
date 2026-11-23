"""Tests for engine.codeburn background refresh bookkeeping."""

import unittest
import unittest.mock as mock

from engine import codeburn


class TestRefreshFlag(unittest.TestCase):
    def test_failed_scan_clears_refresh_flag(self):
        # A scan that raises must not leave the range marked "refreshing",
        # or get_codeburn_report serves the stale report forever.
        key = (9999, "all")
        codeburn._refresh_in_progress.add(key)
        self.addCleanup(codeburn._refresh_in_progress.discard, key)
        with mock.patch.object(codeburn, "_scan_sessions", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                codeburn._compute_and_cache(9999, "all")
        self.assertNotIn(key, codeburn._refresh_in_progress)


if __name__ == "__main__":
    unittest.main()
