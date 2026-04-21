"""Atomic JSON read/write for prompt classification (US-TB-01 AC-06).

Stores `{"everyday": [...], "case_by_case": [...]}` — two mutually exclusive
sections that track the user's manual classification of prompt patterns.

All writes go through `tempfile + Path.replace()` for atomicity.
"""

import json
import tempfile
from pathlib import Path


_EMPTY = {"everyday": [], "case_by_case": []}
_SECTIONS = ("everyday", "case_by_case")


def load_classification(path):
    """Load classification JSON. Returns empty structure if file missing."""
    p = Path(path)
    if not p.exists():
        return {"everyday": [], "case_by_case": []}
    data = json.loads(p.read_text())
    # Defensive: ensure both sections exist
    for s in _SECTIONS:
        data.setdefault(s, [])
    return data


def save_classification(path, data):
    """Atomic write: tempfile in same dir + Path.replace()."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", dir=str(p.parent), delete=False, suffix=".tmp"
    )
    try:
        tmp.write(json.dumps(data, indent=2))
        tmp.flush()
        tmp.close()
        Path(tmp.name).replace(p)
    except Exception:
        Path(tmp.name).unlink(missing_ok=True)
        raise


def move_pattern(path, pattern_id, section):
    """Move `pattern_id` into `section`, removing from the other section.

    Idempotent: if already in `section`, no duplicate is added.
    Raises ValueError if `section` is not one of the allowed values.
    """
    if section not in _SECTIONS:
        raise ValueError(f"bad section: {section!r}")
    d = load_classification(path)
    for s in _SECTIONS:
        if pattern_id in d[s]:
            d[s].remove(pattern_id)
    if pattern_id not in d[section]:
        d[section].append(pattern_id)
    save_classification(path, d)
    return d
