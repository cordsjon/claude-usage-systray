"""Hand-labeling TUI for AC-16a regex precision eval (US-TB-01).

Iterates ``prompt_usage`` rows for each ``pattern_id`` (stratified: up to 20
matches per pattern, plus up to 50 ``prompt_unmatched`` rows as a negative
control). Prompts ``[y]`` (true-positive) / ``[n]`` (false-positive) /
``[s]`` (skip) / ``[q]`` (quit), writes labels to
``prompt_pattern_eval_labels``, then computes per-pattern precision and
writes a summary row to ``prompt_pattern_eval``.

Verdict rule:
    - precision >= 0.95 and sample_size >= 10  -> "pass"
    - sample_size < 10                         -> "insufficient-data"
    - otherwise                                -> "fail"
"""

import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path


# ── pure helpers (tested in engine/tests/test_eval.py) ──────


def compute_precision(labels):
    """Return ``tp / total`` for a list of booleans, or ``None`` if empty.

    ``labels`` is a list where ``True`` means true-positive and ``False``
    means false-positive. Skipped labels must be excluded by the caller.
    """
    if not labels:
        return None
    tp = sum(1 for x in labels if x)
    return tp / len(labels)


def build_stratified_sample(db, today_iso, per_pattern_cap=20, negative_cap=50):
    """Return a stratified sample of match candidates for labeling.

    Output shape::

        {
          "<pattern_id>": [ {"message_id": int, "matched_text": str,
                             "pattern_version": int}, ... ],
          ...
          "_negatives": [ {"message_id": int, "text_excerpt": str}, ... ],
        }

    - For each ``pattern_id`` in ``prompt_usage`` within the last 30 days
      (relative to ``today_iso``), sample up to ``per_pattern_cap`` rows.
    - For the ``_negatives`` control set, pull up to ``negative_cap`` rows
      from ``prompt_unmatched`` (random order).
    """
    today_d = date.fromisoformat(today_iso)
    cutoff = (today_d - timedelta(days=30)).isoformat()

    sample = {}

    pattern_ids = [
        r[0]
        for r in db._conn.execute(
            """SELECT DISTINCT pattern_id
               FROM prompt_usage
               WHERE date >= ?
               ORDER BY pattern_id""",
            (cutoff,),
        ).fetchall()
    ]

    for pid in pattern_ids:
        rows = db._conn.execute(
            """SELECT id, matched_text, pattern_version
               FROM prompt_usage
               WHERE pattern_id = ? AND date >= ?
               ORDER BY RANDOM()
               LIMIT ?""",
            (pid, cutoff, per_pattern_cap),
        ).fetchall()
        sample[pid] = [
            {
                "message_id": r[0],
                "matched_text": r[1] or "",
                "pattern_version": r[2],
            }
            for r in rows
        ]

    neg_rows = db._conn.execute(
        """SELECT id, text_excerpt
           FROM prompt_unmatched
           ORDER BY RANDOM()
           LIMIT ?""",
        (negative_cap,),
    ).fetchall()
    sample["_negatives"] = [
        {"message_id": r[0], "text_excerpt": r[1] or ""} for r in neg_rows
    ]

    return sample


# ── TUI loop (not unit-tested; exercised by running the module) ──


def _prompt_label(prompt_text):
    """Block on stdin for y/n/s/q. Returns 'y', 'n', 's', or 'q'."""
    while True:
        sys.stdout.write(prompt_text)
        sys.stdout.flush()
        line = sys.stdin.readline()
        if not line:
            return "q"
        choice = line.strip().lower()
        if choice in ("y", "n", "s", "q"):
            return choice
        print("  invalid — enter y / n / s / q")


def _write_label(db, pattern_id, message_id, is_tp, labeler):
    """Insert a single label into prompt_pattern_eval_labels."""
    db._conn.execute(
        """INSERT INTO prompt_pattern_eval_labels
           (pattern_id, message_id, is_true_positive, labeler, labeled_at)
           VALUES (?, ?, ?, ?, ?)""",
        (
            pattern_id,
            message_id,
            int(bool(is_tp)),
            labeler,
            datetime.utcnow().isoformat(),
        ),
    )
    db._conn.commit()


def _write_eval_summary(
    db, pattern_id, pattern_version, eval_date, precision, sample_size
):
    """Write one row to prompt_pattern_eval with the computed verdict."""
    if sample_size < 10:
        verdict = "insufficient-data"
    elif precision is not None and precision >= 0.95:
        verdict = "pass"
    else:
        verdict = "fail"
    db._conn.execute(
        """INSERT INTO prompt_pattern_eval
           (pattern_id, pattern_version, eval_date,
            precision_score, sample_size, verdict)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            pattern_id,
            pattern_version,
            eval_date,
            precision,
            sample_size,
            verdict,
        ),
    )
    db._conn.commit()
    return verdict


def run_tui(db, today_iso):
    """Interactive loop. Labels matches, writes summary rows, prints table."""
    labeler = os.environ.get("USER", "anonymous")
    sample = build_stratified_sample(db, today_iso)

    # ── positive labeling pass ────────────────────────────
    per_pattern_labels = {}  # pid -> list[bool]
    per_pattern_version = {}  # pid -> int (first one seen)
    quit_early = False

    for pid, rows in sample.items():
        if pid == "_negatives":
            continue
        if not rows:
            continue
        print(f"\n=== pattern: {pid}  ({len(rows)} candidates) ===")
        labels = []
        for idx, row in enumerate(rows, 1):
            if quit_early:
                break
            per_pattern_version.setdefault(pid, row["pattern_version"])
            excerpt = (row["matched_text"] or "").replace("\n", " ")[:160]
            prompt = f"  [{idx}/{len(rows)}] {excerpt!r}\n  y/n/s/q > "
            choice = _prompt_label(prompt)
            if choice == "q":
                quit_early = True
                break
            if choice == "s":
                continue
            is_tp = choice == "y"
            labels.append(is_tp)
            _write_label(db, pid, row["message_id"], is_tp, labeler)
        per_pattern_labels[pid] = labels
        if quit_early:
            break

    # ── negative-control pass (optional, only if not quit) ──
    if not quit_early and sample.get("_negatives"):
        print(
            f"\n=== negative control "
            f"({len(sample['_negatives'])} unmatched excerpts) ==="
        )
        print("  Mark any that SHOULD have matched an existing pattern.")
        for idx, row in enumerate(sample["_negatives"], 1):
            excerpt = (row["text_excerpt"] or "").replace("\n", " ")[:160]
            prompt = (
                f"  [{idx}/{len(sample['_negatives'])}] {excerpt!r}\n"
                "  y=should-have-matched / n=correctly-unmatched / s / q > "
            )
            choice = _prompt_label(prompt)
            if choice == "q":
                break
            if choice == "s":
                continue
            # A 'y' here is a false-negative miss (no pattern_id assoc).
            # Recorded under the synthetic "_negatives" bucket.
            _write_label(
                db, "_negatives", row["message_id"], choice == "y", labeler
            )

    # ── summary write + table print ────────────────────────
    eval_date = today_iso
    print("\n" + "=" * 60)
    print(f"{'pattern_id':<22} {'n':>4} {'precision':>10} {'verdict':>18}")
    print("-" * 60)
    for pid, labels in per_pattern_labels.items():
        precision = compute_precision(labels)
        verdict = _write_eval_summary(
            db,
            pid,
            per_pattern_version.get(pid, 1),
            eval_date,
            precision,
            len(labels),
        )
        precision_str = "n/a" if precision is None else f"{precision:.2f}"
        print(f"{pid:<22} {len(labels):>4} {precision_str:>10} {verdict:>18}")
    print("=" * 60)


if __name__ == "__main__":
    from engine.db import UsageDB

    db_path = os.environ.get(
        "TOKEN_BUDGET_DB",
        str(Path.home() / ".local/share/token-budget/usage.db"),
    )
    run_tui(UsageDB(db_path), date.today().isoformat())
