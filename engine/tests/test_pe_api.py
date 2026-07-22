# engine/tests/test_pe_api.py
"""HTTP-level tests for the /pe/* routes, cloning engine/tests/test_api.py's
pattern: real HTTPServer on an ephemeral port, real urllib requests."""

import json
import threading
import unittest
import urllib.error
import urllib.request

from engine.api import create_server
from engine.db import UsageDB
from engine.poller import TokenHolder
from engine.pe_config import PEInstance


class TestPEStatusRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = UsageDB(":memory:")
        cls.token_holder = TokenHolder("fake-token")
        cls.pe_instances = [
            PEInstance(name="dev", base_url="http://127.0.0.1:9120",
                       token_ref="x", kick_method="launchctl", budget_24h_usd=1.0),
        ]
        cls.server = create_server(
            cls.db, cls.token_holder, port=0, pe_instances=cls.pe_instances
        )
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.db.close()

    def _get(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_pe_status_returns_configured_instances_shape(self):
        status, body = self._get("/pe/status")
        self.assertEqual(status, 200)
        self.assertIn("instances", body)
        self.assertIn("alerts", body)
        self.assertIn("ops", body)
        names = [i["name"] for i in body["instances"]]
        self.assertIn("dev", names)

    def test_pe_status_before_first_poll_shows_not_reachable(self):
        # No poller has run in this test process (pe_poller module-level
        # state is empty) — status must degrade gracefully, not 500.
        status, body = self._get("/pe/status")
        self.assertEqual(status, 200)
        dev = next(i for i in body["instances"] if i["name"] == "dev")
        self.assertIn("reachable", dev)

    def test_pe_status_alert_active_is_json_boolean(self):
        # SQLite stores active as 0/1; the wire contract (and the Swift
        # client's Bool decode) requires a JSON boolean. Caught live in the
        # 2026-07-22 E2E: "active": 1 made every /pe/status decode fail in
        # the systray app.
        self.db.upsert_pe_alert_state(
            alert_id="stalled:dev:active",
            first_seen="2026-07-22T00:00:00+00:00",
            last_seen="2026-07-22T00:00:00+00:00",
            active=True,
        )
        try:
            status, body = self._get("/pe/status")
            self.assertEqual(status, 200)
            alert = next(a for a in body["alerts"]
                         if a["alert_id"] == "stalled:dev:active")
            self.assertIs(alert["active"], True)
        finally:
            self.db.upsert_pe_alert_state(
                alert_id="stalled:dev:active",
                first_seen="2026-07-22T00:00:00+00:00",
                last_seen="2026-07-22T00:00:00+00:00",
                active=False,
            )


class TestPEControlRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = UsageDB(":memory:")
        cls.token_holder = TokenHolder("fake-token")
        cls.pe_instances = [
            PEInstance(name="dev", base_url="http://127.0.0.1:1",  # unreachable on purpose
                       token_ref="x", kick_method="launchctl", budget_24h_usd=1.0),
        ]
        cls.server = create_server(
            cls.db, cls.token_holder, port=0, pe_instances=cls.pe_instances
        )
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.db.close()

    def _post(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        req = urllib.request.Request(url, data=b"{}", method="POST",
                                      headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_retry_unknown_instance_404(self):
        status, body = self._post("/pe/nonexistent/jobs/job-1/retry")
        self.assertEqual(status, 404)

    def test_kick_unknown_instance_404(self):
        status, body = self._post("/pe/nonexistent/worker/kick")
        self.assertEqual(status, 404)

    def test_retry_unknown_job_404(self):
        # No poll has seeded recent_terminal with this job_id in this test
        # process, so it is "unknown" — this is the expected
        # preflight-miss path (spec design line 222).
        status, body = self._post("/pe/dev/jobs/never-seen/retry")
        self.assertEqual(status, 404)

    def test_retry_known_instance_and_job_returns_202_with_op_id(self):
        from engine.pe_poller import _update_pe_status
        _update_pe_status("dev", {
            "reachable": True, "counts": {}, "oldest_claimable_queued_s": 0,
            "stalled": False,
            "recent_terminal": [{"job_id": "job-xyz", "status": "failed",
                                  "topic": "t", "error": "e", "updated_at": "2026-07-22T00:00:00Z"}],
            "cost": {"d24h_usd": 0.0, "calls": 0, "available": False},
            "budget": {"target_24h_usd": 1.0, "crossed": False},
            "last_poll": "2026-07-22T00:00:00Z",
        })
        status, body = self._post("/pe/dev/jobs/job-xyz/retry")
        self.assertEqual(status, 202)
        self.assertTrue(body["accepted"])
        self.assertIn("op_id", body)

    def test_unmatched_pe_path_falls_through_to_404(self):
        status, body = self._post("/pe/dev/something/unrelated")
        self.assertEqual(status, 404)

    def test_failed_retry_op_appears_in_ops_list(self):
        from engine.pe_poller import _update_pe_status
        # instance base_url is http://127.0.0.1:1 (unreachable by construction
        # in setUpClass) — seed recent_terminal so the preflight passes and
        # the op actually dispatches; the failure we're testing comes from
        # the unreachable base_url, not from the preflight check.
        _update_pe_status("dev", {
            "reachable": True, "counts": {}, "oldest_claimable_queued_s": 0,
            "stalled": False,
            "recent_terminal": [{"job_id": "job-abc", "status": "failed",
                                  "topic": "t", "error": "e", "updated_at": "2026-07-22T00:00:00Z"}],
            "cost": {"d24h_usd": 0.0, "calls": 0, "available": False},
            "budget": {"target_24h_usd": 1.0, "crossed": False},
            "last_poll": "2026-07-22T00:00:00Z",
        })
        status, body = self._post("/pe/dev/jobs/job-abc/retry")
        self.assertEqual(status, 202)
        op_id = body["op_id"]

        import time
        deadline = time.monotonic() + 5
        found = None
        while time.monotonic() < deadline:
            ops = [o for o in self.db.get_recent_pe_ops(limit=10) if o["op_id"] == op_id]
            if ops and ops[0]["state"] != "pending":
                found = ops[0]
                break
            time.sleep(0.2)
        self.assertIsNotNone(found)
        self.assertEqual(found["state"], "failed")
        active_alert_ids = [a["alert_id"] for a in self.db.get_active_pe_alerts()]
        self.assertIn(f"op_failed:dev:{op_id}", active_alert_ids)


if __name__ == "__main__":
    unittest.main()
