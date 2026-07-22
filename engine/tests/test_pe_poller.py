# engine/tests/test_pe_poller.py
import unittest

import pytest
from pytest_httpserver import HTTPServer

from engine.pe_config import PEInstance
from engine.pe_poller import fetch_jobs_summary, fetch_router_metrics


@pytest.fixture
def httpserver_instance(httpserver: HTTPServer):
    return PEInstance(
        name="dev", base_url=httpserver.url_for("").rstrip("/"),
        token_ref="x", kick_method="launchctl", budget_24h_usd=1.0,
    )


class TestFetchJobsSummary:
    def test_success_parses_summary(self, httpserver: HTTPServer, httpserver_instance):
        httpserver.expect_request(
            "/api/jobs/summary", headers={"Authorization": "Bearer tok123"}
        ).respond_with_json({
            "counts": {"queued": 2, "running": 1, "complete_24h": 14, "dead": 0, "failed": 2},
            "oldest_claimable_queued_s": 12,
            "recent_terminal": [{"job_id": "j1", "status": "failed", "topic": "t",
                                  "error": "e", "updated_at": "2026-07-22T00:00:00Z"}],
        })
        result, error = fetch_jobs_summary(httpserver_instance, token="tok123", timeout=5)
        assert error is None
        assert result["counts"]["queued"] == 2
        assert result["oldest_claimable_queued_s"] == 12

    def test_401_returns_error(self, httpserver: HTTPServer, httpserver_instance):
        httpserver.expect_request("/api/jobs/summary").respond_with_json(
            {"error": "unauthorized"}, status=401
        )
        result, error = fetch_jobs_summary(httpserver_instance, token="bad", timeout=5)
        assert result is None
        assert error == "http_401"

    def test_timeout_returns_error(self, httpserver_instance):
        unreachable = PEInstance(
            name="dev", base_url="http://127.0.0.1:1", token_ref="x",
            kick_method="launchctl", budget_24h_usd=1.0,
        )
        result, error = fetch_jobs_summary(unreachable, token="x", timeout=1)
        assert result is None
        assert error is not None


class TestFetchRouterMetrics:
    def test_available_true_parses_cost(self, httpserver: HTTPServer, httpserver_instance):
        httpserver.expect_request("/api/admin/router-metrics").respond_with_json({
            "available": True, "cost_24h_usd": 0.0008, "calls": 12,
        })
        result, error = fetch_router_metrics(httpserver_instance, token="tok123", timeout=5)
        assert error is None
        assert result["available"] is True
        assert result["cost_24h_usd"] == 0.0008

    def test_available_false_never_reported_as_zero_error(self, httpserver: HTTPServer, httpserver_instance):
        httpserver.expect_request("/api/admin/router-metrics").respond_with_json({
            "available": False,
        })
        result, error = fetch_router_metrics(httpserver_instance, token="tok123", timeout=5)
        assert error is None
        assert result["available"] is False


if __name__ == "__main__":
    unittest.main()
