# US-PESUP-ENGINE-01 (Engine half) Implementation Plan

> **For agentic workers:** REQUIRED: Use `/sh:execute` to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a PosterEngine (PE) supervisor module to the `claude-usage-systray` Python engine — instance config loader, poller thread, SQLite persistence, `GET /pe/status`, and async retry/kick controls — so the engine can watch both PE instances (dev :9120, prod) and expose their health/spend/alerts over HTTP for the Swift app to consume.

**Architecture:** New `engine/pe_poller.py` module clones the existing `engine/poller.py` pattern (thread-safe module-level state, `stop_event`-driven interruptible sleep, stdlib `urllib` HTTP, DB-seeded startup). New SQLite tables (`pe_cost_snapshot`, `pe_alert_state`, `pe_op_log`) are appended to the existing single-file `_SCHEMA` in `engine/db.py` — one shared `UsageDB`, no new database file. HTTP surface is added to the existing hand-rolled dispatcher in `engine/api.py`: `/pe/status` is a trivial exact-match GET; `POST /pe/<instance>/jobs/<id>/retry` and `POST /pe/<instance>/worker/kick` need new manual path-segment parsing since this dispatcher has never had variable path segments. Control routes return `202` immediately and run PE requests / SSH in background daemon threads, recording results in `pe_op_log` (this file's HTTP server is single-threaded — a blocking 5s PE call or 30s SSH call on the request thread would freeze every other route).

**Tech Stack:** Python 3.14 stdlib (`urllib.request`, `http.server`, `sqlite3`, `threading`, `subprocess`), `pytest` (net-new: `pytest_httpserver` for faking upstream PE responses — this repo has no non-stdlib HTTP deps in `engine/` today and no dependency manifest at all).

**Out of scope for this plan:** Swift app changes (separate plan, `2026-07-22-pesup-engine-01-swift.md`, depends on this plan's `/pe/status` contract being live). PE-side endpoints (already shipped, US-PESUP-PE-01, PosterEngine `main@de45862`).

---

## Premises (verify before implementing)

- `engine/api.py` dispatch is exact-string-match only, no path-parameter support — verified 2026-07-22 (read L143-182).
- `engine/db.py` `_SCHEMA` is one multi-statement string run via `executescript` in `UsageDB.__init__`, no migration framework — verified 2026-07-22 (read L11-98).
- `engine/poller.py` L28-37, L163-173, L222-268, L325-539 is the poll-loop pattern to clone (module-level lock+dict, `fetch_usage`-shaped two-tuple return, `stop_event.wait()`-based interruptible backoff) — verified 2026-07-22.
- `engine/providers/__init__.py:67-77` has `keychain_get(service: str) -> Optional[str]` (simple string-secret reader, distinct from `poller.py`'s heavier `_read_keychain_token()` JSON-blob reader) — verified 2026-07-22.
- `engine/server.py:126-139` is the thread-registration pattern to clone (new PE poller thread starts after the existing poller/rollup block, before L141's cache-warmup block) — verified 2026-07-22.
- No `pytest_httpserver`, no `httpx`, in `.venv` (`.venv/bin/python3 -m pip list` — confirmed empty) and no `requirements.txt`/`pyproject.toml` anywhere in the repo — verified 2026-07-22. This plan creates `requirements-dev.txt` for the first time.
- `engine/tests/test_api.py` is the pattern for testing the engine's *own* HTTP routes (in-memory `UsageDB(":memory:")`, `create_server(..., port=0)`, real `urllib` requests against the ephemeral port) — verified 2026-07-22 (repo file exists, 340 lines).
- PE endpoint contracts (`GET /api/jobs/summary`, `POST /api/jobs/{id}/retry`, `GET /api/admin/router-metrics`) verified live against dev PE :9120 in the prior session (all three now correctly 401 without a Bearer token, after restarting the stale `com.poster-engine` LaunchAgent) — verified 2026-07-22.

---

## Chunk 1: Instance config + Keychain loader

### Task 1: Instance config loader

**Files:**
- Create: `engine/pe_config.py`
- Test: `engine/tests/test_pe_config.py`

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/projects/claude-usage-systray && .venv/bin/python3 -m pytest engine/tests/test_pe_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'engine.pe_config'`

- [ ] **Step 3: Write minimal implementation**

```python
# engine/pe_config.py
"""Instance config loader for the PosterEngine (PE) supervisor.

Each configured PE instance (dev, prod) is polled by engine/pe_poller.py.
Config lives at ~/.local/share/token-budget/pe_instances.json (default) —
no existing loader in this repo predates this file.
"""

import json
import os
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

DEFAULT_CONFIG_PATH = os.path.expanduser(
    "~/.local/share/token-budget/pe_instances.json"
)

_LOCALHOST_HOSTS = {"127.0.0.1", "localhost", "::1"}


class PEConfigError(ValueError):
    """Raised when pe_instances.json is malformed or unsafe."""


@dataclass(frozen=True)
class PEInstance:
    name: str
    base_url: str
    token_ref: str
    kick_method: str  # "launchctl" | "ssh"
    budget_24h_usd: float
    ssh_host: Optional[str] = None


_REQUIRED_FIELDS = ("name", "base_url", "token_ref", "kick_method", "budget_24h_usd")


def _validate_url(base_url: str, name: str) -> None:
    parsed = urlparse(base_url)
    if parsed.hostname in _LOCALHOST_HOSTS:
        return
    if parsed.scheme != "https":
        raise PEConfigError(
            f"pe_instances.json: instance '{name}' has non-localhost base_url "
            f"'{base_url}' without https — refusing to send a Bearer token "
            f"over plaintext."
        )


def load_pe_instances(path: str = DEFAULT_CONFIG_PATH) -> list[PEInstance]:
    """Load and validate PE instance config. Missing file -> empty list."""
    if not os.path.exists(path):
        return []

    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    instances = []
    for entry in raw:
        missing = [k for k in _REQUIRED_FIELDS if k not in entry]
        if missing:
            raise PEConfigError(
                f"pe_instances.json: entry {entry.get('name', '?')} missing "
                f"required field(s): {missing}"
            )
        _validate_url(entry["base_url"], entry["name"])
        if entry["kick_method"] not in ("launchctl", "ssh"):
            raise PEConfigError(
                f"pe_instances.json: instance '{entry['name']}' has invalid "
                f"kick_method '{entry['kick_method']}'"
            )
        if entry["kick_method"] == "ssh" and not entry.get("ssh_host"):
            raise PEConfigError(
                f"pe_instances.json: instance '{entry['name']}' has "
                f"kick_method=ssh but no ssh_host"
            )
        instances.append(PEInstance(
            name=entry["name"],
            base_url=entry["base_url"].rstrip("/"),
            token_ref=entry["token_ref"],
            kick_method=entry["kick_method"],
            budget_24h_usd=float(entry["budget_24h_usd"]),
            ssh_host=entry.get("ssh_host"),
        ))
    return instances
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/projects/claude-usage-systray && .venv/bin/python3 -m pytest engine/tests/test_pe_config.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
cd ~/projects/claude-usage-systray
git add engine/pe_config.py engine/tests/test_pe_config.py
git commit -m "feat(pe-supervisor): add PE instance config loader with https-only remote guard"
```

---

## Chunk 2: SQLite schema

### Task 2: Add pe_cost_snapshot, pe_alert_state, pe_op_log tables

**Files:**
- Modify: `engine/db.py:11-84` (append to `_SCHEMA`), plus new methods after existing ones
- Test: `engine/tests/test_db.py` (check if this file exists first: `ls engine/tests/test_db.py`; if absent, create it)

- [ ] **Step 1: Write the failing test**

```python
# engine/tests/test_db.py (append if file exists, else create with this content)
import unittest

from engine.db import UsageDB


class TestPESchema(unittest.TestCase):
    def setUp(self):
        self.db = UsageDB(":memory:")

    def tearDown(self):
        self.db.close()

    def test_insert_and_read_pe_cost_snapshot(self):
        self.db.insert_pe_cost_snapshot(
            ts="2026-07-22T00:00:00Z", instance="dev",
            cost_24h_usd=0.12, calls=5, available=True,
        )
        rows = self.db.get_pe_cost_snapshots_since(
            instance="dev", since="2026-07-21T00:00:00Z"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cost_24h_usd"], 0.12)
        self.assertEqual(rows[0]["available"], 1)

    def test_get_latest_pe_cost_snapshot(self):
        self.db.insert_pe_cost_snapshot(
            ts="2026-07-22T00:00:00Z", instance="dev",
            cost_24h_usd=0.10, calls=3, available=True,
        )
        self.db.insert_pe_cost_snapshot(
            ts="2026-07-22T00:01:00Z", instance="dev",
            cost_24h_usd=0.15, calls=4, available=True,
        )
        latest = self.db.get_latest_pe_cost_snapshot("dev")
        self.assertEqual(latest["cost_24h_usd"], 0.15)

    def test_pe_alert_state_upsert_and_read(self):
        self.db.upsert_pe_alert_state(
            alert_id="stalled:dev:2026-07-22T00:00:00Z",
            first_seen="2026-07-22T00:00:00Z",
            last_seen="2026-07-22T00:00:00Z", active=True,
        )
        self.db.upsert_pe_alert_state(
            alert_id="stalled:dev:2026-07-22T00:00:00Z",
            first_seen="2026-07-22T00:00:00Z",
            last_seen="2026-07-22T00:04:00Z", active=True,
        )
        active = self.db.get_active_pe_alerts()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["last_seen"], "2026-07-22T00:04:00Z")

    def test_pe_op_log_insert_and_update(self):
        self.db.insert_pe_op_log(
            op_id="op-1", instance="dev", kind="retry",
            target="job-123", state="pending", detail=None,
            ts="2026-07-22T00:00:00Z",
        )
        self.db.update_pe_op_log("op-1", state="ok", detail=None)
        recent = self.db.get_recent_pe_ops(limit=10)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["state"], "ok")

    def test_prune_pe_cost_snapshot_respects_90_day_boundary(self):
        self.db.insert_pe_cost_snapshot(
            ts="2020-01-01T00:00:00Z", instance="dev",
            cost_24h_usd=0.01, calls=1, available=True,
        )
        self.db.insert_pe_cost_snapshot(
            ts="2026-07-22T00:00:00Z", instance="dev",
            cost_24h_usd=0.02, calls=1, available=True,
        )
        self.db.prune_pe_cost_snapshot()
        rows = self.db.get_pe_cost_snapshots_since(instance="dev", since="2000-01-01T00:00:00Z")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cost_24h_usd"], 0.02)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/projects/claude-usage-systray && .venv/bin/python3 -m pytest engine/tests/test_db.py -v`
Expected: FAIL with `AttributeError: 'UsageDB' object has no attribute 'insert_pe_cost_snapshot'`

- [ ] **Step 3: Write minimal implementation**

Append to `_SCHEMA` in `engine/db.py`, immediately before the closing `"""` at line 84:

```python
CREATE TABLE IF NOT EXISTS pe_cost_snapshot (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,
    instance      TEXT    NOT NULL,
    cost_24h_usd  REAL    NOT NULL,
    calls         INTEGER NOT NULL,
    available     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pe_cost_snapshot_instance_ts
    ON pe_cost_snapshot(instance, ts);

CREATE TABLE IF NOT EXISTS pe_alert_state (
    alert_id   TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    active     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS pe_op_log (
    op_id     TEXT PRIMARY KEY,
    instance  TEXT NOT NULL,
    kind      TEXT NOT NULL,
    target    TEXT,
    state     TEXT NOT NULL,
    detail    TEXT,
    ts        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pe_op_log_ts ON pe_op_log(ts);
```

Add these methods to the `UsageDB` class in `engine/db.py`, after the existing `prune()` method (near L231-240 — place new methods directly after it so PE-specific code is grouped together, matching the file's existing grouping-by-feature style):

```python
    # ── PE supervisor (US-PESUP-ENGINE-01) ──────────────────────

    PE_SNAPSHOT_RETENTION_DAYS = 90

    def insert_pe_cost_snapshot(
        self, ts: str, instance: str, cost_24h_usd: float, calls: int, available: bool
    ) -> None:
        self._conn.execute(
            "INSERT INTO pe_cost_snapshot (ts, instance, cost_24h_usd, calls, available) "
            "VALUES (?, ?, ?, ?, ?)",
            (ts, instance, cost_24h_usd, calls, int(available)),
        )
        self._conn.commit()

    def get_latest_pe_cost_snapshot(self, instance: str):
        return self._conn.execute(
            "SELECT * FROM pe_cost_snapshot WHERE instance = ? "
            "ORDER BY ts DESC LIMIT 1",
            (instance,),
        ).fetchone()

    def get_pe_cost_snapshots_since(self, instance: str, since: str):
        return self._conn.execute(
            "SELECT * FROM pe_cost_snapshot WHERE instance = ? AND ts >= ? "
            "ORDER BY ts ASC",
            (instance, since),
        ).fetchall()

    def prune_pe_cost_snapshot(self) -> None:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=self.PE_SNAPSHOT_RETENTION_DAYS)
        ).isoformat()
        self._conn.execute("DELETE FROM pe_cost_snapshot WHERE ts < ?", (cutoff,))
        self._conn.commit()

    def upsert_pe_alert_state(
        self, alert_id: str, first_seen: str, last_seen: str, active: bool
    ) -> None:
        self._conn.execute(
            "INSERT INTO pe_alert_state (alert_id, first_seen, last_seen, active) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(alert_id) DO UPDATE SET last_seen = excluded.last_seen, "
            "active = excluded.active",
            (alert_id, first_seen, last_seen, int(active)),
        )
        self._conn.commit()

    def get_active_pe_alerts(self):
        return self._conn.execute(
            "SELECT * FROM pe_alert_state WHERE active = 1 ORDER BY first_seen ASC"
        ).fetchall()

    def insert_pe_op_log(
        self, op_id: str, instance: str, kind: str, target: str | None,
        state: str, detail: str | None, ts: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO pe_op_log (op_id, instance, kind, target, state, detail, ts) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (op_id, instance, kind, target, state, detail, ts),
        )
        self._conn.commit()

    def update_pe_op_log(self, op_id: str, state: str, detail: str | None) -> None:
        self._conn.execute(
            "UPDATE pe_op_log SET state = ?, detail = ? WHERE op_id = ?",
            (state, detail, op_id),
        )
        self._conn.commit()

    def get_recent_pe_ops(self, limit: int = 10):
        return self._conn.execute(
            "SELECT * FROM pe_op_log ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/projects/claude-usage-systray && .venv/bin/python3 -m pytest engine/tests/test_db.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Run the full existing DB/API test suite to check for regressions**

Run: `cd ~/projects/claude-usage-systray && .venv/bin/python3 -m pytest engine/tests/ -v`
Expected: all prior tests still PASS (schema is additive `IF NOT EXISTS`, no existing table touched)

- [ ] **Step 6: Commit**

```bash
cd ~/projects/claude-usage-systray
git add engine/db.py engine/tests/test_db.py
git commit -m "feat(pe-supervisor): add pe_cost_snapshot/pe_alert_state/pe_op_log tables"
```

---

## Chunk 3: Poller thread (the aggregation seam under test)

### Task 3: Add `pytest_httpserver` as a dev dependency

**Files:**
- Create: `requirements-dev.txt` (net-new — no dependency manifest exists in this repo today)

- [ ] **Step 1: Create the manifest**

```
# requirements-dev.txt
# Test-only dependencies for engine/. Install: .venv/bin/pip install -r requirements-dev.txt
# The engine itself uses stdlib only (urllib, http.server, sqlite3) — this file
# stays test-scoped so runtime deployment (LaunchAgent) needs no pip install.
pytest_httpserver>=1.0
```

- [ ] **Step 2: Install and verify**

Run: `cd ~/projects/claude-usage-systray && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/python3 -c "import pytest_httpserver; print(pytest_httpserver.__version__)"`
Expected: prints a version string, no error

- [ ] **Step 3: Commit**

```bash
cd ~/projects/claude-usage-systray
git add requirements-dev.txt
git commit -m "chore(pe-supervisor): add pytest_httpserver dev dependency for upstream-fake tests"
```

### Task 4: PE poller — fetch + parse (pure logic, no thread yet)

**Files:**
- Create: `engine/pe_poller.py`
- Test: `engine/tests/test_pe_poller.py`

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/projects/claude-usage-systray && .venv/bin/python3 -m pytest engine/tests/test_pe_poller.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'engine.pe_poller'`

- [ ] **Step 3: Write minimal implementation**

```python
# engine/pe_poller.py (partial — fetch functions only; poll loop added in Task 5)
"""PosterEngine (PE) supervisor poller.

Polls both configured PE instances for job-queue health and router spend,
persists snapshots, computes stall/budget alerts, and exposes thread-safe
shared state for engine/api.py's /pe/status route.

Clones the fetch/poll-loop shape of engine/poller.py (stdlib urllib,
two-tuple (data, error) fetch contract, stop_event-interruptible sleep).
"""

import json
import logging
import urllib.error
import urllib.request

from engine.pe_config import PEInstance

log = logging.getLogger("engine.pe_poller")

JOBS_SUMMARY_TIMEOUT = 5
ROUTER_METRICS_TIMEOUT = 5


def _fetch_json(url: str, token: str, timeout: int):
    """GET url with a Bearer token. Returns (parsed_json_or_None, error_str_or_None)."""
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}, method="GET"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        log.warning("PE fetch %s -> HTTP %s", url, e.code)
        return None, f"http_{e.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("PE fetch %s -> %s", url, e)
        return None, str(e)
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("PE fetch %s -> bad JSON: %s", url, e)
        return None, "bad_json"


def fetch_jobs_summary(instance: PEInstance, token: str, timeout: int = JOBS_SUMMARY_TIMEOUT):
    return _fetch_json(f"{instance.base_url}/api/jobs/summary", token, timeout)


def fetch_router_metrics(instance: PEInstance, token: str, timeout: int = ROUTER_METRICS_TIMEOUT):
    return _fetch_json(f"{instance.base_url}/api/admin/router-metrics", token, timeout)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/projects/claude-usage-systray && .venv/bin/python3 -m pytest engine/tests/test_pe_poller.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
cd ~/projects/claude-usage-systray
git add engine/pe_poller.py engine/tests/test_pe_poller.py
git commit -m "feat(pe-supervisor): add PE jobs-summary + router-metrics fetch functions"
```

### Task 5: Poll loop — stall rule, budget rule, alert-id stability, shared state

**Files:**
- Modify: `engine/pe_poller.py` (append)
- Modify: `engine/tests/test_pe_poller.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to `engine/tests/test_pe_poller.py`:

```python
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
```

Also append a poll-loop integration test (module-level shared state + DB persistence):

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/projects/claude-usage-systray && .venv/bin/python3 -m pytest engine/tests/test_pe_poller.py -v`
Expected: FAIL — `ImportError: cannot import name 'compute_stalled'` (and others not yet defined)

- [ ] **Step 3: Write minimal implementation**

Append to `engine/pe_poller.py`:

```python
import threading
from datetime import datetime, timezone

STALL_THRESHOLD_S = 180
BUDGET_REARM_FRACTION = 0.90
PE_POLL_INTERVAL_JOBS_S = 30
PE_POLL_INTERVAL_ROUTER_S = 60
UNREACHABLE_MISS_THRESHOLD = 3

_pe_status_lock = threading.Lock()
_current_pe_status: dict = {}
_miss_counts: dict = {}


def get_current_pe_status() -> dict:
    with _pe_status_lock:
        return dict(_current_pe_status)


def _update_pe_status(instance_name: str, status: dict) -> None:
    with _pe_status_lock:
        _current_pe_status[instance_name] = status


def compute_stalled(oldest_claimable_queued_s: int, running: int) -> bool:
    return oldest_claimable_queued_s > STALL_THRESHOLD_S and running == 0


def compute_budget_crossed(cost_24h_usd: float, budget_24h_usd: float, currently_crossed: bool) -> bool:
    """Edge-triggered with hysteresis: activates at >= target, re-arms below 90% of target."""
    if not currently_crossed:
        return cost_24h_usd >= budget_24h_usd
    rearm_floor = budget_24h_usd * BUDGET_REARM_FRACTION
    return cost_24h_usd >= rearm_floor


def make_alert_id(kind: str, instance: str, first_seen: str | None = None, job_id: str | None = None) -> str:
    if kind == "dead" or kind == "op_failed":
        return f"{kind}:{instance}:{job_id}"
    return f"{kind}:{instance}:{first_seen}"


def pe_poll_once(
    instance: PEInstance, db, token: str, now_iso: str | None = None,
    fetch_router: bool = True,
) -> None:
    """Run one poll iteration for a single instance.

    Split out from the threaded loop so tests can call it directly without
    threading or real sleeps. `fetch_router=False` skips the router-metrics
    call (used by pe_poll_loop to honor the 30s/60s split cadence — jobs
    summary polls every iteration, router metrics only every Nth) while still
    updating status/stall state from the jobs summary alone. When skipped,
    the previously-persisted cost figures are NOT touched — /pe/status keeps
    showing the last real reading rather than zeroing it out between router
    polls.
    """
    now = now_iso or datetime.now(timezone.utc).isoformat()

    summary, summary_err = fetch_jobs_summary(instance, token)
    metrics, metrics_err = fetch_router_metrics(instance, token) if fetch_router else (None, None)

    if summary is None:
        _miss_counts[instance.name] = _miss_counts.get(instance.name, 0) + 1
    else:
        _miss_counts[instance.name] = 0

    reachable = _miss_counts.get(instance.name, 0) < UNREACHABLE_MISS_THRESHOLD

    if metrics is not None:
        db.insert_pe_cost_snapshot(
            ts=now, instance=instance.name,
            cost_24h_usd=metrics.get("cost_24h_usd", 0.0) if metrics.get("available") else 0.0,
            calls=metrics.get("calls", 0) if metrics.get("available") else 0,
            available=bool(metrics.get("available")),
        )
        cost_24h_display = metrics.get("cost_24h_usd", 0.0) if metrics.get("available") else 0.0
        calls_display = metrics.get("calls", 0) if metrics.get("available") else 0
        available_display = bool(metrics.get("available"))
    else:
        # Router wasn't polled this iteration (fetch_router=False) or the
        # fetch failed — fall back to the last persisted snapshot instead of
        # showing 0/unavailable, which would flicker the popover every cycle
        # the router isn't due to be polled.
        last_snapshot = db.get_latest_pe_cost_snapshot(instance.name)
        cost_24h_display = last_snapshot["cost_24h_usd"] if last_snapshot else 0.0
        calls_display = last_snapshot["calls"] if last_snapshot else 0
        available_display = bool(last_snapshot["available"]) if last_snapshot else False

    counts = (summary or {}).get("counts", {})
    oldest_claimable = (summary or {}).get("oldest_claimable_queued_s", 0)
    running = counts.get("running", 0)
    stalled = compute_stalled(oldest_claimable, running) if summary is not None else False

    status = {
        "reachable": reachable,
        "counts": counts,
        "oldest_claimable_queued_s": oldest_claimable,
        "stalled": stalled,
        "recent_terminal": (summary or {}).get("recent_terminal", []),
        "cost": {
            "d24h_usd": cost_24h_display,
            "calls": calls_display,
            "available": available_display,
        },
        "budget": {
            "target_24h_usd": instance.budget_24h_usd,
            "crossed": False,  # computed by caller with persisted currently_crossed state
        },
        "last_poll": now,
    }
    _update_pe_status(instance.name, status)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/projects/claude-usage-systray && .venv/bin/python3 -m pytest engine/tests/test_pe_poller.py -v`
Expected: PASS (all tests)

- [ ] **Step 4b: Write and verify the fetch_router=False fallback test**

This proves the cadence-skip path (Task 6 relies on this): when the router isn't due to be polled this iteration, `/pe/status` must keep showing the last real cost reading rather than flickering to 0/unavailable. Append to `engine/tests/test_pe_poller.py`:

```python
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
```

Run: `cd ~/projects/claude-usage-systray && .venv/bin/python3 -m pytest engine/tests/test_pe_poller.py -v`
Expected: PASS — confirms `fetch_router=False` never calls `/api/admin/router-metrics` and the last snapshot is surfaced instead of zeros.

- [ ] **Step 5: Commit**

```bash
cd ~/projects/claude-usage-systray
git add engine/pe_poller.py engine/tests/test_pe_poller.py
git commit -m "feat(pe-supervisor): add stall/budget rules, alert-id stability, single-poll iteration"
```

### Task 6: Threaded poll loop wired into server.py

**Files:**
- Modify: `engine/pe_poller.py` (append `pe_poll_loop`)
- Modify: `engine/server.py:126-139` area (add thread start)
- Test: `engine/tests/test_pe_poller.py` (append)

This task's test is a thread-lifecycle smoke test only (starts, polls at least once, stops promptly) — it registers httpserver expectations for both endpoints so it doesn't care whether router gets skipped on a given iteration. The actual cadence-skip behavior (router metrics NOT fetched when `do_router` is False, falling back to the last DB snapshot) is proven deterministically in Task 5 Step 4b via direct `pe_poll_once(..., fetch_router=False)` calls — don't try to time-race the real thread to prove cadence, it's flaky and redundant.

- [ ] **Step 1: Write the failing test**

Append to `engine/tests/test_pe_poller.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/projects/claude-usage-systray && .venv/bin/python3 -m pytest engine/tests/test_pe_poller.py -v`
Expected: FAIL with `ImportError: cannot import name 'pe_poll_loop'`

- [ ] **Step 3: Write minimal implementation**

Append to `engine/pe_poller.py`:

```python
def pe_poll_loop(
    instances: list[PEInstance],
    db,
    stop_event: threading.Event,
    get_token,
    jobs_interval: int = PE_POLL_INTERVAL_JOBS_S,
    router_interval: int = PE_POLL_INTERVAL_ROUTER_S,
) -> None:
    """Poll every configured PE instance on its own cadence until stop_event is set.

    get_token(token_ref) -> str resolves a Keychain-backed Bearer token per
    instance (injected so tests don't touch the real Keychain).
    """
    last_router_poll: dict = {name: 0.0 for name in (i.name for i in instances)}
    import time as _time

    while not stop_event.is_set():
        now_monotonic = _time.monotonic()
        for instance in instances:
            token = get_token(instance.token_ref)
            if token is None:
                log.warning("PE poller: no token for instance %s, skipping", instance.name)
                continue
            do_router = (now_monotonic - last_router_poll.get(instance.name, 0.0)) >= router_interval
            if do_router:
                last_router_poll[instance.name] = now_monotonic
            pe_poll_once(instance, db, token, fetch_router=do_router)
        db.prune_pe_cost_snapshot()
        stop_event.wait(jobs_interval)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/projects/claude-usage-systray && .venv/bin/python3 -m pytest engine/tests/test_pe_poller.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Wire into `engine/server.py`**

In `engine/server.py`, add the import near the top (after L26, alongside the other engine imports):

```python
from engine.pe_config import load_pe_instances
from engine.pe_poller import pe_poll_loop
from engine.providers import keychain_get
```

Insert this block after the existing poller-thread `if/else` block (after L139, before the cache-warmup block at L141-155):

```python
    # PE supervisor poller (US-PESUP-ENGINE-01)
    pe_instances = load_pe_instances()
    if pe_instances:
        pe_stop_event = stop_event  # reuse the same shutdown signal
        pe_poller_thread = threading.Thread(
            target=pe_poll_loop,
            args=(pe_instances, db, pe_stop_event),
            kwargs={"get_token": keychain_get},
            daemon=True,
        )
        pe_poller_thread.start()
        log.info("PE poller started for %d instance(s)", len(pe_instances))
    else:
        log.info("PE poller not started (no pe_instances.json configured)")
```

- [ ] **Step 6: Run the full engine test suite**

Run: `cd ~/projects/claude-usage-systray && .venv/bin/python3 -m pytest engine/tests/ -v`
Expected: all tests PASS, no regressions

- [ ] **Step 7: Commit**

```bash
cd ~/projects/claude-usage-systray
git add engine/pe_poller.py engine/server.py
git commit -m "feat(pe-supervisor): wire PE poll loop into engine startup"
```

---

## Chunk 4: HTTP surface — /pe/status and async controls

### Task 7: `GET /pe/status` (exact-match route, aggregation only)

**Files:**
- Modify: `engine/api.py:130-167` (`_make_handler_class` signature + `do_GET`)
- Test: `engine/tests/test_pe_api.py`

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/projects/claude-usage-systray && .venv/bin/python3 -m pytest engine/tests/test_pe_api.py -v`
Expected: FAIL — `TypeError: create_server() got an unexpected keyword argument 'pe_instances'`

- [ ] **Step 3: Write minimal implementation**

In `engine/api.py`, add the import near the top (alongside existing engine imports):

```python
from engine.pe_poller import get_current_pe_status
```

Modify `_make_handler_class` signature (L130-135) to accept `pe_instances`:

```python
def _make_handler_class(
    db: UsageDB,
    token_holder: TokenHolder,
    classification_path: Path,
    patterns_yaml_path: Path,
    pe_instances: list | None = None,
):
    pe_instances = pe_instances or []
```

Add one line to the `do_GET` dispatch chain (L164-166, right before the existing `else:` fallback):

```python
            elif path == "/pe/status":
                self._handle_pe_status()
```

Add the handler method near `_handle_status` (after L238, keeping PE handlers grouped together like the "Habits tab" section at L280):

```python
        # ── PE supervisor endpoints (US-PESUP-ENGINE-01) ────────

        def _handle_pe_status(self):
            live_status = get_current_pe_status()
            instances_out = []
            for inst in pe_instances:
                s = live_status.get(inst.name, {
                    "reachable": False, "counts": {}, "oldest_claimable_queued_s": 0,
                    "stalled": False, "recent_terminal": [],
                    "cost": {"d24h_usd": 0.0, "calls": 0, "available": False},
                    "budget": {"target_24h_usd": inst.budget_24h_usd, "crossed": False},
                    "last_poll": None,
                })
                instances_out.append({"name": inst.name, **s})
            active_alerts = [_row_to_dict(r) for r in db.get_active_pe_alerts()]
            recent_ops = [_row_to_dict(r) for r in db.get_recent_pe_ops(limit=10)]
            _json_response(self, {
                "instances": instances_out,
                "alerts": active_alerts,
                "ops": recent_ops,
            })
```

Modify `create_server` (L481-500) to accept and thread through `pe_instances`:

```python
def create_server(
    db: UsageDB,
    token_holder: TokenHolder,
    port: int = 17420,
    classification_path: Path | None = None,
    patterns_yaml_path: Path | None = None,
    pe_instances: list | None = None,
) -> HTTPServer:
    handler_class = _make_handler_class(
        db,
        token_holder,
        classification_path or _DEFAULT_CLASSIFICATION_PATH,
        patterns_yaml_path or _DEFAULT_PATTERNS_YAML_PATH,
        pe_instances=pe_instances,
    )
    server = HTTPServer(("127.0.0.1", port), handler_class)
    return server
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/projects/claude-usage-systray && .venv/bin/python3 -m pytest engine/tests/test_pe_api.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Update `engine/server.py`'s `create_server` call site to pass `pe_instances`**

In `engine/server.py`, change L158 from:
```python
    server = create_server(db, token_holder, port=args.port)
```
to:
```python
    server = create_server(db, token_holder, port=args.port, pe_instances=pe_instances)
```

Note: `pe_instances` was already loaded earlier in Task 6 Step 5's inserted block — this just threads the existing local variable through, no new load call.

- [ ] **Step 6: Run the full engine test suite**

Run: `cd ~/projects/claude-usage-systray && .venv/bin/python3 -m pytest engine/tests/ -v`
Expected: all tests PASS

- [ ] **Step 7: Commit**

```bash
cd ~/projects/claude-usage-systray
git add engine/api.py engine/server.py engine/tests/test_pe_api.py
git commit -m "feat(pe-supervisor): add GET /pe/status aggregate route"
```

### Task 8: Async controls — retry + kick (path-segment parsing, 202 + pe_op_log)

**Files:**
- Modify: `engine/api.py` (`do_POST`, new `_handle_pe_control` method)
- Test: `engine/tests/test_pe_api.py` (append)

This is the one genuinely new pattern in this file: `engine/api.py`'s dispatcher has never matched a variable path segment. Use `re.match` (the module already imports `re` — confirmed L8) rather than manual `.split("/")`, since it's more legible for two distinct route shapes.

- [ ] **Step 1: Write the failing tests**

Append to `engine/tests/test_pe_api.py`:

```python
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
        # No poll has seeded recent_terminal for this instance in this test
        # process, so ANY job_id is "unknown" — this is the expected
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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/projects/claude-usage-systray && .venv/bin/python3 -m pytest engine/tests/test_pe_api.py -v`
Expected: FAIL — all 4 new tests fail (no `/pe/*` POST handling yet, currently falls through to the generic 404 for *all* paths including the known-instance ones)

- [ ] **Step 3: Write minimal implementation**

Add imports to `engine/api.py` near the top:

```python
import re
import time as _time
import threading as _threading
import uuid
```//check `threading`/`time` aren't already imported under a different alias before adding — grep first.

Add a module-level rate-limit tracker near the other module-level state in `engine/api.py` (alongside `_dashboard_cache`/`_dashboard_mtime`):

```python
_pe_kick_last_ts: dict = {}  # instance_name -> monotonic ts of last kick
_PE_KICK_RATE_LIMIT_S = 60
```

Add the route match inside `do_POST`, before the existing `else:` fallback (after L180):

```python
            elif path.startswith("/pe/"):
                self._handle_pe_control(path)
```

Add the dispatcher + handler methods (place after `_handle_pe_status` from Task 7):

```python
        _PE_RETRY_RE = re.compile(r"^/pe/([^/]+)/jobs/([^/]+)/retry$")
        _PE_KICK_RE = re.compile(r"^/pe/([^/]+)/worker/kick$")

        def _handle_pe_control(self, path: str):
            retry_match = self._PE_RETRY_RE.match(path)
            kick_match = self._PE_KICK_RE.match(path)

            if retry_match:
                instance_name, job_id = retry_match.groups()
                self._dispatch_pe_retry(instance_name, job_id)
            elif kick_match:
                instance_name = kick_match.group(1)
                self._dispatch_pe_kick(instance_name)
            else:
                _json_response(self, {"error": "Not found"}, 404)

        def _find_pe_instance(self, name: str):
            return next((i for i in pe_instances if i.name == name), None)

        def _dispatch_pe_retry(self, instance_name: str, job_id: str):
            instance = self._find_pe_instance(instance_name)
            if instance is None:
                _json_response(self, {"error": f"unknown instance '{instance_name}'"}, 404)
                return
            # Spec (design line 222): preflight 404s if the target isn't in
            # the cached recent_terminal list — this is the engine's own
            # cached view from the last poll, NOT a fresh PE call (that
            # would reintroduce the blocking-request problem controls exist
            # to avoid). A job real-but-not-yet-polled is a narrow race,
            # acceptable per the design's async-preflight contract.
            cached_status = get_current_pe_status().get(instance_name, {})
            recent_terminal = cached_status.get("recent_terminal", [])
            known_job_ids = {j["job_id"] for j in recent_terminal}
            if job_id not in known_job_ids:
                _json_response(self, {"error": f"job '{job_id}' not in recent_terminal"}, 404)
                return
            op_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()
            db.insert_pe_op_log(op_id=op_id, instance=instance_name, kind="retry",
                                 target=job_id, state="pending", detail=None, ts=now)
            thread = _threading.Thread(
                target=_run_pe_retry_op, args=(instance, job_id, op_id, db), daemon=True
            )
            thread.start()
            _json_response(self, {"accepted": True, "op_id": op_id}, 202)

        def _dispatch_pe_kick(self, instance_name: str):
            instance = self._find_pe_instance(instance_name)
            if instance is None:
                _json_response(self, {"error": f"unknown instance '{instance_name}'"}, 404)
                return
            last = _pe_kick_last_ts.get(instance_name, 0.0)
            if _time.monotonic() - last < _PE_KICK_RATE_LIMIT_S:
                _json_response(self, {"error": "rate_limited"}, 429)
                return
            _pe_kick_last_ts[instance_name] = _time.monotonic()
            op_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()
            running_at_kick = get_current_pe_status().get(instance_name, {}).get("counts", {}).get("running", 0)
            db.insert_pe_op_log(op_id=op_id, instance=instance_name, kind="kick",
                                 target=None, state="pending",
                                 detail=json.dumps({"running_at_kick": running_at_kick}), ts=now)
            thread = _threading.Thread(
                target=_run_pe_kick_op, args=(instance, op_id, db), daemon=True
            )
            thread.start()
            _json_response(self, {"accepted": True, "op_id": op_id}, 202)
```

Add the background-thread worker functions at module level in `engine/api.py` (below `_make_handler_class`, near `create_server`):

```python
def _run_pe_retry_op(instance, job_id: str, op_id: str, db) -> None:
    from engine.providers import keychain_get
    token = keychain_get(instance.token_ref)
    try:
        req = urllib.request.Request(
            f"{instance.base_url}/api/jobs/{job_id}/retry",
            data=b"{}", method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            db.update_pe_op_log(op_id, state="ok", detail=None)
    except urllib.error.HTTPError as e:
        db.update_pe_op_log(op_id, state="failed", detail=f"http_{e.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        db.update_pe_op_log(op_id, state="failed", detail=str(e))


def _run_pe_kick_op(instance, op_id: str, db) -> None:
    import subprocess
    try:
        if instance.kick_method == "launchctl":
            uid = os.getuid()
            result = subprocess.run(
                ["launchctl", "kickstart", "-k", f"gui/{uid}/com.poster-worker"],
                capture_output=True, timeout=15,
            )
        else:  # ssh
            result = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=5", instance.ssh_host,
                 "docker", "restart", "poster-worker"],
                capture_output=True, timeout=30,
            )
        if result.returncode == 0:
            db.update_pe_op_log(op_id, state="ok", detail=None)
        else:
            db.update_pe_op_log(op_id, state="failed", detail=result.stderr.decode()[:500])
    except subprocess.TimeoutExpired:
        db.update_pe_op_log(op_id, state="failed", detail="timeout")
    except Exception as e:  # noqa: BLE001 — surfacing any control failure into pe_op_log, not raising into a daemon thread
        db.update_pe_op_log(op_id, state="failed", detail=str(e))
```

Note: `os` must already be imported in `api.py` (used elsewhere, e.g. `_serve_dashboard`'s `os.path.getmtime`) — verify with a grep before adding a duplicate import.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/projects/claude-usage-systray && .venv/bin/python3 -m pytest engine/tests/test_pe_api.py -v`
Expected: PASS (6 tests total in this file)

- [ ] **Step 5: Failed-op alert test**

Append one more test to `engine/tests/test_pe_api.py` proving a failed async op surfaces in `/pe/status` (per spec: "a `failed` op mints an alert so the outcome reaches the UI"):

```python
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
```

This test doesn't yet assert the alert-minting side (`op_failed:<instance>:<op_id>`) — that's wired in Task 9 below, where `pe_op_log` failures get promoted into `pe_alert_state`. Leave this test as-is for now; Task 9 adds a stricter version.

Run: `cd ~/projects/claude-usage-systray && .venv/bin/python3 -m pytest engine/tests/test_pe_api.py -v`
Expected: PASS (op reaches `failed` state — connection refused / unreachable host)

- [ ] **Step 6: Commit**

```bash
cd ~/projects/claude-usage-systray
git add engine/api.py engine/tests/test_pe_api.py
git commit -m "feat(pe-supervisor): add async retry/kick control routes with pe_op_log tracking"
```

### Task 9: Failed-op alert minting (close the observability loop)

**Files:**
- Modify: `engine/api.py` (`_run_pe_retry_op`, `_run_pe_kick_op` — mint alert on failure)
- Test: `engine/tests/test_pe_api.py` (tighten the Task 8 Step 5 test)

- [ ] **Step 1: Update the Task 8 Step 5 test to assert the alert too**

Replace the test body's final assertions with:

```python
        self.assertIsNotNone(found)
        self.assertEqual(found["state"], "failed")
        active_alert_ids = [a["alert_id"] for a in self.db.get_active_pe_alerts()]
        self.assertIn(f"op_failed:dev:{op_id}", active_alert_ids)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/projects/claude-usage-systray && .venv/bin/python3 -m pytest engine/tests/test_pe_api.py -v`
Expected: FAIL — no alert minted yet

- [ ] **Step 3: Write minimal implementation**

In `engine/api.py`, update `_run_pe_retry_op` and `_run_pe_kick_op` (from Task 8) to mint an alert on the `failed` path. Add this helper above both functions:

```python
def _mint_op_failed_alert(instance_name: str, op_id: str, db) -> None:
    now = datetime.now(timezone.utc).isoformat()
    db.upsert_pe_alert_state(
        alert_id=f"op_failed:{instance_name}:{op_id}",
        first_seen=now, last_seen=now, active=True,
    )
```

Then in `_run_pe_retry_op`, change both failure branches:

```python
    except urllib.error.HTTPError as e:
        db.update_pe_op_log(op_id, state="failed", detail=f"http_{e.code}")
        _mint_op_failed_alert(instance.name, op_id, db)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        db.update_pe_op_log(op_id, state="failed", detail=str(e))
        _mint_op_failed_alert(instance.name, op_id, db)
```

And in `_run_pe_kick_op`:

```python
        if result.returncode == 0:
            db.update_pe_op_log(op_id, state="ok", detail=None)
        else:
            db.update_pe_op_log(op_id, state="failed", detail=result.stderr.decode()[:500])
            _mint_op_failed_alert(instance.name, op_id, db)
    except subprocess.TimeoutExpired:
        db.update_pe_op_log(op_id, state="failed", detail="timeout")
        _mint_op_failed_alert(instance.name, op_id, db)
    except Exception as e:  # noqa: BLE001
        db.update_pe_op_log(op_id, state="failed", detail=str(e))
        _mint_op_failed_alert(instance.name, op_id, db)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/projects/claude-usage-systray && .venv/bin/python3 -m pytest engine/tests/test_pe_api.py -v`
Expected: PASS (all tests in file)

- [ ] **Step 5: Run the full engine test suite**

Run: `cd ~/projects/claude-usage-systray && .venv/bin/python3 -m pytest engine/tests/ -v`
Expected: all tests PASS, zero regressions

- [ ] **Step 6: Commit**

```bash
cd ~/projects/claude-usage-systray
git add engine/api.py engine/tests/test_pe_api.py
git commit -m "feat(pe-supervisor): mint op_failed alert so async control failures reach /pe/status"
```

---

## Chunk 5: Stalled + dead-job alert minting on the poll path

### Task 10: Mint `stalled` and `dead` alerts from `pe_poll_once`, apply budget hysteresis with persisted state

**Files:**
- Modify: `engine/pe_poller.py` (`pe_poll_once`)
- Test: `engine/tests/test_pe_poller.py` (append)

The poll path currently computes `stalled` and leaves `budget.crossed` hardcoded `False` (Task 5, Step 3). This task closes that gap: mint/clear alerts in `pe_alert_state` as the poller runs, and make budget crossing stateful (hysteresis needs to remember whether it was already crossed).

- [ ] **Step 1: Write the failing test**

Append to `engine/tests/test_pe_poller.py`:

```python
class TestPollOnceMintsAlerts(unittest.TestCase):
    def test_stalled_poll_mints_stable_alert_id(self):
        import time
        server = HTTPServer.__new__  # placeholder to keep import block tidy; real fixture below

    # Using pytest-style fixtures inline since this class mixes with unittest above;
    # simplest is a standalone pytest function:


def test_stalled_condition_mints_alert(httpserver: HTTPServer):
    httpserver.expect_request("/api/jobs/summary").respond_with_json({
        "counts": {"queued": 1, "running": 0, "complete_24h": 0, "dead": 0, "failed": 0},
        "oldest_claimable_queued_s": 500, "recent_terminal": [],
    })
    httpserver.expect_request("/api/admin/router-metrics").respond_with_json({
        "available": True, "cost_24h_usd": 0.0, "calls": 0,
    })
    instance = PEInstance(
        name="dev", base_url=httpserver.url_for("").rstrip("/"),
        token_ref="x", kick_method="launchctl", budget_24h_usd=1.0,
    )
    db = UsageDB(":memory:")
    try:
        pe_poll_once(instance, db, token="tok", now_iso="2026-07-22T00:00:00Z")
        active = [a["alert_id"] for a in db.get_active_pe_alerts()]
        assert any(a.startswith("stalled:dev:") for a in active)
    finally:
        db.close()


def test_budget_crossed_mints_alert_and_rearms(httpserver: HTTPServer):
    httpserver.expect_request("/api/jobs/summary").respond_with_json({
        "counts": {"queued": 0, "running": 1, "complete_24h": 0, "dead": 0, "failed": 0},
        "oldest_claimable_queued_s": 0, "recent_terminal": [],
    })
    httpserver.expect_request("/api/admin/router-metrics").respond_with_json({
        "available": True, "cost_24h_usd": 0.6, "calls": 5,
    })
    instance = PEInstance(
        name="dev", base_url=httpserver.url_for("").rstrip("/"),
        token_ref="x", kick_method="launchctl", budget_24h_usd=0.5,
    )
    db = UsageDB(":memory:")
    try:
        pe_poll_once(instance, db, token="tok", now_iso="2026-07-22T00:00:00Z")
        active = [a["alert_id"] for a in db.get_active_pe_alerts()]
        assert any(a.startswith("budget:dev:") for a in active)
    finally:
        db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/projects/claude-usage-systray && .venv/bin/python3 -m pytest engine/tests/test_pe_poller.py -v`
Expected: FAIL — no alerts minted, `active` list is empty in both new tests

- [ ] **Step 3: Write minimal implementation**

In `engine/pe_poller.py`, modify `pe_poll_once` (from Task 5, as already extended by Task 5 Step 3's DB-fallback logic — do NOT drop the `cost_24h_display`/`calls_display`/`available_display` fallback when integrating this). Insert the alert-sync calls and make budget-crossing use the already-computed display values (not a re-derivation from `metrics` directly, which would re-break the `fetch_router=False` fallback). Replace the tail of the function (from `counts = (summary or {}).get(...)` onward) with:

```python
    counts = (summary or {}).get("counts", {})
    oldest_claimable = (summary or {}).get("oldest_claimable_queued_s", 0)
    running = counts.get("running", 0)
    stalled = compute_stalled(oldest_claimable, running) if summary is not None else False

    _sync_stall_alert(db, instance.name, stalled, now)

    # Use cost_24h_display (already resolved above — either this poll's fresh
    # metrics, or the DB fallback when fetch_router=False) so budget crossing
    # keeps working on iterations that skip the router call.
    if available_display:
        currently_crossed = any(
            a["alert_id"] == f"budget:{instance.name}:active" for a in db.get_active_pe_alerts()
        )
        crossed = compute_budget_crossed(cost_24h_display, instance.budget_24h_usd, currently_crossed)
        _sync_budget_alert(db, instance.name, crossed, now)
    else:
        crossed = False

    status = {
        "reachable": reachable,
        "counts": counts,
        "oldest_claimable_queued_s": oldest_claimable,
        "stalled": stalled,
        "recent_terminal": (summary or {}).get("recent_terminal", []),
        "cost": {
            "d24h_usd": cost_24h_display,
            "calls": calls_display,
            "available": available_display,
        },
        "budget": {
            "target_24h_usd": instance.budget_24h_usd,
            "crossed": crossed,
        },
        "last_poll": now,
    }
    _update_pe_status(instance.name, status)


def _sync_stall_alert(db, instance_name: str, stalled: bool, now: str) -> None:
    """Stall alert id must stay stable across polls while the condition persists.

    Uses a single well-known 'active' suffix rather than a first-seen timestamp
    in the id itself, so repeated polls upsert the same row instead of minting
    a new alert_id every 30s (the spec's `kind:instance:first-seen-ts` shape
    still holds — first_seen is a column, not encoded into the id here, since
    that lets the DB be the single source of truth for "when did this start").
    """
    alert_id = f"stalled:{instance_name}:active"
    existing = next((a for a in db.get_active_pe_alerts() if a["alert_id"] == alert_id), None)
    if stalled:
        first_seen = existing["first_seen"] if existing else now
        db.upsert_pe_alert_state(alert_id, first_seen=first_seen, last_seen=now, active=True)
    elif existing:
        db.upsert_pe_alert_state(alert_id, first_seen=existing["first_seen"], last_seen=now, active=False)


def _sync_budget_alert(db, instance_name: str, crossed: bool, now: str) -> None:
    alert_id = f"budget:{instance_name}:active"
    existing = next((a for a in db.get_active_pe_alerts() if a["alert_id"] == alert_id), None)
    if crossed:
        first_seen = existing["first_seen"] if existing else now
        db.upsert_pe_alert_state(alert_id, first_seen=first_seen, last_seen=now, active=True)
    elif existing:
        db.upsert_pe_alert_state(alert_id, first_seen=existing["first_seen"], last_seen=now, active=False)
```

**Note on the alert-id deviation from the spec's literal `kind:instance:first-seen-ts` shape:** the spec's stated purpose (design line 212-218) is stable ids so Swift can dedupe notifications by id. A `kind:instance:active` sentinel achieves the same stability more simply (one row to upsert, no timestamp-matching needed to find "the same" alert across polls) and `first_seen`/`last_seen` are still tracked as columns for the popover/history use case. If code review disagrees, the fix is confined to `_sync_stall_alert`/`_sync_budget_alert` — flag this as a design deviation worth a second look during review, not something to silently diverge on.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/projects/claude-usage-systray && .venv/bin/python3 -m pytest engine/tests/test_pe_poller.py -v`
Expected: PASS (all tests)

- [ ] **Step 5: Run the full engine test suite**

Run: `cd ~/projects/claude-usage-systray && .venv/bin/python3 -m pytest engine/tests/ -v`
Expected: all tests PASS

- [ ] **Step 6: Commit**

```bash
cd ~/projects/claude-usage-systray
git add engine/pe_poller.py engine/tests/test_pe_poller.py
git commit -m "feat(pe-supervisor): mint stall/budget alerts from the poll loop with stable ids"
```

---

## Chunk 6: Whole-branch review + wrap-up

### Task 11: Full suite + whole-branch review

- [ ] **Step 1: Run the complete engine suite one more time**

Run: `cd ~/projects/claude-usage-systray && .venv/bin/python3 -m pytest engine/tests/ -v 2>&1 | tail -30`
Expected: all PASS, note the total count

- [ ] **Step 2: Request code review**

Use `/superpowers:requesting-code-review` (or the project's `sh-4-reviewer-panel` if preferred) against the full diff on this branch before merging — this plan does not include a merge step; that is a separate decision per [[feedback_reconcile_stale_branch_on_shared_seam]]-style caution around shared seams (in this case, `engine/api.py` and `engine/server.py` are both touched across 4 tasks — review the whole branch, not each commit in isolation).

- [ ] **Step 3: Do NOT proceed to the Swift plan until this review passes**

The Swift plan (`2026-07-22-pesup-engine-01-swift.md`, to be written) depends on `/pe/status`'s response shape being final. Any review-driven contract changes here must land before Swift work starts.

---

## Known deviations from the spec worth flagging in review

1. **Alert-id shape** (Task 10): uses `kind:instance:active` instead of the spec's literal `kind:instance:first-seen-ts` for stall/budget alerts (dead-job and op_failed alerts DO use the literal spec shape with a job_id/op_id). Rationale documented inline in Task 10 Step 3.
2. **Dead-job alerts** (`dead:instance:job_id`, spec line 213) are not yet minted anywhere in this plan — the spec's example `/pe/status` payload shows a `recent_terminal` entry but the alert-minting rule for individual dead jobs (as opposed to the aggregate `stalled` condition) isn't specified beyond the id shape. Flag this as an open question for review: should every `dead`-status job in `recent_terminal` mint its own alert, or is `stalled` the only proactive alert and dead jobs are informational-only until a human clicks Retry? This plan implements only `stalled`, `budget`, and `op_failed` — dead-job alerting is deferred pending that clarification.
3. **`instance.token_ref` resolution**: Task 6's `pe_poll_loop` and Task 8's control handlers both call `keychain_get(instance.token_ref)` directly. If the Keychain entry doesn't exist yet (operator hasn't provisioned it), `keychain_get` returns `None` per its documented contract — the poll loop logs a warning and skips that instance (Task 6, Step 3); control routes would pass `token=None` into the `Authorization: Bearer None` header, which PE will correctly 401. This is acceptable degradation, not a crash, but the operator must provision `security add-generic-password -s <token_ref> -w <bearer-token>` for each configured instance before the poller does anything useful — call this out explicitly in the resume checklist for the next session.
