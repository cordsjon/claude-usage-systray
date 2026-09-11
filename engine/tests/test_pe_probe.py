# engine/tests/test_pe_probe.py
import json
import os
import tempfile

import pytest
from pytest_httpserver import HTTPServer

from engine.pe_config import PEInstance
from engine.pe_probe import format_probe, main, probe_instance


@pytest.fixture
def httpserver_instance(httpserver: HTTPServer):
    return PEInstance(
        name="dev", base_url=httpserver.url_for("").rstrip("/"),
        token_ref="PosterEngine-dev-admin", kick_method="launchctl",
        budget_24h_usd=0.5,
    )


def _serve_healthy(httpserver: HTTPServer, cost=0.0008, running=1, oldest=12):
    httpserver.expect_request(
        "/api/jobs/summary", headers={"Authorization": "Bearer tok123"}
    ).respond_with_json({
        "counts": {"queued": 2, "running": running, "complete_24h": 14,
                   "dead": 0, "failed": 1},
        "oldest_claimable_queued_s": oldest,
        "recent_terminal": [],
    })
    httpserver.expect_request("/api/admin/router-metrics").respond_with_json(
        {"available": True, "cost_24h_usd": cost, "calls": 12}
    )


class TestProbeInstance:
    def test_healthy_instance_reports_ok(self, httpserver: HTTPServer, httpserver_instance):
        _serve_healthy(httpserver)
        r = probe_instance(httpserver_instance, get_token=lambda ref: "tok123", timeout=5)
        assert r["ok"] is True
        assert r["error"] is None
        assert r["token_present"] is True
        assert r["jobs"]["counts"]["queued"] == 2
        assert r["jobs"]["stalled"] is False
        assert r["cost"]["d24h_usd"] == 0.0008
        assert r["cost"]["over_budget"] is False

    def test_missing_keychain_token_short_circuits_without_fetching(self, httpserver_instance):
        # No expect_request registered: any HTTP call would fail the assertion below.
        r = probe_instance(httpserver_instance, get_token=lambda ref: None)
        assert r["ok"] is False
        assert r["token_present"] is False
        assert "PosterEngine-dev-admin" in r["error"]
        assert r["jobs"] is None

    def test_never_echoes_the_token(self, httpserver: HTTPServer, httpserver_instance):
        _serve_healthy(httpserver)
        r = probe_instance(httpserver_instance, get_token=lambda ref: "sekrit-tok")
        assert "sekrit-tok" not in json.dumps(r)
        assert "sekrit-tok" not in format_probe(r)

    def test_401_reports_unreachable(self, httpserver: HTTPServer, httpserver_instance):
        httpserver.expect_request("/api/jobs/summary").respond_with_json(
            {"error": "unauthorized"}, status=401
        )
        httpserver.expect_request("/api/admin/router-metrics").respond_with_json(
            {"error": "unauthorized"}, status=401
        )
        r = probe_instance(httpserver_instance, get_token=lambda ref: "bad")
        assert r["ok"] is False
        assert r["error"] == "http_401"
        assert r["cost"]["error"] == "http_401"

    def test_stall_and_over_budget_flags(self, httpserver: HTTPServer, httpserver_instance):
        _serve_healthy(httpserver, cost=1.25, running=0, oldest=600)
        r = probe_instance(httpserver_instance, get_token=lambda ref: "tok123")
        assert r["jobs"]["stalled"] is True
        assert r["cost"]["over_budget"] is True
        assert "STALLED" in format_probe(r)
        assert "OVER BUDGET" in format_probe(r)


def _write_config(data):
    fd, path = tempfile.mkstemp(suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    return path


class TestMain:
    def test_healthy_instance_exits_zero(self, httpserver: HTTPServer, monkeypatch, capsys):
        path = _write_config([
            {"name": "dev", "base_url": httpserver.url_for("").rstrip("/"),
             "token_ref": "x", "kick_method": "launchctl", "budget_24h_usd": 0.5},
        ])
        monkeypatch.setattr("engine.pe_probe.keychain_get", lambda ref: "tok123")
        _serve_healthy(httpserver)
        try:
            assert main(["--config", path]) == 0
        finally:
            os.unlink(path)
        assert "dev: ok" in capsys.readouterr().out

    def test_unreachable_instance_exits_one(self, monkeypatch, capsys):
        path = _write_config([
            {"name": "dev", "base_url": "http://127.0.0.1:1",
             "token_ref": "x", "kick_method": "launchctl", "budget_24h_usd": 0.5},
        ])
        monkeypatch.setattr("engine.pe_probe.keychain_get", lambda ref: "tok123")
        try:
            assert main(["--config", path, "--timeout", "1"]) == 1
        finally:
            os.unlink(path)
        assert "dev: FAIL" in capsys.readouterr().out

    def test_missing_config_exits_two(self):
        assert main(["--config", "/nonexistent/pe_instances.json"]) == 2

    def test_unknown_instance_name_exits_two(self):
        path = _write_config([
            {"name": "dev", "base_url": "http://127.0.0.1:9120",
             "token_ref": "x", "kick_method": "launchctl", "budget_24h_usd": 0.5},
        ])
        try:
            assert main(["--config", path, "--instance", "nope"]) == 2
        finally:
            os.unlink(path)

    def test_json_output_is_parseable(self, httpserver: HTTPServer, monkeypatch, capsys):
        path = _write_config([
            {"name": "dev", "base_url": httpserver.url_for("").rstrip("/"),
             "token_ref": "x", "kick_method": "launchctl", "budget_24h_usd": 0.5},
        ])
        monkeypatch.setattr("engine.pe_probe.keychain_get", lambda ref: "tok123")
        _serve_healthy(httpserver)
        try:
            assert main(["--config", path, "--instance", "dev", "--json"]) == 0
        finally:
            os.unlink(path)
        payload = json.loads(capsys.readouterr().out)
        assert payload[0]["instance"] == "dev"
        assert payload[0]["ok"] is True
