"""Tests for engine.codeburn subagent burn extraction (US-TBD-A AC-01..AC-06).

Fixture shapes mirror real Claude Code transcript lines (verified 2026-06-11):
- Subagent spawn: assistant message content block
    {"type": "tool_use", "id": "toolu_x", "name": "Agent",
     "input": {"description": "...", "subagent_type": "Explore", "prompt": "..."}}
- Subagent result: user message content block
    {"type": "tool_result", "tool_use_id": "toolu_x", "content": [...]}
  with token usage in the ENTRY-LEVEL field
    obj["toolUseResult"]["usage"] = {"input_tokens": ..., "output_tokens": ...,
        "cache_read_input_tokens": ..., "cache_creation_input_tokens": ...}
"""

import unittest
from datetime import datetime, timezone

from engine.codeburn import _extract_subagent_stats


def _ts(minute: int = 0) -> str:
    return f"2026-06-11T10:{minute:02d}:00.000Z"


def _tool_use_entry(tool_id: str, name: str = "Agent", subagent_type: str = "Explore",
                    minute: int = 0) -> dict:
    return {
        "isSidechain": False,
        "uuid": f"uuid-{tool_id}",
        "timestamp": _ts(minute),
        "sessionId": "sess-1",
        "cwd": "/Users/x/projects/demo",
        "message": {
            "id": f"msg-{tool_id}",
            "role": "assistant",
            "model": "claude-opus-4-6",
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": name,
                    "input": {
                        "description": "do a thing",
                        "subagent_type": subagent_type,
                        "prompt": "go",
                    },
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 20},
        },
    }


def _tool_result_entry(tool_id: str, usage: dict | None = None, is_error: bool = False,
                       text: str = "done", minute: int = 1) -> dict:
    block = {
        "type": "tool_result",
        "tool_use_id": tool_id,
        "content": [{"type": "text", "text": text}],
    }
    if is_error:
        block["is_error"] = True
    entry = {
        "isSidechain": False,
        "uuid": f"uuid-result-{tool_id}",
        "timestamp": _ts(minute),
        "sessionId": "sess-1",
        "message": {"role": "user", "content": [block]},
    }
    if usage is not None:
        entry["toolUseResult"] = {
            "status": "completed",
            "agentType": "Explore",
            "totalTokens": sum(usage.values()),
            "usage": usage,
        }
    return entry


_USAGE = {
    "input_tokens": 2,
    "output_tokens": 1869,
    "cache_read_input_tokens": 71190,
    "cache_creation_input_tokens": 484,
}


class TestSubagentCounting(unittest.TestCase):
    def test_counts_task_and_agent_blocks_only(self):
        entries = [
            _tool_use_entry("t1", name="Agent"),
            _tool_use_entry("t2", name="Task", subagent_type="general-purpose"),
        ]
        # Bash tool_use must NOT be counted
        bash = _tool_use_entry("t3", name="Bash")
        bash["message"]["content"][0]["input"] = {"command": "ls"}
        entries.append(bash)
        stats = _extract_subagent_stats(entries)
        self.assertEqual(stats["count"], 2)

    def test_by_type_uses_subagent_type(self):
        entries = [
            _tool_use_entry("t1", subagent_type="Explore"),
            _tool_use_entry("t2", subagent_type="Explore", minute=2),
            _tool_use_entry("t3", subagent_type="general-purpose", minute=3),
        ]
        stats = _extract_subagent_stats(entries)
        self.assertEqual(stats["by_type"]["Explore"]["count"], 2)
        self.assertEqual(stats["by_type"]["general-purpose"]["count"], 1)

    def test_duplicate_tool_use_ids_deduped(self):
        # Resumed sessions copy history lines — same tool_use id appears twice
        entries = [_tool_use_entry("t1"), _tool_use_entry("t1")]
        stats = _extract_subagent_stats(entries)
        self.assertEqual(stats["count"], 1)

    def test_date_filter_excludes_out_of_range_tool_use(self):
        entries = [_tool_use_entry("t1")]
        date_from = datetime(2026, 6, 12, tzinfo=timezone.utc)
        date_to = datetime(2026, 6, 13, tzinfo=timezone.utc)
        stats = _extract_subagent_stats(entries, date_from, date_to)
        self.assertEqual(stats["count"], 0)


class TestSubagentTokenPairing(unittest.TestCase):
    def test_pairs_tool_result_usage(self):
        entries = [
            _tool_use_entry("t1"),
            _tool_result_entry("t1", usage=dict(_USAGE)),
        ]
        stats = _extract_subagent_stats(entries)
        self.assertEqual(stats["total_input_tokens"], 2 + 71190 + 484)
        self.assertEqual(stats["total_output_tokens"], 1869)
        self.assertEqual(stats["by_type"]["Explore"]["input_tokens"], 2 + 71190 + 484)
        self.assertEqual(stats["by_type"]["Explore"]["output_tokens"], 1869)

    def test_result_without_usage_counts_but_adds_zero_tokens(self):
        # Background agent ack: toolUseResult has no usage field
        entries = [_tool_use_entry("t1"), _tool_result_entry("t1", usage=None)]
        stats = _extract_subagent_stats(entries)
        self.assertEqual(stats["count"], 1)
        self.assertEqual(stats["total_input_tokens"], 0)
        self.assertEqual(stats["total_output_tokens"], 0)

    def test_unrelated_tool_result_ignored(self):
        entries = [
            _tool_use_entry("t1"),
            _tool_result_entry("t-other", usage=dict(_USAGE)),
        ]
        stats = _extract_subagent_stats(entries)
        self.assertEqual(stats["total_input_tokens"], 0)


class TestQuotaErrors(unittest.TestCase):
    def test_quota_error_detected(self):
        entries = [
            _tool_use_entry("t1"),
            _tool_result_entry("t1", is_error=True,
                               text="API Error: rate limit exceeded", minute=5),
        ]
        stats = _extract_subagent_stats(entries)
        self.assertEqual(stats["quota_error_count"], 1)
        self.assertEqual(stats["last_quota_error_at"], "2026-06-11T10:05:00+00:00")

    def test_quota_regex_variants(self):
        for text in ("quota exceeded", "rate-limit hit", "ratelimit", "tokens exhausted"):
            entries = [
                _tool_use_entry("t1"),
                _tool_result_entry("t1", is_error=True, text=text),
            ]
            stats = _extract_subagent_stats(entries)
            self.assertEqual(stats["quota_error_count"], 1, text)

    def test_error_without_quota_words_not_counted(self):
        entries = [
            _tool_use_entry("t1"),
            _tool_result_entry("t1", is_error=True, text="agent crashed: KeyError"),
        ]
        stats = _extract_subagent_stats(entries)
        self.assertEqual(stats["quota_error_count"], 0)

    def test_quota_words_without_is_error_not_counted(self):
        entries = [
            _tool_use_entry("t1"),
            _tool_result_entry("t1", is_error=False,
                               text="checked the quota dashboard, all fine"),
        ]
        stats = _extract_subagent_stats(entries)
        self.assertEqual(stats["quota_error_count"], 0)

    def test_string_content_tool_result(self):
        # tool_result content may be a plain string instead of a block list
        entries = [_tool_use_entry("t1")]
        result = _tool_result_entry("t1", is_error=True)
        result["message"]["content"][0]["content"] = "rate limit reached"
        entries.append(result)
        stats = _extract_subagent_stats(entries)
        self.assertEqual(stats["quota_error_count"], 1)


class TestEmptySessionSafety(unittest.TestCase):
    """AC-06: zero subagents must not raise."""

    def test_no_entries(self):
        stats = _extract_subagent_stats([])
        self.assertEqual(stats["count"], 0)
        self.assertEqual(stats["total_input_tokens"], 0)
        self.assertEqual(stats["total_output_tokens"], 0)
        self.assertEqual(stats["by_type"], {})
        self.assertEqual(stats["quota_error_count"], 0)
        self.assertIsNone(stats["last_quota_error_at"])

    def test_entries_without_subagents(self):
        bash = _tool_use_entry("t1", name="Bash")
        bash["message"]["content"][0]["input"] = {"command": "ls"}
        plain_user = {
            "isSidechain": False,
            "uuid": "u1",
            "timestamp": _ts(0),
            "message": {"role": "user", "content": "hello"},
        }
        malformed = {"timestamp": _ts(1), "message": "not-a-dict"}
        stats = _extract_subagent_stats([bash, plain_user, malformed])
        self.assertEqual(stats["count"], 0)


class TestScanIntegration(unittest.TestCase):
    """End-to-end through _scan_sessions: report carries subagent_stats,
    sanity warning fires when subagent input exceeds session input (AC-05)."""

    def setUp(self):
        import json
        import tempfile
        from pathlib import Path
        self.tmpdir = tempfile.mkdtemp()
        proj = Path(self.tmpdir) / "-Users-x-projects-demo"
        proj.mkdir()
        lines = [
            {
                "isSidechain": False,
                "uuid": "u-user",
                "timestamp": _ts(0),
                "sessionId": "sess-1",
                "cwd": "/Users/x/projects/demo",
                "message": {"role": "user", "content": "spawn an agent"},
            },
            _tool_use_entry("t1"),
            _tool_result_entry("t1", usage=dict(_USAGE), minute=2),
        ]
        self.session_file = proj / "sess-1.jsonl"
        with open(self.session_file, "w") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _scan(self):
        import unittest.mock as mock
        from engine import codeburn
        date_from = datetime(2026, 6, 11, tzinfo=timezone.utc)
        date_to = datetime(2026, 6, 12, tzinfo=timezone.utc)
        with mock.patch.object(codeburn, "_SESSIONS_BASE", self.tmpdir):
            return codeburn._scan_sessions(date_from, date_to)

    def test_report_contains_subagent_stats(self):
        report = self._scan()
        self.assertIn("subagent_stats", report)
        agg = report["subagent_stats"]
        self.assertEqual(agg["count"], 1)
        self.assertEqual(agg["total_input_tokens"], 2 + 71190 + 484)
        self.assertEqual(agg["total_output_tokens"], 1869)
        self.assertIn("sess-1", agg["sessions"])
        self.assertEqual(agg["sessions"]["sess-1"]["count"], 1)

    def test_sanity_warning_when_subagent_exceeds_session_input(self):
        # Session input (10 + no cache) < subagent input (71676) → warn, no crash
        with self.assertLogs("engine.codeburn", level="WARNING") as cm:
            report = self._scan()
        self.assertTrue(any("subagent" in m.lower() for m in cm.output))
        # AC-05 is a warning, never a crash — report still complete
        self.assertEqual(report["subagent_stats"]["count"], 1)

    def test_empty_session_dir_safe(self):
        import shutil
        shutil.rmtree(self.tmpdir)
        import os
        os.makedirs(self.tmpdir)
        report = self._scan()
        self.assertEqual(report["subagent_stats"]["count"], 0)
        self.assertEqual(report["subagent_stats"]["sessions"], {})


if __name__ == "__main__":
    unittest.main()
