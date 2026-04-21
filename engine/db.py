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

CREATE TABLE IF NOT EXISTS prompt_usage (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    session_id TEXT NOT NULL,
    project_dir TEXT NOT NULL,
    pattern_id TEXT NOT NULL,
    pattern_version INTEGER NOT NULL DEFAULT 1,
    is_structured INTEGER NOT NULL,
    matched_text TEXT,
    message_ordinal INTEGER NOT NULL,
    UNIQUE (session_id, message_ordinal)
);

CREATE INDEX IF NOT EXISTS idx_prompt_usage_date ON prompt_usage(date);
CREATE INDEX IF NOT EXISTS idx_prompt_usage_pattern ON prompt_usage(pattern_id);

CREATE TABLE IF NOT EXISTS prompt_unmatched (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    session_id TEXT NOT NULL,
    text_excerpt TEXT NOT NULL,
    message_ordinal INTEGER NOT NULL,
    UNIQUE (session_id, message_ordinal)
);

CREATE INDEX IF NOT EXISTS idx_prompt_unmatched_date ON prompt_unmatched(date);

CREATE TABLE IF NOT EXISTS prompt_pattern_eval (
    id INTEGER PRIMARY KEY,
    pattern_id TEXT NOT NULL,
    pattern_version INTEGER NOT NULL,
    eval_date TEXT NOT NULL,
    precision_score REAL,
    sample_size INTEGER NOT NULL,
    verdict TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prompt_pattern_eval_labels (
    id INTEGER PRIMARY KEY,
    pattern_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    is_true_positive INTEGER NOT NULL,
    labeler TEXT NOT NULL,
    labeled_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_watermark (
    file_path TEXT PRIMARY KEY,
    byte_offset INTEGER NOT NULL,
    sha256_head TEXT NOT NULL,
    last_ingested_at TEXT NOT NULL
);
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

    # ── prompt-frequency writes (US-TB-01) ──────────────────

    def insert_prompt_usage(
        self,
        *,
        date,
        session_id,
        project_dir,
        pattern_id,
        pattern_version,
        is_structured,
        matched_text,
        message_ordinal,
    ):
        """Idempotent insert keyed on (session_id, message_ordinal)."""
        self._conn.execute(
            """INSERT OR IGNORE INTO prompt_usage
               (date, session_id, project_dir, pattern_id, pattern_version,
                is_structured, matched_text, message_ordinal)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                date,
                session_id,
                project_dir,
                pattern_id,
                pattern_version,
                int(bool(is_structured)),
                matched_text,
                message_ordinal,
            ),
        )
        self._conn.commit()

    def insert_prompt_unmatched(
        self, *, date, session_id, text_excerpt, message_ordinal
    ):
        """Idempotent insert of redacted excerpt for unmatched user messages."""
        self._conn.execute(
            """INSERT OR IGNORE INTO prompt_unmatched
               (date, session_id, text_excerpt, message_ordinal)
               VALUES (?, ?, ?, ?)""",
            (date, session_id, text_excerpt, message_ordinal),
        )
        self._conn.commit()

    def upsert_watermark(
        self, file_path, byte_offset, sha256_head, last_ingested_at
    ):
        """Upsert ingest watermark for a JSONL file."""
        self._conn.execute(
            """INSERT INTO ingest_watermark
               (file_path, byte_offset, sha256_head, last_ingested_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(file_path) DO UPDATE SET
                 byte_offset=excluded.byte_offset,
                 sha256_head=excluded.sha256_head,
                 last_ingested_at=excluded.last_ingested_at""",
            (file_path, byte_offset, sha256_head, last_ingested_at),
        )
        self._conn.commit()

    def get_watermark(self, file_path):
        """Return watermark dict for file_path, or None if not seen."""
        row = self._conn.execute(
            """SELECT byte_offset, sha256_head, last_ingested_at
               FROM ingest_watermark WHERE file_path=?""",
            (file_path,),
        ).fetchone()
        if row is None:
            return None
        return {
            "byte_offset": row[0],
            "sha256_head": row[1],
            "last_ingested_at": row[2],
        }

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

    def get_ranked_prompts(self, today):
        """Return ranked prompt patterns with 7d/30d/all counts.

        Each dict: {pattern_id, is_structured, count_7d, count_30d, count_all}.
        Ordering: count_7d DESC, then count_all DESC.
        """
        from datetime import date, timedelta

        today_d = date.fromisoformat(today)
        d7 = (today_d - timedelta(days=7)).isoformat()
        d30 = (today_d - timedelta(days=30)).isoformat()
        rows = self._conn.execute(
            """
            SELECT pattern_id,
                   MAX(is_structured) AS is_structured,
                   SUM(CASE WHEN date >= ? THEN 1 ELSE 0 END) AS c7,
                   SUM(CASE WHEN date >= ? THEN 1 ELSE 0 END) AS c30,
                   COUNT(*) AS call_total
            FROM prompt_usage
            GROUP BY pattern_id
            ORDER BY c7 DESC, call_total DESC
            """,
            (d7, d30),
        ).fetchall()
        return [
            dict(
                pattern_id=r[0],
                is_structured=bool(r[1]),
                count_7d=r[2],
                count_30d=r[3],
                count_all=r[4],
            )
            for r in rows
        ]

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

    def downgrade_prompt_tables(self):
        """Drop all prompt-frequency tables (AC-16b rollback)."""
        for t in (
            "prompt_pattern_eval_labels",
            "prompt_pattern_eval",
            "prompt_unmatched",
            "prompt_usage",
            "ingest_watermark",
        ):
            self._conn.execute(f"DROP TABLE IF EXISTS {t}")
        self._conn.commit()

    def checkpoint(self):
        """Force a WAL checkpoint."""
        self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def close(self):
        """Close the database connection."""
        self._conn.close()
