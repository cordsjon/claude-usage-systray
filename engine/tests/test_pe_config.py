# engine/tests/test_pe_config.py
import json
import os
import tempfile
import unittest

from engine.pe_config import PEInstance, load_pe_instances, PEConfigError


class TestLoadPEInstances(unittest.TestCase):
    def _write(self, data):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        self.addCleanup(os.unlink, path)
        return path

    def test_loads_valid_dev_and_prod_instances(self):
        path = self._write([
            {"name": "dev", "base_url": "http://127.0.0.1:9120",
             "token_ref": "PosterEngine-dev-admin", "kick_method": "launchctl",
             "budget_24h_usd": 0.5},
            {"name": "prod", "base_url": "https://poster.getaccess.cloud",
             "token_ref": "PosterEngine-prod-admin", "kick_method": "ssh",
             "ssh_host": "root@72.61.159.117", "budget_24h_usd": 2.0},
        ])
        instances = load_pe_instances(path)
        self.assertEqual(len(instances), 2)
        self.assertIsInstance(instances[0], PEInstance)
        self.assertEqual(instances[0].name, "dev")
        self.assertEqual(instances[1].kick_method, "ssh")
        self.assertEqual(instances[1].ssh_host, "root@72.61.159.117")

    def test_rejects_plaintext_non_localhost_url(self):
        path = self._write([
            {"name": "prod", "base_url": "http://poster.getaccess.cloud",
             "token_ref": "x", "kick_method": "ssh",
             "ssh_host": "root@72.61.159.117", "budget_24h_usd": 1.0},
        ])
        with self.assertRaises(PEConfigError):
            load_pe_instances(path)

    def test_allows_plaintext_localhost_and_127001(self):
        path = self._write([
            {"name": "dev", "base_url": "http://127.0.0.1:9120",
             "token_ref": "x", "kick_method": "launchctl", "budget_24h_usd": 1.0},
            {"name": "dev2", "base_url": "http://localhost:9120",
             "token_ref": "x", "kick_method": "launchctl", "budget_24h_usd": 1.0},
        ])
        instances = load_pe_instances(path)
        self.assertEqual(len(instances), 2)

    def test_missing_file_returns_empty_list(self):
        self.assertEqual(load_pe_instances("/nonexistent/path.json"), [])

    def test_missing_required_field_raises(self):
        path = self._write([{"name": "dev", "base_url": "http://127.0.0.1:9120"}])
        with self.assertRaises(PEConfigError):
            load_pe_instances(path)


if __name__ == "__main__":
    unittest.main()
