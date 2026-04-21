"""Integration tests for the HTTP API layer."""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from engine.api import create_server
from engine.classification import load_classification, save_classification
from engine.db import UsageDB
from engine.poller import _update_status


class TestAPI(unittest.TestCase):
    """Test all API endpoints against a live server on a random port."""

    @classmethod
    def setUpClass(cls):
        cls.db = UsageDB(":memory:")
        # Seed one snapshot
        cls.db.insert_snapshot(
            timestamp="2026-03-26T12:00:00Z",
            five_hour_util=0.42,
            seven_day_util=0.31,
            sonnet_util=0.10,
            five_hour_resets_at="2026-03-26T17:00:00Z",
            seven_day_resets_at="2026-03-27T00:00:00Z",
        )
        # Seed shared status via poller
        _update_status({
            "version": 1,
            "current": {
                "five_hour_util": 0.42,
                "seven_day_util": 0.31,
            },
            "projection": {
                "runway_hours": 2.5,
                "burn_rate_per_hour": 0.08,
            },
            "budget": {
                "recommended_daily": 0.14,
                "days_remaining": 3,
            },
            "updated_at": "2026-03-26T12:00:00Z",
        })
        # Start server on random port
        cls.server = create_server(cls.db, port=0)
        cls.port = cls.server.server_address[1]
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.db.close()

    def _get(self, path: str) -> tuple:
        """GET a path and return (status_code, body_bytes)."""
        url = f"{self.base_url}{path}"
        try:
            with urllib.request.urlopen(url) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def _get_json(self, path: str) -> tuple:
        """GET a path and return (status_code, parsed_json)."""
        status, body = self._get(path)
        return status, json.loads(body)

    def test_health_endpoint(self):
        status, data = self._get_json("/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")
        self.assertIn("uptime_seconds", data)
        self.assertIsInstance(data["uptime_seconds"], (int, float))

    def test_status_endpoint(self):
        status, data = self._get_json("/api/status")
        self.assertEqual(status, 200)
        self.assertIn("version", data)
        self.assertIn("current", data)
        self.assertIn("projection", data)
        self.assertIn("budget", data)

    def test_history_endpoint(self):
        status, data = self._get_json("/api/history?range=7d")
        self.assertEqual(status, 200)
        self.assertIn("snapshots", data)
        self.assertIn("cycles", data)
        self.assertIn("weekday_avg", data)
        self.assertIsInstance(data["snapshots"], list)
        self.assertGreaterEqual(len(data["snapshots"]), 1)

    def test_root_serves_html(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"<!DOCTYPE html>", body)

    def test_unknown_path_404(self):
        status, data = self._get_json("/nonexistent")
        self.assertEqual(status, 404)


class TestPromptsAPI(unittest.TestCase):
    """Tests for US-TB-01 Habits Tab /api/prompts* endpoints."""

    @classmethod
    def setUpClass(cls):
        cls.db = UsageDB(":memory:")

        # Seed prompt_usage rows for today (so count_7d > 0)
        from datetime import date
        today = date.today().isoformat()
        cls.db.insert_prompt_usage(
            date=today,
            session_id="s-api-1",
            project_dir="/p",
            pattern_id="lightsout",
            pattern_version=1,
            is_structured=1,
            matched_text="/lightsout",
            message_ordinal=0,
        )
        cls.db.insert_prompt_usage(
            date=today,
            session_id="s-api-2",
            project_dir="/p",
            pattern_id="produce",
            pattern_version=1,
            is_structured=0,
            matched_text="produce animals",
            message_ordinal=0,
        )
        # Seed unmatched rows for dry-run endpoint
        cls.db.insert_prompt_unmatched(
            date=today,
            session_id="s-u-1",
            text_excerpt="produce svg collection v2",
            message_ordinal=0,
        )
        cls.db.insert_prompt_unmatched(
            date=today,
            session_id="s-u-2",
            text_excerpt="hello world",
            message_ordinal=1,
        )

        # Temp paths for classification JSON + pattern YAML
        cls.tmpdir = Path(tempfile.mkdtemp())
        cls.classification_path = cls.tmpdir / "classification.json"
        save_classification(
            cls.classification_path, {"everyday": [], "case_by_case": []}
        )
        cls.patterns_yaml_path = cls.tmpdir / "patterns.yaml"
        cls.patterns_yaml_path.write_text(
            "patterns:\n"
            "  - id: lightsout\n"
            "    intent: wrap session\n"
            "    regex: '^/lightsout\\b'\n"
            "    type: structured\n"
            "    version: 1\n"
            "  - id: produce\n"
            "    intent: SVG production run\n"
            "    regex: '(?i)^\\s*produce\\s+'\n"
            "    type: unstructured\n"
            "    version: 1\n"
        )

        class _Holder:
            def __init__(self):
                self.token = "test"
                self.needs_refresh = False

        cls.token_holder = _Holder()
        cls.server = create_server(
            cls.db,
            cls.token_holder,
            port=0,
            classification_path=cls.classification_path,
            patterns_yaml_path=cls.patterns_yaml_path,
        )
        cls.port = cls.server.server_address[1]
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True
        )
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.db.close()

    def _get(self, path: str):
        url = f"{self.base_url}{path}"
        try:
            with urllib.request.urlopen(url) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def _post(self, path: str, payload: dict):
        url = f"{self.base_url}{path}"
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    # ── /api/prompts ────────────────────────────────────────

    def test_api_prompts_returns_two_sections(self):
        status, body = self._get("/api/prompts")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIn("everyday", data)
        self.assertIn("case_by_case", data)
        self.assertIn("generated_at", data)

    def test_api_prompts_rows_have_required_fields(self):
        status, body = self._get("/api/prompts")
        self.assertEqual(status, 200)
        data = json.loads(body)
        # Combine sections — pattern_id should be somewhere
        rows = data["everyday"] + data["case_by_case"]
        ids = [r["pattern_id"] for r in rows]
        self.assertIn("lightsout", ids)
        self.assertIn("produce", ids)
        for r in rows:
            for key in (
                "pattern_id", "intent", "type", "count_7d",
                "count_30d", "count_all", "has_skill", "skill_candidate",
            ):
                self.assertIn(key, r)

    # ── /api/prompts/classify ───────────────────────────────

    def test_api_classify_moves_pattern(self):
        status, body = self._post(
            "/api/prompts/classify",
            {"pattern_id": "lightsout", "section": "everyday"},
        )
        self.assertEqual(status, 200)
        data = load_classification(self.classification_path)
        self.assertIn("lightsout", data["everyday"])

    def test_api_classify_400_on_bad_section(self):
        status, body = self._post(
            "/api/prompts/classify",
            {"pattern_id": "x", "section": "invalid"},
        )
        self.assertEqual(status, 400)
        err = json.loads(body)
        self.assertIn("error", err)

    def test_api_classify_400_on_missing_field(self):
        status, body = self._post(
            "/api/prompts/classify",
            {"section": "everyday"},
        )
        self.assertEqual(status, 400)

    # ── /api/prompts/dry-run ────────────────────────────────

    def test_api_dry_run_returns_hit_count(self):
        status, body = self._post(
            "/api/prompts/dry-run",
            {"regex": r"(?i)^\s*produce\s+"},
        )
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIn("hit_count", data)
        self.assertIn("sample_matches", data)
        self.assertGreaterEqual(data["hit_count"], 1)

    def test_api_dry_run_400_on_bad_regex(self):
        status, body = self._post(
            "/api/prompts/dry-run",
            {"regex": "(unclosed"},
        )
        self.assertEqual(status, 400)

    def test_api_dry_run_400_on_missing_regex(self):
        status, body = self._post("/api/prompts/dry-run", {})
        self.assertEqual(status, 400)

    # ── /api/prompts/pattern ────────────────────────────────

    def test_api_add_pattern_appends_to_yaml(self):
        status, body = self._post(
            "/api/prompts/pattern",
            {
                "id": "newp",
                "intent": "test",
                "regex": "^xyz",
                "type": "unstructured",
                "version": 1,
            },
        )
        self.assertEqual(status, 200)
        # Verify YAML now has the new pattern
        import yaml as _yaml
        doc = _yaml.safe_load(self.patterns_yaml_path.read_text())
        ids = [p["id"] for p in doc.get("patterns", [])]
        self.assertIn("newp", ids)

    def test_api_add_pattern_400_on_missing_field(self):
        status, body = self._post(
            "/api/prompts/pattern",
            {"id": "incomplete"},
        )
        self.assertEqual(status, 400)

    def test_api_add_pattern_400_on_bad_regex(self):
        status, body = self._post(
            "/api/prompts/pattern",
            {
                "id": "badre",
                "intent": "x",
                "regex": "(unclosed",
                "type": "unstructured",
                "version": 1,
            },
        )
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
