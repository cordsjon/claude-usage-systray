"""Ingest Claude Code conversation transcripts into prompt_usage tables.

Walks ``~/.claude/projects/<encoded>/conversations/*.jsonl``, classifies each
user message against the YAML pattern list, and persists matches + redacted
unmatched excerpts. Resumable via sha256_head watermark; idempotent via
UNIQUE(session_id, message_ordinal).

Entry point: ``python3 -m engine.ingest_prompts``.
"""

import hashlib
import json
from datetime import datetime
from pathlib import Path


def decode_project_dir(encoded, fs_root=None):
    """Decode Claude Code's ``~/.claude/projects/<encoded>/`` name to an absolute path.

    Claude Code's encoding is lossy: it replaces every ``/`` in a real path with ``-``,
    so hyphens in usernames (``jcords-macmini``) or project names (``30_SVG-PAINT``)
    become indistinguishable from path separators. We disambiguate by consulting the
    filesystem: at each level, pick the longest hyphen-joined prefix of remaining
    segments that exists as a directory.

    ``fs_root`` lets tests inject a temp directory for deterministic behaviour.
    """
    parts = encoded.lstrip("-").split("-")
    if not parts:
        return encoded
    root_path = Path(fs_root) if fs_root else Path("/")
    cur_rel = Path(parts[0])
    if not (root_path / cur_rel).is_dir():
        # Fallback: unknown prefix, collapse all hyphens to '/'
        return "/" + "-".join(parts).replace("-", "/")
    i = 1
    while i < len(parts):
        best_n = 0
        for n in range(1, len(parts) - i + 1):
            candidate = cur_rel / "-".join(parts[i:i + n])
            if (root_path / candidate).is_dir():
                best_n = n
        if best_n == 0:
            # No matching directory — append remaining as-is and stop walking
            cur_rel = cur_rel / "-".join(parts[i:])
            break
        cur_rel = cur_rel / "-".join(parts[i:i + best_n])
        i += best_n
    return "/" + str(cur_rel)


def _extract_user_text(content):
    """Return the text payload of a user message's ``content`` field or None.

    Strings are returned verbatim (if non-empty). Lists are scanned for text
    blocks; any ``tool_result`` block causes the whole message to be skipped.
    Unknown shapes return None (logged as unmatched later, never crashed).
    """
    if isinstance(content, str):
        return content if content.strip() else None
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "tool_result":
                    return None  # exclude entire message
                if block.get("type") == "text" and block.get("text"):
                    texts.append(block["text"])
        combined = "\n".join(texts).strip()
        return combined or None
    return None


def _count_lines_up_to(path, offset):
    with open(path, "rb") as fh:
        chunk = fh.read(offset)
    return chunk.count(b"\n")


def iter_user_messages(jsonl_path, start_offset=0):
    """Yield user-message dicts from a JSONL transcript.

    Each dict: ``{text, session_id, timestamp, message_ordinal, byte_offset_after}``.
    Only yields ``role=user`` entries whose content is a non-empty string OR a
    list with at least one text block. Skips tool_result-bearing user entries.
    """
    jsonl_path = Path(jsonl_path)
    with open(jsonl_path, "rb") as fh:
        fh.seek(start_offset)
        ordinal = (
            _count_lines_up_to(jsonl_path, start_offset) if start_offset > 0 else 0
        )
        while True:
            line = fh.readline()
            if not line:
                break
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                ordinal += 1
                continue
            ordinal += 1
            if obj.get("role") != "user":
                continue
            content = obj.get("content")
            text = _extract_user_text(content)
            if not text:
                continue
            yield {
                "text": text,
                "session_id": obj.get("sessionId") or jsonl_path.stem,
                "timestamp": obj.get("timestamp", ""),
                "message_ordinal": ordinal - 1,
                "byte_offset_after": fh.tell(),
            }


def compute_sha256_head(path, size=4096):
    """SHA-256 of the first ``size`` bytes — rotation-detection fingerprint."""
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read(size)).hexdigest()


def resolve_start_offset(db, path):
    """Return ``(start_offset, sha256_head)`` honouring the watermark.

    If the stored sha256_head matches the current file head the stored offset
    is resumed; otherwise the file is treated as rotated and ingest restarts
    from byte 0 (AC-05).
    """
    head = compute_sha256_head(path)
    wm = db.get_watermark(path)
    if wm and wm["sha256_head"] == head:
        return wm["byte_offset"], head
    return 0, head


def ingest_all(db, projects_root, patterns_yaml):
    """Walk projects_root, classify, persist, return a coverage report dict."""
    from engine.patterns import classify_message, load_patterns
    from engine.redact import redact_for_unmatched

    patterns = load_patterns(patterns_yaml)
    total, matched, unmatched, structured = 0, 0, 0, 0
    for proj_dir in Path(projects_root).glob("-*"):
        conv_dir = proj_dir / "conversations"
        if not conv_dir.is_dir():
            continue
        project_dir = decode_project_dir(proj_dir.name)
        for jsonl in sorted(conv_dir.glob("*.jsonl")):
            start_off, head = resolve_start_offset(db, str(jsonl))
            last_off = start_off
            for msg in iter_user_messages(jsonl, start_offset=start_off):
                total += 1
                last_off = msg["byte_offset_after"]
                cls = classify_message(msg["text"], patterns)
                date = (msg["timestamp"] or datetime.utcnow().isoformat())[:10]
                if cls["pattern_id"]:
                    matched += 1
                    if cls["is_structured"]:
                        structured += 1
                    db.insert_prompt_usage(
                        date=date,
                        session_id=msg["session_id"],
                        project_dir=project_dir,
                        pattern_id=cls["pattern_id"],
                        pattern_version=cls["version"],
                        is_structured=cls["is_structured"],
                        matched_text=msg["text"][:500],
                        message_ordinal=msg["message_ordinal"],
                    )
                else:
                    unmatched += 1
                    db.insert_prompt_unmatched(
                        date=date,
                        session_id=msg["session_id"],
                        text_excerpt=redact_for_unmatched(msg["text"]),
                        message_ordinal=msg["message_ordinal"],
                    )
            db.upsert_watermark(
                str(jsonl), last_off, head, datetime.utcnow().isoformat()
            )
    mp = (matched / total * 100.0) if total else 0.0
    return {
        "total_user_messages": total,
        "matched": matched,
        "unmatched": unmatched,
        "structured": structured,
        "matched_percent": round(mp, 1),
    }


if __name__ == "__main__":
    import os
    import sys

    from engine.db import UsageDB

    db_path = os.environ.get(
        "TOKEN_BUDGET_DB",
        str(Path.home() / ".local/share/token-budget/usage.db"),
    )
    projects_root = Path.home() / ".claude" / "projects"
    patterns_yaml = (
        Path.home()
        / ".claude/projects"
        / "-Users-jcords-macmini-projects"
        / "memory"
        / "prompt-patterns.yaml"
    )
    report = ingest_all(UsageDB(db_path), projects_root, patterns_yaml)
    print(
        f"ingest: {report['total_user_messages']} msgs, "
        f"{report['matched_percent']}% matched, "
        f"{report['unmatched']} unmatched"
    )
    sys.exit(0)
