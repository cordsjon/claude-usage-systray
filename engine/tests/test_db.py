"""Tests for the SQLite database layer."""

import unittest
from datetime import datetime, timedelta, timezone

from engine.db import UsageDB


class TestSchema(unittest.TestCase):
    """Verify table, indexes, and WAL mode."""

    def setUp(self):
        self.db = UsageDB(":memory:")

    def tearDown(self):
        self.db.close()

    def test_table_exists(self):
        cur = self.db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='usage_snapshots'"
        )
        self.assertIsNotNone(cur.fetchone())

    def test_wal_mode(self):
        # :memory: DBs silently ignore WAL, so just verify the pragma runs
        cur = self.db._conn.execute("PRAGMA journal_mode")
        mode = cur.fetchone()[0]
        # memory always returns 'memory', file-based would return 'wal'
        self.assertIn(mode, ("wal", "memory"))

    def test_indexes_created(self):
        cur = self.db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"
        )
        names = {row[0] for row in cur.fetchall()}
        self.assertIn("idx_snapshots_timestamp", names)
        self.assertIn("idx_snapshots_cycle_id", names)


class TestWrite(unittest.TestCase):
    """Verify insert_snapshot behavior."""

    def setUp(self):
        self.db = UsageDB(":memory:")

    def tearDown(self):
        self.db.close()

    def test_insert_snapshot(self):
        self.db.insert_snapshot(
            timestamp="2026-03-26T10:00:00Z",
            five_hour_util=0.45,
            seven_day_util=0.30,
            sonnet_util=0.10,
            five_hour_resets_at="2026-03-26T15:00:00Z",
            seven_day_resets_at="2026-03-27T00:00:00Z",
        )
        rows = self.db.get_recent_snapshots(limit=1)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["five_hour_util"], 0.45)
        self.assertAlmostEqual(rows[0]["seven_day_util"], 0.30)

    def test_cycle_id_derived_from_seven_day_resets_at(self):
        self.db.insert_snapshot(
            timestamp="2026-03-26T10:00:00Z",
            five_hour_util=0.5,
            seven_day_util=0.5,
            sonnet_util=None,
            five_hour_resets_at=None,
            seven_day_resets_at="2026-03-27T00:00:00Z",
        )
        rows = self.db.get_recent_snapshots(limit=1)
        self.assertEqual(rows[0]["cycle_id"], "2026-03-27")

    def test_null_sonnet_handled(self):
        self.db.insert_snapshot(
            timestamp="2026-03-26T10:00:00Z",
            five_hour_util=0.5,
            seven_day_util=0.5,
            sonnet_util=None,
            five_hour_resets_at=None,
            seven_day_resets_at="2026-03-27T00:00:00Z",
        )
        rows = self.db.get_recent_snapshots(limit=1)
        self.assertIsNone(rows[0]["sonnet_util"])


class TestPrune(unittest.TestCase):
    """Verify retention logic."""

    def setUp(self):
        self.db = UsageDB(":memory:")

    def tearDown(self):
        self.db.close()

    def _insert_at(self, ts_str):
        self.db.insert_snapshot(
            timestamp=ts_str,
            five_hour_util=0.5,
            seven_day_util=0.5,
            sonnet_util=None,
            five_hour_resets_at=None,
            seven_day_resets_at="2026-03-27T00:00:00Z",
        )

    def test_prune_removes_old(self):
        old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        recent = datetime.now(timezone.utc).isoformat()
        self._insert_at(old)
        self._insert_at(recent)
        deleted = self.db.prune()
        self.assertEqual(deleted, 1)
        rows = self.db.get_recent_snapshots(limit=10)
        self.assertEqual(len(rows), 1)

    def test_prune_retains_boundary(self):
        # 354 days ago (within 355-day window) should be retained
        boundary = (datetime.now(timezone.utc) - timedelta(days=354)).isoformat()
        self._insert_at(boundary)
        deleted = self.db.prune()
        self.assertEqual(deleted, 0)
        rows = self.db.get_recent_snapshots(limit=10)
        self.assertEqual(len(rows), 1)


class TestRead(unittest.TestCase):
    """Verify query methods."""

    def setUp(self):
        self.db = UsageDB(":memory:")
        # Insert snapshots across two cycles
        for i in range(5):
            self.db.insert_snapshot(
                timestamp=f"2026-03-25T{10+i:02d}:00:00Z",
                five_hour_util=0.1 * (i + 1),
                seven_day_util=0.05 * (i + 1),
                sonnet_util=None,
                five_hour_resets_at=None,
                seven_day_resets_at="2026-03-26T00:00:00Z",
            )
        for i in range(3):
            self.db.insert_snapshot(
                timestamp=f"2026-03-26T{10+i:02d}:00:00Z",
                five_hour_util=0.2 * (i + 1),
                seven_day_util=0.1 * (i + 1),
                sonnet_util=None,
                five_hour_resets_at=None,
                seven_day_resets_at="2026-03-27T00:00:00Z",
            )

    def tearDown(self):
        self.db.close()

    def test_get_snapshots_by_cycle(self):
        rows = self.db.get_snapshots_by_cycle("2026-03-26")
        self.assertEqual(len(rows), 5)

    def test_get_recent_snapshots_order(self):
        rows = self.db.get_recent_snapshots(limit=3)
        self.assertEqual(len(rows), 3)
        # Most recent first
        self.assertGreater(rows[0]["timestamp"], rows[1]["timestamp"])
        self.assertGreater(rows[1]["timestamp"], rows[2]["timestamp"])

    def test_get_cycle_peaks(self):
        peaks = self.db.get_cycle_peaks()
        self.assertEqual(len(peaks), 2)
        # Each peak should have cycle_id, peak_five_hour, peak_seven_day, stoppage
        for p in peaks:
            self.assertIn("cycle_id", p.keys())
            self.assertIn("peak_five_hour", p.keys())
            self.assertIn("peak_seven_day", p.keys())
            self.assertIn("stoppage", p.keys())
        # Cycle 2026-03-26 has max five_hour_util = 0.5
        cycle_26 = [p for p in peaks if p["cycle_id"] == "2026-03-26"][0]
        self.assertAlmostEqual(cycle_26["peak_five_hour"], 0.5)
        # Stoppage flag: peak >= 0.95 means stoppage
        self.assertEqual(cycle_26["stoppage"], 0)

    def test_get_cycle_peaks_stoppage(self):
        # Insert a snapshot with util >= 0.95
        self.db.insert_snapshot(
            timestamp="2026-03-26T18:00:00Z",
            five_hour_util=0.98,
            seven_day_util=0.10,
            sonnet_util=None,
            five_hour_resets_at=None,
            seven_day_resets_at="2026-03-27T00:00:00Z",
        )
        peaks = self.db.get_cycle_peaks()
        cycle_27 = [p for p in peaks if p["cycle_id"] == "2026-03-27"][0]
        self.assertEqual(cycle_27["stoppage"], 1)


if __name__ == "__main__":
    unittest.main()
