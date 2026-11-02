"""A database error must not end a poller, and one odd JSONL line must not end a scan.

On 2026-10-13 poll_loop died on "database is locked" and /api/status served the
same numbers for 19 days. All threads shared one sqlite3 connection, so the
transaction one thread left open pinned a read snapshot for all of them, and the
WAL grew to 23 GB behind it.
"""

import json
import sqlite3
import threading
import time
import unittest.mock as mock
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pytest_httpserver import HTTPServer

from engine import codeburn, sessions
from engine.api import create_server
from engine.db import UsageDB
from engine.pe_config import PEInstance
from engine.pe_poller import _fetch_json, pe_poll_loop
from engine.poller import (
    USAGE_STALE_AFTER,
    TokenHolder,
    _poll_until_stopped,
    _update_status,
    fetch_usage,
    get_current_status,
    poll_loop,
)
from engine.watchdog import Watchdog

_USAGE = {
    "five_hour": {"utilization": 2.0, "resets_at": "2026-11-01T12:00:00+00:00"},
    "seven_day": {"utilization": 20.0, "resets_at": "2026-11-05T12:00:00+00:00"},
}


def _fail_inserts(db: UsageDB, table: str) -> None:
    db._conn.execute(
        f"CREATE TRIGGER fail_insert BEFORE INSERT ON {table} "
        "BEGIN SELECT RAISE(ABORT, 'synthetic database error'); END"
    )
    db._conn.commit()


def _allow_inserts(db: UsageDB) -> None:
    # Takes the write lock from another thread's connection, so a poller that
    # kept its failed transaction open would block this until the timeout.
    db._conn.execute("DROP TRIGGER fail_insert")
    db._conn.commit()


def _wait_for(condition, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def _count(db: UsageDB, table: str) -> int:
    return db._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_an_open_transaction_stays_in_its_thread(tmp_path: Path):
    db = UsageDB(str(tmp_path / "t.db"))

    def leave_transaction_open():
        db._conn.execute(
            "INSERT INTO engine_state (key, value, updated_at) VALUES ('k', 'v', 't')"
        )

    thread = threading.Thread(target=leave_transaction_open)
    thread.start()
    thread.join()

    assert not db._conn.in_transaction
    assert db.get_state("k") is None
    db.close()


def test_poll_loop_survives_a_database_error_and_recovers(tmp_path: Path):
    db = UsageDB(str(tmp_path / "t.db"))
    _fail_inserts(db, "usage_snapshots")
    stop_event = threading.Event()
    calls = []

    def fetch(_holder):
        calls.append(1)
        return _USAGE, None

    with mock.patch("engine.poller.fetch_usage", fetch), mock.patch("engine.poller._update_status"):
        thread = threading.Thread(
            target=poll_loop, args=(mock.Mock(), db, stop_event),
            kwargs={"poll_interval": 0.01}, daemon=True,
        )
        thread.start()
        try:
            assert _wait_for(lambda: len(calls) >= 2), "poller stopped after the first error"
            _allow_inserts(db)
            assert _wait_for(lambda: _count(db, "usage_snapshots") >= 1)
        finally:
            stop_event.set()
            thread.join(timeout=5)
    assert not thread.is_alive()
    db.close()


def test_pe_poll_loop_survives_a_database_error_and_recovers(tmp_path: Path, httpserver: HTTPServer):
    httpserver.expect_request("/api/jobs/summary").respond_with_json({
        "counts": {"queued": 0, "running": 0, "complete_24h": 0, "dead": 0, "failed": 0},
        "oldest_claimable_queued_s": 0, "recent_terminal": [],
    })
    metrics = httpserver.expect_request("/api/admin/router-metrics")
    metrics.respond_with_json({"available": True, "cost_24h_usd": 0.0, "calls": 0})
    instance = PEInstance(
        name="dev", base_url=httpserver.url_for("").rstrip("/"),
        token_ref="x", kick_method="launchctl", budget_24h_usd=1.0,
    )
    db = UsageDB(str(tmp_path / "t.db"))
    _fail_inserts(db, "pe_cost_snapshot")
    stop_event = threading.Event()
    thread = threading.Thread(
        target=pe_poll_loop, args=([instance], db, stop_event),
        kwargs={"get_token": lambda ref: "tok", "jobs_interval": 0.01, "router_interval": 0},
        daemon=True,
    )
    thread.start()
    try:
        assert _wait_for(lambda: len(httpserver.log) >= 4), "PE poller stopped after the first error"
        assert thread.is_alive()
        _allow_inserts(db)
        assert _wait_for(lambda: _count(db, "pe_cost_snapshot") >= 1)
    finally:
        stop_event.set()
        thread.join(timeout=5)
    assert not thread.is_alive()
    db.close()


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_watchdog_restarts_a_worker_after_any_exception():
    stop_event = threading.Event()
    runs = []

    def worker():
        runs.append(1)
        if len(runs) == 1:
            raise TypeError("a bug nobody foresaw")
        stop_event.wait()

    watchdog = Watchdog(stop_event, check_interval=0.01)
    watchdog.add("poller", worker, restart_delay=0)
    watchdog.start()
    try:
        assert _wait_for(lambda: len(runs) == 2), "worker was not restarted"
        status = watchdog.status()["poller"]
        assert status["alive"] and status["restarts"] == 1
        assert "TypeError: a bug nobody foresaw" in status["last_error"]
    finally:
        stop_event.set()


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_watchdog_waits_out_the_restart_delay():
    stop_event = threading.Event()
    run_times = []

    def worker():
        run_times.append(time.monotonic())
        if len(run_times) == 1:
            raise TypeError("boom")
        stop_event.wait()

    watchdog = Watchdog(stop_event, check_interval=0.01)
    watchdog.add("poller", worker, restart_delay=0.5)
    watchdog.start()
    try:
        assert _wait_for(lambda: not watchdog.status()["poller"]["alive"])
        assert _wait_for(lambda: len(run_times) == 2)
        assert run_times[1] - run_times[0] >= 0.5
    finally:
        stop_event.set()


def test_watchdog_does_not_restart_after_stop():
    stop_event = threading.Event()
    runs = []

    def worker():
        runs.append(1)
        stop_event.wait()

    watchdog = Watchdog(stop_event, check_interval=0.01)
    watchdog.add("poller", worker, restart_delay=0)
    watchdog.start()
    assert _wait_for(lambda: len(runs) == 1)
    stop_event.set()
    assert _wait_for(lambda: not watchdog.status()["poller"]["alive"])
    time.sleep(0.1)
    assert len(runs) == 1


class _KeepsConnections(UsageDB):
    """Holds every connection it opens, so only an explicit close can close one.

    Otherwise the cycle collector may close an ended thread's connection by
    chance, and a test passes without the code under test doing it.
    """

    def __init__(self, db_path: str):
        self.opened = []
        super().__init__(db_path)

    def _connect(self):
        conn = super()._connect()
        self.opened.append(conn)
        return conn

    def open_connections(self) -> int:
        count = 0
        for conn in self.opened:
            try:
                conn.execute("SELECT 1")
                count += 1
            except sqlite3.ProgrammingError:
                pass
        return count


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_dead_workers_open_transaction_does_not_block_its_replacement(tmp_path: Path):
    db = _KeepsConnections(str(tmp_path / "t.db"))
    stop_event = threading.Event()
    wrote = []

    def worker():
        if not wrote:
            wrote.append("died holding a write transaction")
            db._conn.execute(
                "INSERT INTO engine_state (key, value, updated_at) VALUES ('dead', 'v', 't')"
            )
            raise TypeError("boom")
        db._conn.execute("PRAGMA busy_timeout=500")
        db.set_state("alive", "v")
        wrote.append("replacement wrote")
        stop_event.wait()

    watchdog = Watchdog(stop_event, check_interval=0.01)
    watchdog.add("poller", worker, restart_delay=0, on_exit=db.release)
    watchdog.start()
    try:
        assert _wait_for(lambda: len(wrote) == 2), watchdog.status()["poller"]
        assert db.get_state("alive") == "v"
        assert db.get_state("dead") is None
    finally:
        stop_event.set()
    db.close()


def test_request_threads_close_their_connections(tmp_path: Path):
    # Left open, they ran the live engine out of file descriptors in 3 hours.
    db = _KeepsConnections(str(tmp_path / "t.db"))
    open_before = db.open_connections()
    server = create_server(db, TokenHolder("t"), port=0, host="127.0.0.1")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        for _ in range(5):
            url = f"http://127.0.0.1:{server.server_address[1]}/api/history"
            with urllib.request.urlopen(url, timeout=5) as resp:
                assert resp.status == 200
    finally:
        server.shutdown()
        server.server_close()
    assert len(db.opened) > open_before, "no request opened a connection; the test proves nothing"
    # finish() runs after the client has its response, so allow it a moment.
    assert _wait_for(lambda: db.open_connections() == open_before, timeout=2), db.open_connections()
    db.close()


@pytest.fixture
def live_status():
    saved = get_current_status()
    yield
    _update_status(saved)


def _health(db: UsageDB, watchdog: Watchdog) -> tuple[int, dict]:
    server = create_server(db, TokenHolder("t"), port=0, host="127.0.0.1", watchdog=watchdog)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/api/health"
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_health_reports_a_dead_worker_and_stays_http_200(tmp_path: Path, live_status):
    # The Swift app spawns a second engine unless /api/health answers 200.
    _update_status({"updated_at": datetime.now(timezone.utc).isoformat()})
    db = UsageDB(str(tmp_path / "t.db"))
    stop_event = threading.Event()
    die = threading.Event()

    def worker():
        die.wait()
        raise TypeError("boom")

    watchdog = Watchdog(stop_event, check_interval=0.01)
    watchdog.add("poller", worker, restart_delay=3600)
    watchdog.start()
    try:
        status, body = _health(db, watchdog)
        assert (status, body["status"]) == (200, "ok")
        assert body["workers"]["poller"]["alive"] is True
        die.set()
        assert _wait_for(lambda: not watchdog.status()["poller"]["alive"])
        status, body = _health(db, watchdog)
        assert (status, body["status"]) == (200, "degraded")
        assert body["workers"]["poller"]["alive"] is False
        assert "TypeError" in body["workers"]["poller"]["last_error"]
    finally:
        stop_event.set()
    db.close()


def test_health_reports_usage_numbers_too_old_to_trust(tmp_path: Path, live_status):
    # What the 19-day freeze looked like, except that this thread is alive.
    db = UsageDB(str(tmp_path / "t.db"))
    stop_event = threading.Event()
    watchdog = Watchdog(stop_event, check_interval=0.01)
    watchdog.add("poller", stop_event.wait, restart_delay=0)
    watchdog.start()
    try:
        too_old = datetime.now(timezone.utc) - timedelta(seconds=USAGE_STALE_AFTER + 60)
        _update_status({"updated_at": too_old.isoformat()})
        _, body = _health(db, watchdog)
        assert body["status"] == "degraded"
        assert body["usage_age_seconds"] > USAGE_STALE_AFTER

        fresh = datetime.now(timezone.utc) - timedelta(seconds=USAGE_STALE_AFTER - 60)
        _update_status({"updated_at": fresh.isoformat()})
        _, body = _health(db, watchdog)
        assert body["status"] == "ok"
    finally:
        stop_event.set()
    db.close()


def test_fetch_usage_treats_a_non_object_response_as_a_failed_fetch(httpserver: HTTPServer):
    httpserver.expect_request("/usage").respond_with_data("[1, 2]", content_type="application/json")
    with mock.patch("engine.poller.API_URL", httpserver.url_for("/usage")):
        assert fetch_usage(TokenHolder("t")) == (None, None)


def test_pe_fetch_treats_a_non_object_response_as_bad_json(httpserver: HTTPServer):
    httpserver.expect_request("/x").respond_with_data("[1, 2]", content_type="application/json")
    assert _fetch_json(httpserver.url_for("/x"), "tok", 5) == (None, "bad_json")


class _StopPolling(Exception):
    pass


def _poll_until_first_fetch(db: UsageDB) -> None:
    with mock.patch("engine.poller.fetch_usage", side_effect=_StopPolling):
        with pytest.raises(_StopPolling):
            _poll_until_stopped(mock.Mock(), db, threading.Event(), 1)


def test_a_restarted_poller_seeds_only_an_empty_status(tmp_path: Path, live_status):
    db = UsageDB(str(tmp_path / "t.db"))
    db.insert_snapshot(
        timestamp="2026-11-01T10:00:00+00:00", five_hour_util=1.0, seven_day_util=2.0,
        sonnet_util=None, five_hour_resets_at=None, seven_day_resets_at="2026-11-05T12:00:00+00:00",
    )
    _update_status({})
    _poll_until_first_fetch(db)
    assert get_current_status()["restored_from_db"] is True

    # A live status has projections; the seed would zero them until the next good poll.
    _update_status({"updated_at": "live"})
    _poll_until_first_fetch(db)
    assert get_current_status() == {"updated_at": "live"}
    db.close()


def _session_file(root: Path) -> None:
    proj = root / "-Users-x-projects-demo"
    proj.mkdir()
    good = {
        "uuid": "u-1", "timestamp": "2026-06-11T10:00:00.000Z", "sessionId": "s", "cwd": "/x",
        "message": {
            "id": "m-1", "role": "assistant", "model": "claude-opus-4-6", "content": [],
            "usage": {"input_tokens": 100000, "output_tokens": 50000},
        },
    }
    bad_timestamp = {"uuid": "u-2", "timestamp": 12345, "sessionId": "s",
                     "message": {"role": "user", "content": "go"}}
    user = {"uuid": "u-0", "timestamp": "2026-06-11T09:59:00.000Z", "sessionId": "s", "cwd": "/x",
            "message": {"role": "user", "content": "go"}}
    lines = ["[1, 2]", '"text"', "7", json.dumps(bad_timestamp), json.dumps(user), json.dumps(good)]
    (proj / "s.jsonl").write_text("\n".join(lines) + "\n")


def test_codeburn_scan_skips_lines_it_cannot_read(tmp_path: Path):
    _session_file(tmp_path)
    attribution = tmp_path / "attribution.jsonl"
    attribution.write_text('[1, 2]\n{"session_id": "s", "project": "demo"}\n')
    with mock.patch.object(codeburn, "_SESSIONS_BASE", str(tmp_path)), \
            mock.patch.object(codeburn, "_ATTRIBUTION_PATH", str(attribution)), \
            mock.patch.object(codeburn, "_attribution_cache", None):
        now = datetime(2026, 6, 11, 12, tzinfo=timezone.utc)
        report = codeburn._scan_sessions(now - timedelta(days=1), now)
        assert codeburn._load_attribution() == {"s": "demo"}
    assert report["total_cost_usd"] > 0


def test_token_history_scan_skips_lines_it_cannot_read(tmp_path: Path):
    _session_file(tmp_path)
    with mock.patch.object(sessions, "_SESSIONS_BASE", str(tmp_path)):
        result = sessions._scan_sessions()
    assert result["totals"]["input"] == 100000
