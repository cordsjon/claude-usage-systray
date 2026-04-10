"""Integration tests for the HTTP API layer."""

import json
import threading
import unittest
import urllib.request

from engine.api import create_server
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


if __name__ == "__main__":
    unittest.main()
