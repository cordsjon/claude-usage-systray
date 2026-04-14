#!/usr/bin/env python3
"""Backfill session-projects.jsonl by scanning workspace JSONL sessions.

For each workspace session, extracts file paths from:
  - cwd fields (working directories visited during the session)
  - tool_use content (Read/Edit/Write/Glob/Grep file_path arguments)
  - user message text (file paths mentioned)

Then determines the dominant project by counting path hits per project.
"""

import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

WORKSPACE_DIR = os.path.expanduser(
    "~/.claude/projects/-Users-jcords-macmini-projects"
)
OUTPUT = os.path.expanduser("~/.local/state/codeburn/session-projects.jsonl")
PROJECTS_ROOT = "/Users/jcords-macmini/projects/"

# Known project directories (canonical names)
PROJECT_DIRS = [
    "00_Governance", "00_portmgr", "00_devhub",
    "10_Sidequest", "10_agentflow-demo", "10_SI", "10_STEUB-usecases",
    "10_command-promo",
    "15_SAAS",  # parent of 20_PosterEngine
    "20_CONSIGLIERE",
    "30_SVG-PAINT", "30_briefing-publisher", "30_Lecture2skill",
    "35_AIRLINE-UX-AUDIT",
    "40_FontTriage", "40_convergence",
    "50_KETO", "50_Excelbridge",
    "60_skillengineering",
    "70_ASSET-ENGINE", "70_XPERIA_SOA",
    "71_XPERIA_VIZ", "72_SONY_RESEARCH",
    "75_Coaching",
    "90_atlas_os",
    "claude-usage-systray",
    "cockpit", "deploy", "homer", "nanobot",
    "sonyjuke", "solar-app", "bb-dashboard",
    "platform-tools", "phantom-sync",
]

# Aliases: subdir or variant name → canonical
ALIASES = {
    "SVG-PAINT": "30_SVG-PAINT",
    "svg-paint": "30_SVG-PAINT",
    "20_PosterEngine": "15_SAAS",
    "poster_engine": "15_SAAS",
    "poster-engine": "15_SAAS",
    "keto-data": "50_KETO",
    "keto_score": "50_KETO",
    "60_keto_score": "50_KETO",
    "Governance": "00_Governance",
    "governance": "00_Governance",
    "Consigliere": "20_CONSIGLIERE",
    "consigliere": "20_CONSIGLIERE",
    "FontTriage": "40_FontTriage",
    "remotion-video": "10_Sidequest",
    "XPERIA_SOA": "70_XPERIA_SOA",
    "XPERIA_VIZ": "71_XPERIA_VIZ",
    "ASSET-ENGINE": "70_ASSET-ENGINE",
    "skillengineering": "60_skillengineering",
    "agentflow-demo": "10_agentflow-demo",
    "STEUB-usecases": "10_STEUB-usecases",
    "portmgr": "00_portmgr",
    "devhub": "00_devhub",
    "Excelbridge": "50_Excelbridge",
    "AIRLINE-UX-AUDIT": "35_AIRLINE-UX-AUDIT",
    "convergence": "40_convergence",
    "SONY_RESEARCH": "72_SONY_RESEARCH",
    "Coaching": "75_Coaching",
    "briefing-publisher": "30_briefing-publisher",
    "Lecture2skill": "30_Lecture2skill",
    "SI": "10_SI",
    "command-promo": "10_command-promo",
    # Subdirectory noise — map to parent projects
    "docs": None,  # too ambiguous
    "scripts": None,
    "templates": None,
    "assets": None,
    "tests": None,
    "src": None,
    "ts": None,
    "web": None,
    ".vexp": None,
    ".claude": None,
    ".claude-code-leak": None,
    "s-macmini": None,  # path fragment
    "page-agent": None,
    "yxl\npath = \"": None,  # corrupt entry
}


def extract_project_from_path(filepath: str) -> str | None:
    """Extract project name from a file path."""
    if not filepath or PROJECTS_ROOT not in filepath:
        return None
    remainder = filepath[len(PROJECTS_ROOT):]
    # First segment is the project directory
    top = remainder.split("/")[0]
    if not top:
        return None
    # Check aliases
    if top in ALIASES:
        return ALIASES[top]  # None means "skip this — ambiguous subdirectory"
    # Check known projects
    if top in PROJECT_DIRS:
        return top
    # Try alias lookup for partial matches
    for alias, canonical in ALIASES.items():
        if top.lower() == alias.lower():
            return canonical
    # Reject single-word generic names (likely subdirectories)
    if len(top) < 4 or top.startswith("."):
        return None
    return top


def scan_session(jsonl_path: str) -> tuple[str | None, str]:
    """Scan a session JSONL and return (dominant_project, date).

    Returns None for project if no clear signal found.
    """
    project_hits = Counter()
    session_date = ""

    try:
        with open(jsonl_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue

                # Extract date from first timestamp
                if not session_date:
                    ts = obj.get("timestamp", "")
                    if ts and len(ts) >= 10:
                        session_date = ts[:10]

                # Signal 1: CWD field
                cwd = obj.get("cwd", "")
                if cwd and PROJECTS_ROOT in cwd:
                    proj = extract_project_from_path(cwd + "/")
                    if proj:
                        project_hits[proj] += 3  # CWD is strong signal

                # Signal 2: tool_use content blocks
                msg = obj.get("message", {})
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_use":
                            inp = block.get("input", {})
                            if isinstance(inp, dict):
                                # File paths in tool arguments
                                for key in ("file_path", "path", "command"):
                                    val = inp.get(key, "")
                                    if isinstance(val, str) and PROJECTS_ROOT in val:
                                        proj = extract_project_from_path(val)
                                        if proj:
                                            weight = 2 if key == "file_path" else 1
                                            project_hits[proj] += weight

                        elif block.get("type") == "tool_result":
                            # Check tool result content for paths
                            result_content = block.get("content", "")
                            if isinstance(result_content, str):
                                for match in re.finditer(
                                    r"/Users/jcords-macmini/projects/([^/\s\"']+)",
                                    result_content[:2000],  # Cap to avoid huge results
                                ):
                                    proj = extract_project_from_path(
                                        PROJECTS_ROOT + match.group(1) + "/"
                                    )
                                    if proj:
                                        project_hits[proj] += 1

    except OSError:
        return None, session_date

    if not project_hits:
        return None, session_date

    # Return the dominant project
    top = project_hits.most_common(1)[0]
    # Only attribute if there's a clear signal (at least 3 hits)
    if top[1] < 3:
        return None, session_date

    return top[0], session_date


def main():
    # Load existing attributions to skip
    existing: set[str] = set()
    if os.path.exists(OUTPUT):
        with open(OUTPUT, "r") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    existing.add(entry.get("session_id", ""))
                except (json.JSONDecodeError, ValueError):
                    continue

    # Scan all workspace sessions
    session_files = sorted(
        Path(WORKSPACE_DIR).glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
    )
    # Exclude agent files
    session_files = [f for f in session_files if not f.name.startswith("agent-")]

    print(f"Scanning {len(session_files)} workspace sessions...")
    print(f"Already attributed: {len(existing)}")

    attributed = 0
    skipped_existing = 0
    skipped_no_signal = 0
    project_counts: Counter = Counter()

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)

    with open(OUTPUT, "a") as out:
        for i, fpath in enumerate(session_files):
            session_id = fpath.stem
            if session_id in existing:
                skipped_existing += 1
                continue

            project, date = scan_session(str(fpath))
            if project:
                entry = {
                    "session_id": session_id,
                    "project": project,
                    "date": date,
                }
                out.write(json.dumps(entry) + "\n")
                attributed += 1
                project_counts[project] += 1
            else:
                skipped_no_signal += 1

            if (i + 1) % 50 == 0:
                print(f"  Processed {i + 1}/{len(session_files)}...")

    print(f"\nDone!")
    print(f"  Attributed: {attributed}")
    print(f"  Skipped (existing): {skipped_existing}")
    print(f"  Skipped (no signal): {skipped_no_signal}")
    print(f"\nProject breakdown:")
    for proj, count in project_counts.most_common():
        print(f"  {proj:30s} {count:4d} sessions")


if __name__ == "__main__":
    main()
