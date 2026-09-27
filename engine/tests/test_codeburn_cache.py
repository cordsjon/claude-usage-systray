"""Tests for engine.codeburn background refresh bookkeeping."""

import unittest
import unittest.mock as mock

from engine import codeburn


class TestRefreshFlag(unittest.TestCase):
    def test_failed_scan_clears_refresh_flag(self):
        # A scan that raises must not leave the range marked "refreshing",
        # or get_codeburn_report serves the stale report forever.
        codeburn._refresh_in_progress.add(9999)
        self.addCleanup(codeburn._refresh_in_progress.discard, 9999)
        with mock.patch.object(codeburn, "_scan_sessions", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                codeburn._compute_and_cache(9999)
        self.assertNotIn(9999, codeburn._refresh_in_progress)


if __name__ == "__main__":
    unittest.main()
