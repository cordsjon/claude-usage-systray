"""YAML pattern loader + message classifier (US-TB-01 AC-02).

Reads a ``patterns.yaml`` with a top-level ``patterns:`` list of
``{id, intent, regex, type, version}`` entries and compiles the regexes.
``classify_message`` returns a dict with ``pattern_id``, ``is_structured``,
and ``version``; structured slash commands are detected ahead of the
unstructured regex list and their ID is the slash name (args stripped).
"""

import re
from pathlib import Path

import yaml


_SLASH_RE = re.compile(r"^/([a-z][a-z0-9:_\-]*)")
# Claude Code wraps slash-command invocations as
# <command-name>/sh:plan</command-name> blocks inside the user message body,
# not as the literal `/sh:plan` string. Detect both shapes.
_COMMAND_TAG_RE = re.compile(
    r"<command-name>\s*/([a-z][a-z0-9:_\-]*)", re.IGNORECASE
)
# Claude Code machinery that floods user-role entries — classify as
# pattern_id="<machinery>" so it never counts as a real prompt.
_MACHINERY_RE = re.compile(
    r"^\s*(?:<ide_opened_file>|<task-notification>|\[Image:|<local-command-stdout>)",
    re.IGNORECASE,
)


def load_patterns(yaml_path):
    """Load + compile the pattern list from ``yaml_path``."""
    data = yaml.safe_load(Path(yaml_path).read_text())
    compiled = []
    for entry in (data or {}).get("patterns", []):
        compiled.append(
            {
                "id": entry["id"],
                "intent": entry.get("intent", ""),
                "regex": re.compile(entry["regex"]),
                "type": entry.get("type", "unstructured"),
                "version": int(entry.get("version", 1)),
            }
        )
    return compiled


def classify_message(text, patterns):
    """Return ``{pattern_id, is_structured, version}`` for a user message.

    Structured slash commands (``/foo args...``) take precedence; pattern_id
    is the slash name. Otherwise the first matching unstructured regex wins.
    Unmatched messages get ``pattern_id=None``.
    """
    stripped = text.lstrip()
    if _MACHINERY_RE.match(stripped):
        return {"pattern_id": "_machinery", "is_structured": False, "version": 0}
    m = _SLASH_RE.match(stripped)
    if m:
        return {"pattern_id": m.group(1), "is_structured": True, "version": 0}
    m = _COMMAND_TAG_RE.search(stripped)
    if m:
        return {"pattern_id": m.group(1), "is_structured": True, "version": 0}
    for p in patterns:
        if p["regex"].search(stripped):
            return {
                "pattern_id": p["id"],
                "is_structured": False,
                "version": p["version"],
            }
    return {"pattern_id": None, "is_structured": False, "version": 0}
