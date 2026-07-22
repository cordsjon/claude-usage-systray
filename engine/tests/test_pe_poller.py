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


# ── Stall + budget rule tests (pure functions, no I/O) ──────────

from engine.pe_poller import compute_stalled, compute_budget_crossed, make_alert_id


class TestComputeStalled:
    def test_stalled_true_when_old_claimable_and_no_running(self):
        assert compute_stalled(oldest_claimable_queued_s=181, running=0) is True

    def test_not_stalled_under_threshold(self):
        assert compute_stalled(oldest_claimable_queued_s=179, running=0) is False

    def test_not_stalled_when_worker_running(self):
        assert compute_stalled(oldest_claimable_queued_s=999, running=1) is False


class TestComputeBudgetCrossed:
    def test_crosses_at_or_above_target(self):
        assert compute_budget_crossed(cost_24h_usd=0.5, budget_24h_usd=0.5, currently_crossed=False) is True

    def test_not_crossed_below_target(self):
        assert compute_budget_crossed(cost_24h_usd=0.4, budget_24h_usd=0.5, currently_crossed=False) is False

    def test_rearm_only_below_90_percent_hysteresis(self):
        # was crossed; still above 90% of target -> stays crossed (no re-arm yet)
        assert compute_budget_crossed(cost_24h_usd=0.46, budget_24h_usd=0.5, currently_crossed=True) is True
        # drops below 90% (0.45) -> re-arms
        assert compute_budget_crossed(cost_24h_usd=0.44, budget_24h_usd=0.5, currently_crossed=True) is False

    def test_unavailable_never_triggers(self):
        # caller is responsible for not calling this when available=False;
        # this function assumes cost_24h_usd is a real number when called.
        assert compute_budget_crossed(cost_24h_usd=0.0, budget_24h_usd=0.5, currently_crossed=False) is False


class TestMakeAlertId:
    def test_stall_id_shape(self):
        aid = make_alert_id("stalled", "dev", first_seen="2026-07-21T22:58:00Z")
        assert aid == "stalled:dev:2026-07-21T22:58:00Z"

    def test_dead_job_id_uses_job_id_not_timestamp(self):
        aid = make_alert_id("dead", "dev", job_id="1b4d1c31")
        assert aid == "dead:dev:1b4d1c31"


# ── Poll loop integration (one iteration, injected clock/instances) ──

import threading

from engine.db import UsageDB
from engine.pe_poller import pe_poll_once, get_current_pe_status


class TestPePollOnce:
    def test_single_poll_persists_snapshot_and_updates_status(self, httpserver: HTTPServer):
        httpserver.expect_request("/api/jobs/summary").respond_with_json({
            "counts": {"queued": 0, "running": 1, "complete_24h": 14, "dead": 0, "failed": 0},
            "oldest_claimable_queued_s": 0,
            "recent_terminal": [],
        })
        httpserver.expect_request("/api/admin/router-metrics").respond_with_json({
            "available": True, "cost_24h_usd": 0.05, "calls": 3,
        })
        instance = PEInstance(
            name="dev", base_url=httpserver.url_for("").rstrip("/"),
            token_ref="x", kick_method="launchctl", budget_24h_usd=1.0,
        )
        db = UsageDB(":memory:")
        try:
            pe_poll_once(instance, db, token="tok", now_iso="2026-07-22T00:00:00Z")
            snap = db.get_latest_pe_cost_snapshot("dev")
            assert snap["cost_24h_usd"] == 0.05
            status = get_current_pe_status()
            assert status["dev"]["reachable"] is True
            assert status["dev"]["stalled"] is False
        finally:
            db.close()

    def test_unreachable_after_three_consecutive_misses(self, httpserver: HTTPServer):
        httpserver.expect_request("/api/jobs/summary").respond_with_json(
            {"error": "boom"}, status=500
        )
        instance = PEInstance(
            name="dev", base_url=httpserver.url_for("").rstrip("/"),
            token_ref="x", kick_method="launchctl", budget_24h_usd=1.0,
        )
        db = UsageDB(":memory:")
        try:
            for _ in range(3):
                pe_poll_once(instance, db, token="tok", now_iso="2026-07-22T00:00:00Z")
            status = get_current_pe_status()
            assert status["dev"]["reachable"] is False
        finally:
            db.close()


def test_skipped_router_poll_falls_back_to_last_snapshot(httpserver: HTTPServer):
    httpserver.expect_request("/api/jobs/summary").respond_with_json({
        "counts": {"queued": 0, "running": 1, "complete_24h": 0, "dead": 0, "failed": 0},
        "oldest_claimable_queued_s": 0, "recent_terminal": [],
    })
    # No /api/admin/router-metrics expectation registered — if pe_poll_once
    # calls it anyway with fetch_router=False, pytest_httpserver raises
    # AssertionFailedError for the unexpected request, failing this test.
    instance = PEInstance(
        name="dev", base_url=httpserver.url_for("").rstrip("/"),
        token_ref="x", kick_method="launchctl", budget_24h_usd=1.0,
    )
    db = UsageDB(":memory:")
    try:
        db.insert_pe_cost_snapshot(
            ts="2026-07-21T23:59:00Z", instance="dev",
            cost_24h_usd=0.42, calls=7, available=True,
        )
        pe_poll_once(instance, db, token="tok", now_iso="2026-07-22T00:00:00Z", fetch_router=False)
        status = get_current_pe_status()
        assert status["dev"]["cost"]["d24h_usd"] == 0.42
        assert status["dev"]["cost"]["calls"] == 7
        assert status["dev"]["cost"]["available"] is True
    finally:
        db.close()


from engine.pe_poller import pe_poll_loop


class TestPePollLoop:
    def test_loop_stops_promptly_on_stop_event(self, httpserver: HTTPServer):
        httpserver.expect_request("/api/jobs/summary").respond_with_json({
            "counts": {"queued": 0, "running": 0, "complete_24h": 0, "dead": 0, "failed": 0},
            "oldest_claimable_queued_s": 0, "recent_terminal": [],
        })
        httpserver.expect_request("/api/admin/router-metrics").respond_with_json({
            "available": True, "cost_24h_usd": 0.0, "calls": 0,
        })
        instance = PEInstance(
            name="dev", base_url=httpserver.url_for("").rstrip("/"),
            token_ref="x", kick_method="launchctl", budget_24h_usd=1.0,
        )
        db = UsageDB(":memory:")
        stop_event = threading.Event()
        thread = threading.Thread(
            target=pe_poll_loop,
            args=([instance], db, stop_event),
            kwargs={"get_token": lambda ref: "tok", "jobs_interval": 1, "router_interval": 1},
            daemon=True,
        )
        thread.start()
        import time
        time.sleep(0.3)
        stop_event.set()
        thread.join(timeout=3)
        assert not thread.is_alive()
        db.close()


if __name__ == "__main__":
    unittest.main()
