"""A database error must not end a poller, and one odd JSONL line must not end a scan.

On 2026-10-13 poll_loop died on "database is locked" and /api/status served the
same numbers for 19 days. All threads shared one sqlite3 connection, so the
transaction one thread left open pinned a read snapshot for all of them, and the
WAL grew to 23 GB behind it.
"""

import json
import threading
import time
import unittest.mock as mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pytest_httpserver import HTTPServer

from engine import codeburn, sessions
from engine.db import UsageDB
from engine.pe_config import PEInstance
from engine.pe_poller import pe_poll_loop
from engine.poller import poll_loop

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
