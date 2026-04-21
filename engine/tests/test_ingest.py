"""Tests for ingest_prompts driver (US-TB-01 AC-01, AC-05, AC-05a, AC-16)."""

import shutil
import tempfile
import unittest
from pathlib import Path

from engine.db import UsageDB
from engine.ingest_prompts import (
    compute_sha256_head,
    ingest_all,
    iter_user_messages,
    resolve_start_offset,
)


FIXT = Path(__file__).parent / "fixtures" / "sample_conversation.jsonl"
PATTERNS_YAML = Path(__file__).parent / "fixtures" / "patterns_test.yaml"


class TestIterUserMessages(unittest.TestCase):
    def test_extracts_only_text_user_messages(self):
        msgs = list(iter_user_messages(FIXT, start_offset=0))
        texts = [m["text"] for m in msgs]
        self.assertEqual(texts, ["/lightsout", "produce animals s1"])

    def test_each_has_ordinal(self):
        msgs = list(iter_user_messages(FIXT, start_offset=0))
        ordinals = [m["message_ordinal"] for m in msgs]
        self.assertEqual(ordinals, [0, 3])

    def test_end_offset_reported(self):
        msgs = list(iter_user_messages(FIXT, start_offset=0))
        self.assertGreater(msgs[-1]["byte_offset_after"], 0)


class TestWatermark(unittest.TestCase):
    def test_new_file_starts_at_offset_0(self):
        db = UsageDB(":memory:")
        off, head = resolve_start_offset(db, str(FIXT))
        self.assertEqual(off, 0)
        self.assertEqual(head, compute_sha256_head(FIXT))

    def test_known_file_resumes_from_watermark(self):
        db = UsageDB(":memory:")
        head = compute_sha256_head(FIXT)
        db.upsert_watermark(str(FIXT), 50, head, "2026-04-20T10:00:00")
        off, h = resolve_start_offset(db, str(FIXT))
        self.assertEqual(off, 50)
        self.assertEqual(h, head)

    def test_sha_mismatch_resets_offset(self):
        db = UsageDB(":memory:")
        db.upsert_watermark(str(FIXT), 50, "stale_sha", "2026-04-20T10:00:00")
        off, _ = resolve_start_offset(db, str(FIXT))
        self.assertEqual(off, 0)


class TestIngestE2E(unittest.TestCase):
    def test_full_run_populates_prompt_usage_and_reports_coverage(self):
        db = UsageDB(":memory:")
        tmp = Path(tempfile.mkdtemp())
        try:
            proj = tmp / "-Users-x-projects-demo" / "conversations"
            proj.mkdir(parents=True)
            shutil.copy(FIXT, proj / "s1.jsonl")

            report = ingest_all(db, projects_root=tmp, patterns_yaml=PATTERNS_YAML)

            count = db._conn.execute(
                "SELECT COUNT(*) FROM prompt_usage"
            ).fetchone()[0]
            self.assertGreaterEqual(count, 1)
            self.assertGreater(report["total_user_messages"], 0)
            self.assertIn("matched_percent", report)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            db.close()


if __name__ == "__main__":
    unittest.main()
