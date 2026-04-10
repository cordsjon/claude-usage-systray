"""SQLite database layer for token usage snapshots.

WAL journal mode, 355-day retention, thread-safe.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

RETENTION_DAYS = 355

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL,
    five_hour_util  REAL    NOT NULL,
    seven_day_util  REAL    NOT NULL,
    sonnet_util     REAL,
    five_hour_resets_at TEXT,
    seven_day_resets_at TEXT NOT NULL,
    cycle_id        TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_timestamp ON usage_snapshots(timestamp);
CREATE INDEX IF NOT EXISTS idx_snapshots_cycle_id  ON usage_snapshots(cycle_id);
"""


class UsageDB:
    """Thin wrapper around a SQLite database for usage snapshots."""

    def __init__(self, db_path: str = "usage.db"):
        self._conn = sqlite3.connect(
            db_path,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    # ── writes ──────────────────────────────────────────────

    def insert_snapshot(
        self,
        *,
        timestamp: str,
        five_hour_util: float,
        seven_day_util: float,
        sonnet_util: float | None,
        five_hour_resets_at: str | None,
        seven_day_resets_at: str,
    ) -> int:
        """Insert a usage snapshot. Returns the new row id."""
        cycle_id = seven_day_resets_at[:10]
        cur = self._conn.execute(
            """INSERT INTO usage_snapshots
               (timestamp, five_hour_util, seven_day_util, sonnet_util,
                five_hour_resets_at, seven_day_resets_at, cycle_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                timestamp,
                five_hour_util,
                seven_day_util,
                sonnet_util,
                five_hour_resets_at,
                seven_day_resets_at,
                cycle_id,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def prune(self) -> int:
        """Delete snapshots older than RETENTION_DAYS. Returns deleted count."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
        ).isoformat()
        cur = self._conn.execute(
            "DELETE FROM usage_snapshots WHERE timestamp < ?", (cutoff,)
        )
        self._conn.commit()
        return cur.rowcount

    # ── reads ───────────────────────────────────────────────

    def get_latest_snapshot(self) -> sqlite3.Row | None:
        """Return the single most recent snapshot, or None."""
        cur = self._conn.execute(
            "SELECT * FROM usage_snapshots ORDER BY timestamp DESC LIMIT 1"
        )
        return cur.fetchone()

    def get_recent_snapshots(self, limit: int = 100) -> list[sqlite3.Row]:
        """Return the most recent snapshots, newest first."""
        cur = self._conn.execute(
            "SELECT * FROM usage_snapshots ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
        return cur.fetchall()

    def get_snapshots_by_cycle(self, cycle_id: str) -> list[sqlite3.Row]:
        """Return all snapshots for a given cycle_id."""
        cur = self._conn.execute(
            "SELECT * FROM usage_snapshots WHERE cycle_id = ? ORDER BY timestamp",
            (cycle_id,),
        )
        return cur.fetchall()

    def get_snapshots_since(self, since: str) -> list[sqlite3.Row]:
        """Return snapshots with timestamp >= since, oldest first."""
        cur = self._conn.execute(
            "SELECT * FROM usage_snapshots WHERE timestamp >= ? ORDER BY timestamp",
            (since,),
        )
        return cur.fetchall()

    def get_cycle_peaks(self) -> list[sqlite3.Row]:
        """Return peak utilisation per cycle with stoppage flag.

        stoppage = 1 when peak five_hour_util >= 0.95 in the cycle.
        """
        cur = self._conn.execute(
            """SELECT
                 cycle_id,
                 MAX(five_hour_util)  AS peak_five_hour,
                 MAX(seven_day_util)  AS peak_seven_day,
                 CASE WHEN MAX(five_hour_util) >= 95.0 THEN 1 ELSE 0 END AS stoppage
               FROM usage_snapshots
               GROUP BY cycle_id
               ORDER BY cycle_id"""
        )
        return cur.fetchall()

    def get_weekday_averages(self, since: str) -> list[sqlite3.Row]:
        """Return average utilisation grouped by weekday (0=Mon .. 6=Sun)."""
        cur = self._conn.execute(
            """SELECT
                 CAST(strftime('%%w', timestamp) AS INTEGER) AS weekday,
                 AVG(five_hour_util)  AS avg_five_hour,
                 AVG(seven_day_util)  AS avg_seven_day,
                 COUNT(*)             AS sample_count
               FROM usage_snapshots
               WHERE timestamp >= ?
               GROUP BY weekday
               ORDER BY weekday""",
            (since,),
        )
        return cur.fetchall()

    # ── maintenance ─────────────────────────────────────────

    def checkpoint(self):
        """Force a WAL checkpoint."""
        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def close(self):
        """Close the database connection."""
        self._conn.close()
