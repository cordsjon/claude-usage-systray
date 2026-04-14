"""CodeBurn session analytics — per-activity cost, project breakdown, model split.

Reads Claude Code JSONL session transcripts, groups entries into turns,
classifies activity categories, calculates costs, and tracks edit efficiency.
Thread-safe caching with 1-hour TTL, keyed by date range.

Stdlib only — no external packages.
"""

import glob
import json
import logging
import os
import re
import threading
import time
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from engine.sessions import _SESSIONS_BASE

log = logging.getLogger("engine.codeburn")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CACHE_TTL = 3600  # 1 hour
_PRICING_TTL = 86400  # 24 hours
_PRICING_CACHE_DIR = Path.home() / ".cache" / "codeburn"
_PRICING_CACHE_FILE = _PRICING_CACHE_DIR / "litellm-pricing.json"
_LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)

# Tool classification sets
EDIT_TOOLS = {"Edit", "Write", "FileEditTool", "FileWriteTool", "NotebookEdit"}
READ_TOOLS = {"Read", "Grep", "Glob", "FileReadTool", "GrepTool", "GlobTool"}
BASH_TOOLS = {"Bash", "BashTool", "PowerShellTool"}
TASK_TOOLS = {
    "TaskCreate", "TaskUpdate", "TaskGet", "TaskList",
    "TaskOutput", "TaskStop", "TodoWrite",
}
SEARCH_TOOLS = {"WebSearch", "WebFetch", "ToolSearch"}

# Keyword regexes (word-boundary)
_DEBUG_RE = re.compile(
    r"\b(fix|bug|error|broken|failing|crash|issue|debug|traceback|"
    r"exception|wrong|doesnt work|doesn't work)\b",
    re.IGNORECASE,
)
_FEATURE_RE = re.compile(
    r"\b(add|create|implement|new|build|feature|introduce|set up|"
    r"scaffold|generate)\b",
    re.IGNORECASE,
)
_REFACTOR_RE = re.compile(
    r"\b(refactor|clean up|rename|reorganize|simplify|extract|"
    r"restructure|move|migrate|split)\b",
    re.IGNORECASE,
)
_BRAINSTORM_RE = re.compile(
    r"\b(brainstorm|what if|design|idea|approach|strategy|consider|"
    r"think about)\b",
    re.IGNORECASE,
)
_RESEARCH_RE = re.compile(
    r"\b(research|investigate|explore|find out|look into|search for)\b",
    re.IGNORECASE,
)

# Bash command keyword regexes
_TEST_CMD_RE = re.compile(r"pytest|vitest|jest|unittest|cargo test", re.IGNORECASE)
_GIT_CMD_RE = re.compile(r"git\s+(push|commit|merge|rebase|cherry)", re.IGNORECASE)
_BUILD_CMD_RE = re.compile(
    r"npm\s+build|docker|pm2|make\b|cargo\s+build", re.IGNORECASE
)

# Fallback pricing ($/1M tokens)
FALLBACK_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-6": {"input": 15.0, "output": 75.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-5-20250514": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5-20251001": {"input": 0.80, "output": 4.0},
    "claude-opus-4-5-20250520": {"input": 15.0, "output": 75.0},
    "claude-3-5-sonnet-20241022": {"input": 3.0, "output": 15.0},
}

# ---------------------------------------------------------------------------
# Thread-safe cache (keyed by days)
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cached_reports: dict[int, dict] = {}  # days -> report
_cached_at: dict[int, float] = {}  # days -> monotonic timestamp

# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

_pricing_lock = threading.Lock()
_pricing_data: dict | None = None
_pricing_loaded_at: float = 0.0


def _fetch_litellm_pricing() -> dict | None:
    """Fetch LiteLLM pricing JSON, cache to disk for 24h."""
    # Check disk cache first
    if _PRICING_CACHE_FILE.exists():
        try:
            age = time.time() - _PRICING_CACHE_FILE.stat().st_mtime
            if age < _PRICING_TTL:
                with open(_PRICING_CACHE_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Failed to read pricing cache: %s", exc)

    # Fetch from GitHub
    try:
        req = urllib.request.Request(_LITELLM_URL, headers={"User-Agent": "codeburn/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)

        # Write disk cache
        _PRICING_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        import tempfile
        fd, tmp = tempfile.mkstemp(dir=_PRICING_CACHE_DIR, suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f)
            Path(tmp).replace(_PRICING_CACHE_FILE)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

        log.info("Fetched LiteLLM pricing (%d models)", len(data))
        return data
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as exc:
        log.warning("Failed to fetch LiteLLM pricing: %s", exc)
        return None


def _get_pricing() -> dict:
    """Return pricing dict, thread-safe with 24h TTL."""
    global _pricing_data, _pricing_loaded_at

    with _pricing_lock:
        now = time.monotonic()
        if _pricing_data is not None and (now - _pricing_loaded_at) < _PRICING_TTL:
            return _pricing_data

    data = _fetch_litellm_pricing()

    with _pricing_lock:
        _pricing_data = data
        _pricing_loaded_at = time.monotonic()

    return data


def _normalize_model_name(model: str) -> str:
    """Strip @provider suffix and -YYYYMMDD date suffix."""
    # Strip @provider
    if "@" in model:
        model = model.split("@")[0]
    # Strip trailing -YYYYMMDD
    model = re.sub(r"-\d{8}$", "", model)
    return model


def _get_model_pricing(model: str) -> dict[str, float]:
    """Get per-token pricing for a model.

    Returns {"input": float, "output": float, "cache_write": float, "cache_read": float}
    in $/token (not $/1M).
    """
    normalized = _normalize_model_name(model)

    # Try LiteLLM data first
    litellm = _get_pricing()
    if litellm:
        # Exact match
        entry = litellm.get(normalized) or litellm.get(model)
        if not entry:
            # Try with claude/ prefix
            entry = litellm.get(f"claude/{normalized}") or litellm.get(f"claude/{model}")
        if not entry:
            # Fuzzy prefix match
            for key, val in litellm.items():
                bare_key = key.split("/")[-1] if "/" in key else key
                if bare_key.startswith(normalized) or normalized.startswith(bare_key):
                    entry = val
                    break

        if entry and isinstance(entry, dict):
            inp = entry.get("input_cost_per_token", 0)
            out = entry.get("output_cost_per_token", 0)
            cw = entry.get("cache_creation_input_token_cost", inp * 1.25)
            cr = entry.get("cache_read_input_token_cost", inp * 0.1)
            return {"input": inp, "output": out, "cache_write": cw, "cache_read": cr}

    # Fallback
    for key, prices in FALLBACK_PRICING.items():
        if key == normalized or normalized.startswith(key) or key.startswith(normalized):
            inp = prices["input"] / 1_000_000
            out = prices["output"] / 1_000_000
            return {
                "input": inp,
                "output": out,
                "cache_write": inp * 1.25,
                "cache_read": inp * 0.1,
            }

    # Unknown model — zero cost rather than crash
    log.warning("No pricing found for model: %s (normalized: %s)", model, normalized)
    return {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}


# ---------------------------------------------------------------------------
# JSONL Parsing & Turn Grouping
# ---------------------------------------------------------------------------

def _extract_tool_names(content_blocks: list) -> list[str]:
    """Extract tool names from assistant message content blocks."""
    tools = []
    if not isinstance(content_blocks, list):
        return tools
    for block in content_blocks:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            name = block.get("name", "")
            if name:
                tools.append(name)
    return tools


def _extract_bash_commands(content_blocks: list) -> list[str]:
    """Extract raw bash command strings from Bash tool_use blocks."""
    commands = []
    if not isinstance(content_blocks, list):
        return commands
    for block in content_blocks:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            if block.get("name") in BASH_TOOLS:
                inp = block.get("input", {})
                if isinstance(inp, dict):
                    cmd = inp.get("command", "")
                    if cmd:
                        commands.append(cmd)
    return commands


def _extract_user_text(content_blocks) -> str:
    """Extract text from user message content."""
    if isinstance(content_blocks, str):
        return content_blocks
    if isinstance(content_blocks, list):
        parts = []
        for block in content_blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(parts)
    return ""


def _parse_bash_command_names(cmd: str) -> list[str]:
    """Extract command basenames from a bash command string.

    Strips quoted strings, splits on && ; |, takes first token of each segment.
    """
    # Remove quoted strings (single and double)
    stripped = re.sub(r'"[^"]*"', "", cmd)
    stripped = re.sub(r"'[^']*'", "", stripped)
    # Split on separators
    segments = re.split(r"[;&|]+", stripped)
    names = []
    for seg in segments:
        tokens = seg.strip().split()
        if tokens:
            name = os.path.basename(tokens[0])
            if name:
                names.append(name)
    return names


def _is_mcp_tool(name: str) -> bool:
    """Check if a tool name is an MCP tool."""
    return name.startswith("mcp__")


def _mcp_server_name(tool_name: str) -> str:
    """Extract MCP server name from tool name."""
    parts = tool_name.split("__")
    return parts[1] if len(parts) >= 2 else tool_name


def _project_name_from_cwd(cwd: str) -> str:
    """Derive project name from the cwd field in JSONL entries.

    The cwd is the actual working directory, e.g. /Users/.../projects/30_SVG-PAINT.
    Returns the first directory segment after "projects/" — the top-level project,
    not a subdirectory within it. If the cwd is the projects root itself
    (e.g. ~/projects), return "workspace".
    """
    if not cwd:
        return "unknown"
    parts = cwd.rstrip("/").split("/")
    # Find "projects" in the path and take the segment right after it
    for i, seg in enumerate(parts):
        if seg.lower() == "projects" and i + 1 < len(parts):
            return parts[i + 1]
    # If cwd ends with "projects", this is the workspace root
    if parts and parts[-1].lower() == "projects":
        return "workspace"
    # Fallback: last non-system segment
    for seg in reversed(parts):
        if seg and seg.lower() not in ("users", "user", "home", "projects", ""):
            return seg
    return "unknown"


def _project_name_from_path(session_path: str) -> str:
    """Derive project name from JSONL file path.

    Session files live under ~/.claude/projects/<project-path-encoded>/...
    The encoded directory replaces / with - in the original filesystem path.
    We reconstruct by finding the segment right after "projects" — the
    top-level project directory.
    """
    # Path structure: _SESSIONS_BASE / <encoded-project-dir> / *.jsonl
    rel = os.path.relpath(session_path, _SESSIONS_BASE)
    parts = rel.split(os.sep)
    if not parts:
        return "unknown"

    encoded = parts[0]
    # The encoded string is the original path with / replaced by -.
    # e.g. "-Users-jcords-macmini-projects-30_SVG-PAINT"
    # But project names can contain - (e.g. "SVG-PAINT"), making split
    # ambiguous. Strategy: find "projects" marker, then take everything
    # after it up to the next path-level boundary.

    # Try to find "-projects-" marker
    marker = "-projects-"
    idx = encoded.lower().find(marker)
    if idx >= 0:
        remainder = encoded[idx + len(marker):]
        # The remainder is the project path with subdirs still joined by -.
        # We can't perfectly split since - is ambiguous, but the alias map
        # handles consolidation. Return the full remainder as the raw name.
        return remainder if remainder else "workspace"

    # Check if it ends with "-projects" (workspace root)
    if encoded.lower().endswith("-projects"):
        return "workspace"

    # Fallback: last non-system segment (original logic)
    segments = encoded.split("-")
    for seg in reversed(segments):
        if seg and seg.lower() not in ("users", "user", "home", "projects"):
            return seg
    return "unknown"


# ---------------------------------------------------------------------------
# Project alias map — consolidates variant names to canonical project names.
#
# Different CWDs and encoded JSONL paths produce different names for the
# same project (e.g. "SVG-PAINT" vs "30_SVG-PAINT"). This map normalises
# after extraction so the dashboard shows one entry per real project.
#
# Keys are lowercase for case-insensitive matching.
# ---------------------------------------------------------------------------
_PROJECT_ALIASES: dict[str, str] = {
    # SVG-PAINT variants (path encoding produces "30_SVG-PAINT", "SVG-PAINT",
    # or "30-SVG-PAINT" depending on the original path separator)
    "svg-paint": "30_SVG-PAINT",
    "svgpaint": "30_SVG-PAINT",
    "30-svg-paint": "30_SVG-PAINT",
    # CONSIGLIERE
    "consigliere": "20_CONSIGLIERE",
    "20-consigliere": "20_CONSIGLIERE",
    # KETO variants
    "keto-data": "50_KETO",
    "keto_score": "50_KETO",
    "60_keto_score": "50_KETO",
    "50-keto": "50_KETO",
    "50-keto-keto-data": "50_KETO",
    # PosterEngine
    "posterengine": "20_PosterEngine",
    "poster-engine": "20_PosterEngine",
    "poster_engine": "20_PosterEngine",
    # Xperia
    "xperia_viz": "71_XPERIA_VIZ",
    "xperia-viz": "71_XPERIA_VIZ",
    # FontTriage
    "fonttriage": "40_FontTriage",
    "font-triage": "40_FontTriage",
    # Governance
    "governance": "00_Governance",
    "00-governance": "00_Governance",
    # Skill engineering
    "60-skillengineering": "60_skillengineering",
    # Sidequest
    "remotion-video": "10_Sidequest",
    # Dagu config directory
    ".config-dagu-dags": "dagu",
}


# ---------------------------------------------------------------------------
# Session attribution — manual overrides for workspace sessions.
#
# Sessions started from ~/projects get classified as "workspace" because the
# CWD is the projects root. At session end (/lightsout, /handoff), the actual
# project(s) worked on can be recorded to:
#   ~/.local/state/codeburn/session-projects.jsonl
#
# Format per line:
#   {"session_id": "<uuid>", "project": "<canonical name>", "date": "YYYY-MM-DD"}
#
# Multiple lines per session_id are allowed (multi-project sessions) — the
# first entry wins (primary project). The file is append-only and cached.
# ---------------------------------------------------------------------------
_ATTRIBUTION_PATH = os.path.expanduser(
    "~/.local/state/codeburn/session-projects.jsonl"
)
_attribution_cache: dict[str, str] | None = None
_attribution_mtime: float = 0.0


def _load_attribution() -> dict[str, str]:
    """Load session→project attribution, cached by file mtime."""
    global _attribution_cache, _attribution_mtime
    try:
        mtime = os.path.getmtime(_ATTRIBUTION_PATH)
    except OSError:
        return {}
    if _attribution_cache is not None and mtime == _attribution_mtime:
        return _attribution_cache
    result: dict[str, str] = {}
    try:
        with open(_ATTRIBUTION_PATH, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    sid = entry.get("session_id", "")
                    proj = entry.get("project", "")
                    if sid and proj and sid not in result:
                        result[sid] = proj
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    _attribution_cache = result
    _attribution_mtime = mtime
    log.info("Loaded %d session attributions", len(result))
    return result


def _normalize_project_name(raw_name: str) -> str:
    """Apply alias map to consolidate variant project names."""
    key = raw_name.lower()
    canonical = _PROJECT_ALIASES.get(key)
    if canonical:
        return canonical
    # Try without numeric prefix (e.g. "30_SVG-PAINT" → lookup "svg-paint")
    stripped = re.sub(r"^\d+[_-]", "", raw_name)
    canonical = _PROJECT_ALIASES.get(stripped.lower())
    if canonical:
        return canonical
    # For path-encoded names with subdirs (e.g. "30_SVG-PAINT-scripts"),
    # try progressively shorter prefixes to find the project root
    if "-" in raw_name:
        parts = raw_name.split("-")
        for length in range(len(parts) - 1, 0, -1):
            prefix = "-".join(parts[:length]).lower()
            canonical = _PROJECT_ALIASES.get(prefix)
            if canonical:
                return canonical
    return raw_name


def _estimate_system_overhead() -> int:
    """Estimate per-turn system overhead tokens from CLAUDE.md + runtime injections.

    These are invisible in JSONL — injected by Claude Code's runtime every turn:
    - System prompt base (~2,000 tokens)
    - CLAUDE.md files (measured from disk)
    - Skill catalog listing (~15 tokens per skill)
    - Deferred tool names (~8 tokens per tool)
    - MCP server instructions (~1,000 tokens)
    - Memory index MEMORY.md (measured from disk)

    Returns estimated tokens per turn.
    """
    overhead = 2000  # base system prompt

    # Measure CLAUDE.md files
    home = os.path.expanduser("~")
    claude_md_paths = [
        os.path.join(home, ".claude", "CLAUDE.md"),
        os.path.join(home, "projects", ".claude", "CLAUDE.md"),
    ]
    for p in claude_md_paths:
        try:
            overhead += os.path.getsize(p) // 4  # ~4 chars per token
        except OSError:
            pass

    # Memory index
    memory_path = os.path.join(
        home, ".claude", "projects",
        "-Users-jcords-macmini-projects", "memory", "MEMORY.md"
    )
    try:
        overhead += os.path.getsize(memory_path) // 4
    except OSError:
        pass

    # Skill catalog: count .md files in ~/.claude/commands
    commands_dir = os.path.join(home, ".claude", "commands")
    try:
        skill_count = sum(
            1 for root, _, files in os.walk(commands_dir)
            for f in files if f.endswith(".md")
        )
        overhead += skill_count * 15  # ~15 tokens per skill listing
    except OSError:
        overhead += 1500  # fallback estimate

    # Deferred tools: count from MCP config
    claude_json = os.path.join(home, ".claude.json")
    try:
        with open(claude_json) as f:
            import json as _json
            cfg = _json.load(f)
            server_count = len(cfg.get("mcpServers", {}))
            overhead += server_count * 250  # ~250 tokens per server (instructions + tool list)
    except (OSError, ValueError):
        overhead += 1000

    return overhead


def _scan_sessions(date_from: datetime, date_to: datetime) -> dict:
    """Scan JSONL files and build the full codeburn report.

    Args:
        date_from: Start of range (inclusive), timezone-aware UTC.
        date_to: End of range (inclusive), timezone-aware UTC.

    Returns:
        Full report dict matching the API response shape.
    """
    files = glob.glob(os.path.join(_SESSIONS_BASE, "**/*.jsonl"), recursive=True)
    # Exclude agent-*.jsonl
    files = [f for f in files if not os.path.basename(f).startswith("agent-")]
    log.info("CodeBurn scanning %d session files for %s to %s",
             len(files), date_from.date(), date_to.date())

    # Collect all entries with dedup
    seen_ids: set[str] = set()
    # entries grouped by file for project attribution
    # Each entry: (timestamp_dt, role, message_dict, file_path, cwd)
    all_entries: list[tuple[datetime, str, dict, str, str]] = []

    for fpath in files:
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue

                    # Skip sidechain entries (agent-delegated work)
                    if obj.get("isSidechain", False):
                        continue

                    msg = obj.get("message")
                    if not isinstance(msg, dict):
                        continue

                    role = msg.get("role", "")
                    if role not in ("user", "assistant"):
                        continue

                    # Dedup by message.id (assistant) or uuid (user)
                    msg_id = msg.get("id") or obj.get("uuid")
                    if msg_id:
                        if msg_id in seen_ids:
                            continue
                        seen_ids.add(msg_id)

                    # Parse timestamp
                    ts_str = obj.get("timestamp")
                    if not ts_str:
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except (ValueError, TypeError):
                        continue

                    # Filter by date range
                    if ts < date_from or ts > date_to:
                        continue

                    all_entries.append((ts, role, msg, fpath, obj.get("cwd", "")))
        except OSError:
            continue

    # Sort by timestamp
    all_entries.sort(key=lambda e: e[0])

    # Load session attribution overrides (for workspace sessions)
    attribution = _load_attribution()

    # Group into turns: one user message + subsequent assistant messages
    turns: list[dict] = []
    current_turn: dict | None = None

    for ts, role, msg, fpath, cwd in all_entries:
        if role == "user":
            if current_turn is not None:
                turns.append(current_turn)

            project = _normalize_project_name(
                _project_name_from_cwd(cwd) if cwd else _project_name_from_path(fpath)
            )
            # Override "workspace" with manual attribution if available
            if project == "workspace":
                session_id = Path(fpath).stem  # UUID from filename
                if session_id in attribution:
                    project = attribution[session_id]

            current_turn = {
                "timestamp": ts,
                "date": ts.strftime("%Y-%m-%d"),
                "user_text": _extract_user_text(msg.get("content", "")),
                "api_calls": [],
                "file_path": fpath,
                "project": project,
            }
        elif role == "assistant" and current_turn is not None:
            current_turn["api_calls"].append(msg)

    if current_turn is not None:
        turns.append(current_turn)

    # Process each turn
    cat_agg: dict[str, dict] = defaultdict(lambda: {
        "turns": 0, "cost_usd": 0.0,
        "edit_turns": 0, "oneshot_turns": 0, "retries": 0,
    })
    model_agg: dict[str, dict] = defaultdict(lambda: {
        "calls": 0, "cost_usd": 0.0,
        "input": 0, "output": 0, "cache_read": 0, "cache_create": 0,
    })
    project_agg: dict[str, dict] = defaultdict(lambda: {
        "cost_usd": 0.0, "turns": 0, "path": "",
    })
    tool_agg: dict[str, int] = defaultdict(int)
    mcp_agg: dict[str, int] = defaultdict(int)
    daily_agg: dict[str, dict] = defaultdict(lambda: {
        "cost_usd": 0.0, "turns": 0,
    })

    for turn in turns:
        # Collect all tools and bash commands across API calls
        all_tools: list[str] = []
        all_bash_cmds: list[str] = []
        turn_cost = 0.0
        has_edits = False

        for api_call in turn["api_calls"]:
            content = api_call.get("content", [])
            tools = _extract_tool_names(content)
            bash_cmds = _extract_bash_commands(content)
            all_tools.extend(tools)
            all_bash_cmds.extend(bash_cmds)

            if any(t in EDIT_TOOLS for t in tools):
                has_edits = True

            # Count tools
            for t in tools:
                tool_agg[t] += 1
                if _is_mcp_tool(t):
                    mcp_agg[_mcp_server_name(t)] += 1

            # Cost calculation per API call
            usage = api_call.get("usage", {})
            model = api_call.get("model", "")
            if usage and model:
                pricing = _get_model_pricing(model)
                inp_tokens = usage.get("input_tokens", 0)
                out_tokens = usage.get("output_tokens", 0)
                cc_tokens = usage.get("cache_creation_input_tokens", 0)
                cr_tokens = usage.get("cache_read_input_tokens", 0)

                # Web search requests from usage.server_tool_use
                server_tool_use = usage.get("server_tool_use", {})
                ws_count = server_tool_use.get("web_search_requests", 0)

                # Speed multiplier: usage.speed == "fast" for fast mode
                speed = usage.get("speed", "standard")
                multiplier = 1.25 if speed == "fast" else 1.0
                call_cost = multiplier * (
                    inp_tokens * pricing["input"]
                    + out_tokens * pricing["output"]
                    + cc_tokens * pricing["cache_write"]
                    + cr_tokens * pricing["cache_read"]
                    + ws_count * 0.01
                )
                turn_cost += call_cost

                # Model aggregation
                norm_model = _normalize_model_name(model)
                model_agg[norm_model]["calls"] += 1
                model_agg[norm_model]["cost_usd"] += call_cost
                model_agg[norm_model]["input"] += inp_tokens
                model_agg[norm_model]["output"] += out_tokens
                model_agg[norm_model]["cache_read"] += cr_tokens
                model_agg[norm_model]["cache_create"] += cc_tokens

        # One-shot rate tracking
        retries = _count_retries(turn["api_calls"])

        # Classification
        tool_set = set(all_tools)
        category = _classify_turn(tool_set, all_bash_cmds, turn["user_text"])

        # Aggregate category
        cat = cat_agg[category]
        cat["turns"] += 1
        cat["cost_usd"] += turn_cost
        if has_edits:
            cat["edit_turns"] += 1
            if retries == 0:
                cat["oneshot_turns"] += 1
            cat["retries"] += retries

        # Aggregate project
        proj = turn["project"]
        project_agg[proj]["turns"] += 1
        project_agg[proj]["cost_usd"] += turn_cost
        if not project_agg[proj]["path"]:
            project_agg[proj]["path"] = turn["file_path"]

        # Aggregate daily
        daily_agg[turn["date"]]["turns"] += 1
        daily_agg[turn["date"]]["cost_usd"] += turn_cost

    # Build response
    total_cost = sum(d["cost_usd"] for d in daily_agg.values())
    total_turns = len(turns)

    categories = sorted(
        [{"name": k, **v} for k, v in cat_agg.items()],
        key=lambda x: x["cost_usd"],
        reverse=True,
    )

    models = sorted(
        [
            {
                "name": k,
                "calls": v["calls"],
                "cost_usd": round(v["cost_usd"], 2),
                "tokens": {
                    "input": v["input"],
                    "output": v["output"],
                    "cache_read": v["cache_read"],
                    "cache_create": v["cache_create"],
                },
            }
            for k, v in model_agg.items()
        ],
        key=lambda x: x["cost_usd"],
        reverse=True,
    )

    projects = sorted(
        [
            {"name": k, "path": v["path"], "cost_usd": round(v["cost_usd"], 2), "turns": v["turns"]}
            for k, v in project_agg.items()
        ],
        key=lambda x: x["cost_usd"],
        reverse=True,
    )

    tools_list = sorted(
        [{"name": k, "calls": v} for k, v in tool_agg.items()],
        key=lambda x: x["calls"],
        reverse=True,
    )

    mcp_servers = sorted(
        [{"name": k, "calls": v} for k, v in mcp_agg.items()],
        key=lambda x: x["calls"],
        reverse=True,
    )

    daily = [
        {"date": d, "cost_usd": round(v["cost_usd"], 2), "turns": v["turns"]}
        for d, v in sorted(daily_agg.items())
    ]

    # Round costs and compute one-shot rates per category
    total_edit_turns = 0
    total_oneshot_turns = 0
    for c in categories:
        c["cost_usd"] = round(c["cost_usd"], 2)
        total_edit_turns += c["edit_turns"]
        total_oneshot_turns += c["oneshot_turns"]
        c["oneshot_rate"] = (
            round(c["oneshot_turns"] / c["edit_turns"], 3)
            if c["edit_turns"] > 0 else None
        )

    overall_oneshot_rate = (
        round(total_oneshot_turns / total_edit_turns, 3)
        if total_edit_turns > 0 else None
    )

    # -----------------------------------------------------------------------
    # Context overhead estimation
    #
    # Claude Code injects invisible context every turn: system prompt,
    # CLAUDE.md files, skill catalog, deferred tool list, MCP instructions,
    # memory index. None of this appears in JSONL — but we can estimate it
    # from the gap between total input tokens and actual user content.
    #
    # Method: for each API call, input_tokens = user content + system overhead
    # + conversation history. cache_read_input_tokens tells us how much was
    # served from cache (cheap) vs fresh (expensive). The ratio reveals how
    # much is "boilerplate" that gets cached vs "new work."
    # -----------------------------------------------------------------------
    total_input = sum(m["input"] for m in model_agg.values())
    total_output = sum(m["output"] for m in model_agg.values())
    total_cache_read = sum(m["cache_read"] for m in model_agg.values())
    total_cache_create = sum(m["cache_create"] for m in model_agg.values())
    total_api_calls = sum(m["calls"] for m in model_agg.values())

    # Estimate system overhead per turn from CLAUDE.md + skill catalog + deferred tools
    # These are constants injected by the runtime, not visible in JSONL
    overhead_per_turn_est = _estimate_system_overhead()

    context_overhead = {
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_cache_read_tokens": total_cache_read,
        "total_cache_create_tokens": total_cache_create,
        "total_context_tokens": total_input + total_cache_read + total_cache_create,
        "cache_hit_rate": round(
            total_cache_read / (total_input + total_cache_read + total_cache_create), 3
        ) if (total_input + total_cache_read + total_cache_create) > 0 else 0,
        "io_ratio": round(total_input / total_output, 3) if total_output > 0 else 0,
        "full_context_ratio": round(
            (total_input + total_cache_read + total_cache_create) / total_output, 1
        ) if total_output > 0 else 0,
        "est_overhead_per_turn": overhead_per_turn_est,
        "est_total_overhead": overhead_per_turn_est * total_turns,
        "est_overhead_pct": round(
            (overhead_per_turn_est * total_turns)
            / (total_input + total_cache_read + total_cache_create) * 100, 1
        ) if (total_input + total_cache_read + total_cache_create) > 0 else 0,
        "avg_input_per_turn": round(total_input / total_turns) if total_turns > 0 else 0,
        "avg_output_per_turn": round(total_output / total_turns) if total_turns > 0 else 0,
        "avg_context_per_call": round(
            (total_input + total_cache_read + total_cache_create) / total_api_calls
        ) if total_api_calls > 0 else 0,
    }

    return {
        "period": {
            "from": date_from.strftime("%Y-%m-%d"),
            "to": date_to.strftime("%Y-%m-%d"),
        },
        "total_cost_usd": round(total_cost, 2),
        "total_turns": total_turns,
        "total_edit_turns": total_edit_turns,
        "total_oneshot_turns": total_oneshot_turns,
        "oneshot_rate": overall_oneshot_rate,
        "categories": categories,
        "models": models,
        "projects": projects,
        "tools": tools_list,
        "mcp_servers": mcp_servers,
        "daily": daily,
        "context_overhead": context_overhead,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Turn Classification (pure functions)
# ---------------------------------------------------------------------------

def _classify_turn(tool_set: set[str], bash_cmds: list[str], user_text: str) -> str:
    """Classify a turn into one of 13 categories.

    Two-pass heuristic: tool pattern matching, then keyword refinement.
    """
    category = _pass1_tool_matching(tool_set, bash_cmds)
    return _pass2_keyword_refinement(category, user_text)


def _pass1_tool_matching(tool_set: set[str], bash_cmds: list[str]) -> str:
    """Pass 1: classify by tool usage patterns."""
    if not tool_set:
        return "no_tools"

    # 1. EnterPlanMode
    if "EnterPlanMode" in tool_set:
        return "planning"

    # 2. Agent
    if "Agent" in tool_set:
        return "delegation"

    has_bash = bool(tool_set & BASH_TOOLS)
    has_edit = bool(tool_set & EDIT_TOOLS)
    has_read = bool(tool_set & READ_TOOLS)
    has_task = bool(tool_set & TASK_TOOLS)
    has_search = bool(tool_set & SEARCH_TOOLS)
    has_mcp = any(_is_mcp_tool(t) for t in tool_set)
    bash_only = has_bash and not has_edit and not has_read and not has_task

    # Concatenate bash commands for regex matching
    bash_text = " ".join(bash_cmds)

    # 3. Bash-only + test
    if bash_only and _TEST_CMD_RE.search(bash_text):
        return "testing"

    # 4. Bash-only + git
    if bash_only and _GIT_CMD_RE.search(bash_text):
        return "git"

    # 5. Bash-only + build
    if bash_only and _BUILD_CMD_RE.search(bash_text):
        return "build/deploy"

    # 6. Any edit tool
    if has_edit:
        return "coding"

    # 7. Bash + read tools, no edits
    if has_bash and has_read and not has_edit:
        return "exploration"

    # 8. WebSearch/WebFetch/MCP
    if has_search or has_mcp:
        return "exploration"

    # 9. Read-only tools
    if has_read and not has_bash and not has_edit:
        return "exploration"

    # 10. Task tools without edits
    if has_task and not has_edit:
        return "planning"

    # 11. Skill tool
    if "Skill" in tool_set:
        return "general"

    # 12. Fallback — has tools but didn't match any pattern
    return "general"


def _pass2_keyword_refinement(category: str, user_text: str) -> str:
    """Pass 2: refine category using user message keywords."""
    if category == "coding":
        if _DEBUG_RE.search(user_text):
            return "debugging"
        if _REFACTOR_RE.search(user_text):
            return "refactoring"
        if _FEATURE_RE.search(user_text):
            return "feature"
        return "coding"

    if category == "exploration":
        if _DEBUG_RE.search(user_text):
            return "debugging"
        return "exploration"

    if category == "no_tools":
        if _BRAINSTORM_RE.search(user_text):
            return "brainstorming"
        if _RESEARCH_RE.search(user_text):
            return "exploration"
        if _DEBUG_RE.search(user_text):
            return "debugging"
        if _FEATURE_RE.search(user_text):
            return "feature"
        return "conversation"

    return category


# ---------------------------------------------------------------------------
# One-Shot Rate
# ---------------------------------------------------------------------------

def _count_retries(api_calls: list[dict]) -> int:
    """Count edit->bash->edit retry cycles within a turn.

    Iterates over individual tool_use blocks across all API calls in the turn,
    since Claude Code puts multiple tool_use blocks within a single assistant message.

    Returns the number of retries (0 = one-shot success if edits were present).
    """
    saw_edit = False
    saw_bash_after_edit = False
    retries = 0

    for api_call in api_calls:
        content = api_call.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_name = block.get("name", "")
            is_edit = tool_name in EDIT_TOOLS
            is_bash = tool_name in BASH_TOOLS

            if is_edit:
                if saw_bash_after_edit:
                    retries += 1
                saw_edit = True
                saw_bash_after_edit = False

            if is_bash and saw_edit:
                saw_bash_after_edit = True

    return retries


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_codeburn_report(days: int) -> dict:
    """Return cached codeburn report for the given date range.

    Args:
        days: Number of days to look back (0 or negative = all time).

    Returns:
        Full report dict with categories, models, projects, tools, etc.
    """
    global _cached_reports, _cached_at

    with _cache_lock:
        now = time.monotonic()
        cached_time = _cached_at.get(days, 0.0)
        cached_report = _cached_reports.get(days)
        if cached_report is not None and (now - cached_time) < _CACHE_TTL:
            return cached_report

    # Compute date range
    date_to = datetime.now(timezone.utc)
    if days > 0:
        date_from = date_to - timedelta(days=days)
    else:
        # All time — go back far enough
        date_from = datetime(2024, 1, 1, tzinfo=timezone.utc)

    # Scan outside the lock (slow operation)
    report = _scan_sessions(date_from, date_to)

    with _cache_lock:
        _cached_reports[days] = report
        _cached_at[days] = time.monotonic()

    return report
