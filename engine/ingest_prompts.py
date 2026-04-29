"""Ingest Claude Code conversation transcripts into prompt_usage tables.

Walks ``~/.claude/projects/<encoded>/conversations/*.jsonl``, classifies each
user message against the YAML pattern list, and persists matches + redacted
unmatched excerpts. Resumable via sha256_head watermark; idempotent via
UNIQUE(session_id, message_ordinal).

Entry point: ``python3 -m engine.ingest_prompts``.
"""

import hashlib
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
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


def _is_user_entry(obj):
    """True if this JSONL line is a user prompt we should classify.

    Claude Code's transcript schema wraps user messages as
    ``{"type": "user", "message": {"role": "user", "content": ...}}``.
    Older samples also used flat ``{"role": "user", "content": ...}``.
    Both shapes are accepted.
    """
    if obj.get("type") == "user":
        msg = obj.get("message")
        if isinstance(msg, dict) and msg.get("role") == "user":
            return True
    if obj.get("role") == "user" and "content" in obj:
        return True
    return False


def _user_content(obj):
    """Return the ``content`` payload (string or list) of a user entry."""
    if obj.get("type") == "user":
        return (obj.get("message") or {}).get("content")
    return obj.get("content")


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
            if not _is_user_entry(obj):
                continue
            content = _user_content(obj)
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
        if not proj_dir.is_dir():
            continue
        # Claude Code writes JSONL transcripts directly into the project dir,
        # named <session_uuid>.jsonl. (Older plan assumption of a conversations/
        # subdir was wrong — verified against live filesystem on 2026-04-21.)
        jsonl_files = sorted(proj_dir.glob("*.jsonl"))
        if not jsonl_files:
            continue
        project_dir = decode_project_dir(proj_dir.name)
        for jsonl in jsonl_files:
            start_off, head = resolve_start_offset(db, str(jsonl))
            last_off = start_off
            for msg in iter_user_messages(jsonl, start_offset=start_off):
                total += 1
                last_off = msg["byte_offset_after"]
                cls = classify_message(msg["text"], patterns)
                date = (msg["timestamp"] or datetime.now(timezone.utc).isoformat())[:10]
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
                str(jsonl), last_off, head, datetime.now(timezone.utc).isoformat()
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
    from engine.db import UsageDB

    import argparse

    ap = argparse.ArgumentParser(description="Ingest Claude Code prompts into prompt_usage tables.")
    ap.add_argument(
        "--db-path",
        default=os.environ.get(
            "TOKEN_BUDGET_DB",
            str(Path.home() / ".local/share/token-budget/token_budget.db"),
        ),
        help="SQLite DB path (default matches engine/server.py token_budget.db)",
    )
    ap.add_argument(
        "--projects-root",
        default=str(Path.home() / ".claude" / "projects"),
        help="Claude Code projects root (default: ~/.claude/projects)",
    )
    ap.add_argument(
        "--patterns-yaml",
        default=str(
            Path.home()
            / ".claude/projects"
            / "-Users-jcords-macmini-projects"
            / "memory"
            / "prompt-patterns.yaml"
        ),
        help="YAML patterns path (default: ~/.claude/projects/.../memory/prompt-patterns.yaml)",
    )
    ap.add_argument("--reset", action="store_true", help="Clear ingest tables + watermarks before ingest")
    ap.add_argument("--inspect", action="store_true", help="Print one-shot diagnostic and exit 0")
    ap.add_argument("--json", action="store_true", help="With --inspect: emit JSON")
    args = ap.parse_args()

    db = UsageDB(args.db_path)
    projects_root = Path(args.projects_root).expanduser()
    patterns_yaml = Path(args.patterns_yaml).expanduser()

    if args.inspect:
        info = {
            "python": sys.version.split()[0],
            "db_path": str(Path(args.db_path).expanduser()),
            "projects_root": str(projects_root),
            "patterns_yaml": str(patterns_yaml),
            "counts": db.count_rows(),
            "unmatched_sample": db.sample_unmatched(3),
            "projects_dir_exists": projects_root.exists(),
            "patterns_yaml_exists": patterns_yaml.exists(),
        }
        try:
            with urllib.request.urlopen("http://127.0.0.1:17420/api/status", timeout=2) as resp:
                info["engine_api_status_code"] = resp.status
        except Exception:
            info["engine_api_status_code"] = None

        if args.json:
            print(json.dumps(info, indent=2, ensure_ascii=False))
        else:
            print(f"python: {info['python']}")
            print(f"db: {info['db_path']}")
            print(f"projects_root: {info['projects_root']} (exists={info['projects_dir_exists']})")
            print(f"patterns_yaml: {info['patterns_yaml']} (exists={info['patterns_yaml_exists']})")
            c = info["counts"]
            print(
                "counts: "
                f"prompt_usage={c['prompt_usage']} "
                f"unmatched={c['prompt_unmatched']} "
                f"watermarks={c['ingest_watermark']}"
            )
            if info.get("engine_api_status_code"):
                print(f"engine api: ok (status={info['engine_api_status_code']})")
            else:
                print("engine api: not reachable (ok if not running)")
            if info["unmatched_sample"]:
                print("unmatched sample:")
                for r in info["unmatched_sample"]:
                    ex = str(r.get("text_excerpt") or "").replace("\n", " ")
                    print(f"  - {r.get('date')} {r.get('session_id')}: {ex[:120]}")
        sys.exit(0)

    if args.reset:
        db.reset_ingest()

    report = ingest_all(db, projects_root, patterns_yaml)
    print(
        f"ingest: {report['total_user_messages']} msgs, "
        f"{report['matched_percent']}% matched, "
        f"{report['unmatched']} unmatched"
    )
    sys.exit(0)
