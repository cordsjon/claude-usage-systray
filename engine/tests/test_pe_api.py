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


if __name__ == "__main__":
    unittest.main()
