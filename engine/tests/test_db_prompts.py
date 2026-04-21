"""Tests for prompt-frequency tables and CRUD (US-TB-01)."""

import unittest

from engine.db import UsageDB


class TestPromptTables(unittest.TestCase):
    """Verify all 5 new prompt-related tables exist."""

    def setUp(self):
        self.db = UsageDB(":memory:")

    def tearDown(self):
        self.db.close()

    def _table_exists(self, name):
        row = self.db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        return row is not None

    def test_prompt_usage_table_exists(self):
        self.assertTrue(self._table_exists("prompt_usage"))

    def test_prompt_unmatched_table_exists(self):
        self.assertTrue(self._table_exists("prompt_unmatched"))

    def test_prompt_pattern_eval_table_exists(self):
        self.assertTrue(self._table_exists("prompt_pattern_eval"))

    def test_prompt_pattern_eval_labels_table_exists(self):
        self.assertTrue(self._table_exists("prompt_pattern_eval_labels"))

    def test_ingest_watermark_table_exists(self):
        self.assertTrue(self._table_exists("ingest_watermark"))


class TestPromptCRUD(unittest.TestCase):
    """Verify insert_prompt_usage / insert_prompt_unmatched / watermark upsert."""

    def setUp(self):
        self.db = UsageDB(":memory:")

    def tearDown(self):
        self.db.close()

    def test_insert_prompt_usage_is_idempotent(self):
        row = dict(
            date="2026-04-21",
            session_id="s1",
            project_dir="/Users/x",
            pattern_id="lightsout",
            pattern_version=1,
            is_structured=1,
            matched_text="/lightsout",
            message_ordinal=7,
        )
        self.db.insert_prompt_usage(**row)
        self.db.insert_prompt_usage(**row)  # duplicate — must not double-count
        count = self.db._conn.execute(
            "SELECT COUNT(*) FROM prompt_usage"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_insert_prompt_unmatched_stores_excerpt(self):
        self.db.insert_prompt_unmatched(
            date="2026-04-21",
            session_id="s1",
            text_excerpt="some novel prompt",
            message_ordinal=3,
        )
        row = self.db._conn.execute(
            "SELECT text_excerpt FROM prompt_unmatched"
        ).fetchone()
        self.assertEqual(row[0], "some novel prompt")

    def test_upsert_watermark_updates_on_conflict(self):
        self.db.upsert_watermark(
            "/tmp/a.jsonl", 100, "abc", "2026-04-21T10:00:00"
        )
        self.db.upsert_watermark(
            "/tmp/a.jsonl", 200, "abc", "2026-04-21T11:00:00"
        )
        row = self.db._conn.execute(
            "SELECT byte_offset FROM ingest_watermark WHERE file_path=?",
            ("/tmp/a.jsonl",),
        ).fetchone()
        self.assertEqual(row[0], 200)

    def test_get_watermark_returns_none_for_new_file(self):
        self.assertIsNone(self.db.get_watermark("/tmp/new.jsonl"))


class TestRankedPrompts(unittest.TestCase):
    """Verify get_ranked_prompts windowed aggregation."""

    def setUp(self):
        self.db = UsageDB(":memory:")
        # Seed: 3× lightsout, 1× produce (all dated 2026-04-20)
        for i in range(3):
            self.db.insert_prompt_usage(
                date="2026-04-20",
                session_id=f"s{i}",
                project_dir="/p",
                pattern_id="lightsout",
                pattern_version=1,
                is_structured=1,
                matched_text="/lightsout",
                message_ordinal=i,
            )
        self.db.insert_prompt_usage(
            date="2026-04-20",
            session_id="sp",
            project_dir="/p",
            pattern_id="produce",
            pattern_version=1,
            is_structured=0,
            matched_text="produce animals",
            message_ordinal=0,
        )

    def tearDown(self):
        self.db.close()

    def test_ranked_includes_7d_30d_all_counts(self):
        rows = self.db.get_ranked_prompts(today="2026-04-21")
        by_id = {r["pattern_id"]: r for r in rows}
        self.assertEqual(by_id["lightsout"]["count_all"], 3)
        self.assertEqual(by_id["produce"]["count_all"], 1)
        # Both within 7d window (yesterday)
        self.assertEqual(by_id["lightsout"]["count_7d"], 3)
        self.assertEqual(by_id["produce"]["count_7d"], 1)


class TestDowngrade(unittest.TestCase):
    """Verify downgrade_prompt_tables drops only the new tables."""

    def test_downgrade_drops_only_new_tables(self):
        db = UsageDB(":memory:")
        try:
            db.downgrade_prompt_tables()
            for t in (
                "prompt_usage",
                "prompt_unmatched",
                "prompt_pattern_eval",
                "prompt_pattern_eval_labels",
                "ingest_watermark",
            ):
                row = db._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (t,),
                ).fetchone()
                self.assertIsNone(row, f"{t} should be dropped")
            # usage_snapshots must remain
            row = db._conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='usage_snapshots'"
            ).fetchone()
            self.assertIsNotNone(row)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
