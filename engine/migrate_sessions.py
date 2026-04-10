#!/usr/bin/env python3
"""Migrate Claude Code session JSONL data into the token budget database.

Since session files contain per-message token counts (not utilization %),
we estimate hourly utilization by aggregating tokens into hourly buckets
and scaling to a synthetic utilization curve per weekly cycle.

Usage:
    python3 -m engine.migrate_sessions [--db-path PATH] [--sessions-dir DIR]
"""

import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from engine.db import UsageDB

DEFAULT_SESSIONS = os.path.expanduser("~/.claude/projects")
DEFAULT_DB = os.path.expanduser("~/.local/share/token-budget/token_budget.db")

# Rough weekly token cap estimate for Max plan (Opus)
# This is an approximation — the actual cap is unknown
ESTIMATED_WEEKLY_CAP_TOKENS = 50_000_000  # 50M tokens/week (conservative estimate)


def scan_sessions(sessions_dir: str) -> dict[str, int]:
    """Scan all session JSONLs and aggregate output tokens per hour.

    Returns: {"2026-03-10T19": 45230, "2026-03-10T20": 89100, ...}
    """
    hourly_tokens: dict[str, int] = defaultdict(int)
    files = glob.glob(os.path.join(sessions_dir, "**", "*.jsonl"), recursive=True)

    print(f"[migrate] Scanning {len(files)} session files...", file=sys.stderr)

    for i, fpath in enumerate(files):
        if i % 100 == 0 and i > 0:
            print(f"[migrate] ... {i}/{len(files)}", file=sys.stderr)
        try:
            with open(fpath) as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if obj.get("type") != "assistant":
                        continue

                    ts = obj.get("timestamp", "")
                    msg = obj.get("message", {})
                    usage = msg.get("usage", {}) if isinstance(msg, dict) else {}

                    if not ts or not usage:
                        continue

                    # Aggregate input + output tokens
                    tokens = (
                        usage.get("input_tokens", 0)
                        + usage.get("output_tokens", 0)
                    )
                    hour_key = ts[:13]  # "2026-03-10T19"
                    hourly_tokens[hour_key] += tokens
        except (OSError, UnicodeDecodeError):
            continue

    print(f"[migrate] Found {len(hourly_tokens)} hours of data", file=sys.stderr)
    return dict(hourly_tokens)


def build_weekly_cycles(hourly_tokens: dict[str, int]) -> list[dict]:
    """Convert hourly token counts into synthetic utilization snapshots.

    Groups hours into 7-day cycles (starting from the earliest data),
    computes cumulative utilization within each cycle.
    """
    if not hourly_tokens:
        return []

    # Sort by hour
    sorted_hours = sorted(hourly_tokens.keys())
    first_hour = datetime.fromisoformat(sorted_hours[0] + ":00:00")
    last_hour = datetime.fromisoformat(sorted_hours[-1] + ":00:00")

    # Determine cycle boundaries (7-day windows)
    # Start from the first hour, roll forward in 7-day increments
    cycles: list[dict] = []
    cycle_start = first_hour

    while cycle_start <= last_hour:
        cycle_end = cycle_start + timedelta(days=7)
        cycle_reset = cycle_end.replace(tzinfo=timezone.utc).isoformat()
        cycle_id = cycle_end.strftime("%Y-%m-%d")

        # Gather all hours in this cycle
        cycle_tokens: dict[str, int] = {}
        for hour_key, tokens in hourly_tokens.items():
            hour_dt = datetime.fromisoformat(hour_key + ":00:00")
            if cycle_start <= hour_dt < cycle_end:
                cycle_tokens[hour_key] = tokens

        if cycle_tokens:
            # Compute cumulative utilization within cycle
            total_in_cycle = sum(cycle_tokens.values())
            # Scale: what fraction of estimated weekly cap was used?
            cycle_cap = max(total_in_cycle, ESTIMATED_WEEKLY_CAP_TOKENS)

            cumulative = 0
            for hour_key in sorted(cycle_tokens.keys()):
                cumulative += cycle_tokens[hour_key]
                util = round(100.0 * cumulative / cycle_cap, 1)
                timestamp = hour_key + ":30:00"  # mid-hour
                timestamp_utc = datetime.fromisoformat(timestamp).replace(
                    tzinfo=timezone.utc
                ).isoformat()

                cycles.append({
                    "timestamp": timestamp_utc,
                    "five_hour_util": min(util, 100.0),  # approximate
                    "seven_day_util": min(util, 100.0),
                    "sonnet_util": None,
                    "five_hour_resets_at": None,
                    "seven_day_resets_at": cycle_reset,
                    "cycle_id": cycle_id,
                })

        cycle_start = cycle_end

    return cycles


def migrate(db_path: str, sessions_dir: str, dry_run: bool = False):
    """Run the full migration."""
    hourly = scan_sessions(sessions_dir)
    snapshots = build_weekly_cycles(hourly)

    print(f"[migrate] Generated {len(snapshots)} synthetic snapshots "
          f"across {len(set(s['cycle_id'] for s in snapshots))} cycles",
          file=sys.stderr)

    if dry_run:
        print("[migrate] Dry run — not writing to database", file=sys.stderr)
        for s in snapshots[:5]:
            print(f"  {s['timestamp']} → {s['seven_day_util']}% (cycle {s['cycle_id']})",
                  file=sys.stderr)
        print(f"  ... ({len(snapshots) - 5} more)", file=sys.stderr)
        return

    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    db = UsageDB(db_path)

    # Check for existing data to avoid duplicates
    existing = db.get_snapshots_since("2000-01-01T00:00:00Z")
    if existing:
        earliest_existing = min(s["timestamp"] for s in existing)
        print(f"[migrate] Database already has {len(existing)} snapshots "
              f"(earliest: {earliest_existing[:19]})", file=sys.stderr)
        # Only insert snapshots older than existing data
        snapshots = [s for s in snapshots if s["timestamp"] < earliest_existing]
        print(f"[migrate] Will insert {len(snapshots)} pre-existing snapshots",
              file=sys.stderr)

    inserted = 0
    for s in snapshots:
        db.insert_snapshot(
            timestamp=s["timestamp"],
            five_hour_util=s["five_hour_util"],
            seven_day_util=s["seven_day_util"],
            sonnet_util=s["sonnet_util"],
            five_hour_resets_at=s["five_hour_resets_at"],
            seven_day_resets_at=s["seven_day_resets_at"],
        )
        inserted += 1

    db.close()
    print(f"[migrate] Inserted {inserted} snapshots into {db_path}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Migrate session data to token budget DB")
    parser.add_argument("--db-path", default=DEFAULT_DB)
    parser.add_argument("--sessions-dir", default=DEFAULT_SESSIONS)
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()
    migrate(args.db_path, args.sessions_dir, args.dry_run)


if __name__ == "__main__":
    main()
