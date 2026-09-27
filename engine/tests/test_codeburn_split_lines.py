"""Tests for engine.codeburn handling of one API response logged as several lines.

Claude Code writes each content block of an assistant response (thinking, text,
tool_use) as its own JSONL line. All lines share message.id and carry the same
usage (verified 2026-09-27: 16,775 repeat lines, 0 with differing usage). The
scanner used to keep only the first line per message.id, which dropped 25% of
[TENET:] tags and ~80% of tool_use blocks.
"""

import json
import shutil
import tempfile
import unittest
import unittest.mock as mock
from datetime import datetime, timezone
from pathlib import Path

from engine import codeburn

_USAGE = {"input_tokens": 100, "output_tokens": 50}


def _line(minute: int, block: dict) -> dict:
    return {
        "isSidechain": False,
        "uuid": f"u-{minute}",
        "timestamp": f"2026-06-11T10:{minute:02d}:00.000Z",
        "sessionId": "sess-1",
        "cwd": "/Users/x/projects/demo",
        "message": {
            "id": "msg-1",
            "role": "assistant",
            "model": "claude-opus-4-6",
            "content": [block],
            "usage": dict(_USAGE),
        },
    }


class TestSplitResponseLines(unittest.TestCase):
    def _scan(self, lines: list[dict]) -> dict:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        proj = Path(tmp) / "-Users-x-projects-demo"
        proj.mkdir()
        user = {
            "isSidechain": False, "uuid": "u-user",
            "timestamp": "2026-06-11T10:00:00.000Z", "sessionId": "sess-1",
            "cwd": "/Users/x/projects/demo",
            "message": {"role": "user", "content": "go"},
        }
        with open(proj / "sess-1.jsonl", "w") as f:
            for obj in [user, *lines]:
                f.write(json.dumps(obj) + "\n")
        with mock.patch.object(codeburn, "_SESSIONS_BASE", tmp):
            return codeburn._scan_sessions(
                datetime(2026, 6, 11, tzinfo=timezone.utc),
                datetime(2026, 6, 12, tzinfo=timezone.utc),
            )

    def test_blocks_on_later_lines_are_counted_and_usage_once(self):
        thinking = {"type": "thinking", "thinking": "hmm"}
        text = {"type": "text", "text": "Keeping it small [TENET: mva]"}
        tool = {"type": "tool_use", "id": "toolu_1", "name": "Bash",
                "input": {"command": "ls"}}
        split = self._scan([_line(1, thinking), _line(1, text), _line(1, tool)])
        single = self._scan([_line(1, tool)])

        self.assertEqual([c["tenet"] for c in split["tenet_citations"]], ["mva"])
        self.assertEqual({t["name"]: t["calls"] for t in split["tools"]}, {"Bash": 1})
        # usage is repeated on every line; cost must count it once
        self.assertEqual(split["total_cost_usd"], single["total_cost_usd"])

    def test_resumed_session_copy_does_not_double_count(self):
        text = {"type": "text", "text": "[TENET: mva]"}
        split = self._scan([_line(1, text), _line(1, text)])
        self.assertEqual(len(split["tenet_citations"]), 1)


if __name__ == "__main__":
    unittest.main()
