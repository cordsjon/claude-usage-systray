"""Tests for turn origin (US-CUS-ORIGIN-01): operator-typed vs scripted turns.

User records carry ``turnOrigin`` ("human" / "sdk"); older ones fall back to
``promptSource`` / ``entrypoint``. Tool results are user-role records too but
continue the current turn instead of starting one.
"""

import json
import shutil
import tempfile
import unittest
import unittest.mock as mock
from datetime import datetime, timezone
from pathlib import Path

from engine import codeburn, jsonl_rollup

_FROM = datetime(2026, 6, 11, tzinfo=timezone.utc)
_TO = datetime(2026, 6, 12, tzinfo=timezone.utc)


def _prompt(minute: int, text: str = "go", **extra) -> dict:
    return {
        "type": "user", "isSidechain": False, "uuid": f"p-{minute}",
        "timestamp": f"2026-06-11T10:{minute:02d}:00.000Z",
        "cwd": "/Users/x/projects/demo",
        "message": {"role": "user", "content": text},
        **extra,
    }


def _tool_result(minute: int) -> dict:
    return {
        "type": "user", "isSidechain": False, "uuid": f"r-{minute}",
        "timestamp": f"2026-06-11T10:{minute:02d}:00.000Z",
        "cwd": "/Users/x/projects/demo",
        "toolUseResult": {"stdout": "ok"},
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"toolu_{minute}", "content": "ok"},
        ]},
    }


def _assistant(minute: int, *tools: str) -> dict:
    blocks = [
        {"type": "tool_use", "id": f"toolu_{minute}_{i}", "name": t,
         "input": {"command": "pytest"} if t == "Bash" else {}}
        for i, t in enumerate(tools)
    ] or [{"type": "text", "text": "ok"}]
    return {
        "type": "assistant", "isSidechain": False, "uuid": f"a-{minute}",
        "timestamp": f"2026-06-11T10:{minute:02d}:00.000Z",
        "cwd": "/Users/x/projects/demo",
        "message": {
            "id": f"msg-{minute}", "role": "assistant", "model": "claude-opus-4-6",
            "content": blocks, "usage": {"input_tokens": 100, "output_tokens": 50},
        },
    }


class _TmpSessions(unittest.TestCase):
    def _write(self, files: dict[str, list[dict]]) -> str:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        proj = Path(tmp) / "-Users-x-projects-demo"
        proj.mkdir()
        for name, lines in files.items():
            with open(proj / name, "w") as f:
                for obj in lines:
                    f.write(json.dumps(obj) + "\n")
        return tmp

    def _scan(self, files: dict[str, list[dict]], origin: str = "all") -> dict:
        tmp = self._write(files)
        with mock.patch.object(codeburn, "_SESSIONS_BASE", tmp):
            return codeburn._scan_sessions(_FROM, _TO, origin)


class TestTurnOrigin(_TmpSessions):
    def test_human_and_sdk_prompts_each_start_one_turn(self):
        files = {
            "human.jsonl": [_prompt(1, turnOrigin="human"), _assistant(2)],
            "sdk.jsonl": [_prompt(3, turnOrigin="sdk"), _assistant(4)],
        }
        report = self._scan(files)
        self.assertEqual(report["origin"], "all")
        self.assertEqual(report["total_turns"], 2)
        self.assertEqual(report["origin_summary"]["human"]["turns"], 1)
        self.assertEqual(report["origin_summary"]["sdk"]["turns"], 1)

        human_only = self._scan(files, origin="human")
        self.assertEqual(human_only["total_turns"], 1)
        # summary still covers all turns under a filter
        self.assertEqual(human_only["origin_summary"]["sdk"]["turns"], 1)

    def test_fallback_without_turn_origin(self):
        self.assertEqual(codeburn.classify_origin({"promptSource": "sdk"}), "sdk")
        self.assertEqual(codeburn.classify_origin({"entrypoint": "sdk-cli"}), "sdk")
        self.assertEqual(codeburn.classify_origin({"entrypoint": "claude-vscode"}), "human")
        self.assertEqual(codeburn.classify_origin({}), "human")

    def test_tool_result_continues_turn_and_retry_is_counted(self):
        # edit -> bash -> (tool_result) -> edit: one turn, one retry
        report = self._scan({"s.jsonl": [
            _prompt(1, turnOrigin="human"),
            _assistant(2, "Edit", "Bash"),
            _tool_result(3),
            _assistant(4, "Edit"),
        ]})
        self.assertEqual(report["total_turns"], 1)
        coding = [c for c in report["categories"] if c["edit_turns"]]
        self.assertEqual(sum(c["retries"] for c in coding), 1)
        self.assertEqual(report["origin_summary"]["human"]["oneshot_turns"], 0)

    def test_injected_user_records_are_not_prompts(self):
        user = {"role": "user", "content": "go"}
        not_prompts = [
            ({"isMeta": True}, user),
            ({"isCompactSummary": True, "turnOrigin": "human"}, user),
            ({"turnOrigin": "task_notification", "promptSource": "system"}, user),
            ({}, {"role": "user", "content": "<local-command-stdout>x</local-command-stdout>"}),
            ({}, {"role": "user", "content": [{"type": "text",
                                                "text": "[Request interrupted by user]"}]}),
        ]
        for obj, msg in not_prompts:
            self.assertFalse(codeburn.is_prompt_record(obj, msg), obj or msg)
        self.assertTrue(codeburn.is_prompt_record({"turnOrigin": "human"}, user))

    def test_interleaved_sessions_keep_their_own_turns(self):
        # Entries sort across files; an sdk session's records landing between
        # an operator prompt and its reply must not join the operator's turn.
        report = self._scan({
            "human.jsonl": [_prompt(1, turnOrigin="human"), _assistant(4)],
            "sdk.jsonl": [_prompt(2, turnOrigin="sdk"), _assistant(3), _tool_result(5),
                          _assistant(6)],
        })
        self.assertEqual(report["origin_summary"]["human"]["output_tokens"], 50)
        self.assertEqual(report["origin_summary"]["sdk"]["output_tokens"], 100)


class TestRollupOrigin(_TmpSessions):
    def test_assistant_tokens_follow_preceding_prompt_origin(self):
        tmp = self._write({"s.jsonl": [
            _assistant(0),  # before any prompt -> human
            _prompt(1, turnOrigin="human"),
            _assistant(2),
            _prompt(3, turnOrigin="sdk"),
            _tool_result(4),  # does not reset the origin
            _assistant(5),
        ]})
        now = datetime(2026, 6, 11, 11, 0, tzinfo=timezone.utc)
        with mock.patch.object(jsonl_rollup, "JSONL_ROOT", tmp):
            snap = jsonl_rollup.compute_snapshot(now)
        per_call = int(jsonl_rollup._weighted({"input_tokens": 100, "output_tokens": 50},
                                              "claude-opus-4-6"))
        self.assertEqual(snap["weighted_7d_by_origin"], {"human": 2 * per_call, "sdk": per_call})
        self.assertEqual(snap["weighted_5h_by_origin"], {"human": 2 * per_call, "sdk": per_call})


if __name__ == "__main__":
    unittest.main()
