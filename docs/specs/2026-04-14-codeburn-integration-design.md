# CodeBurn Integration — Design Spec

**Date:** 2026-04-14
**Status:** Draft
**Scope:** Integrate codeburn-style session analytics into the existing claude-usage-systray engine and dashboard.

## Problem

The systray tracks API-level utilization (runway, pacing, burn rate) but not *where* tokens go. Per-activity cost, per-project breakdown, per-model split, and edit efficiency (one-shot rate) are invisible. The open-source [codeburn](https://github.com/AgentSeal/codeburn) project solves this by reading Claude Code JSONL session transcripts. This spec ports the core analytics into the existing engine as new dashboard tabs.

## Architecture

### New Module: `engine/codeburn.py`

Pure-function analytics module. No I/O except JSONL file reads and a one-time LiteLLM pricing fetch. Thread-safe caching with 1-hour TTL (same as `sessions.py`).

#### Turn Grouping

JSONL entries are grouped into **turns**: one user message + all subsequent assistant API calls before the next user message. This is the atomic unit for classification and cost attribution.

- Deduplication by `message.id` across sessions (prevents double-counting on resume)
- Date filtering per entry timestamp, not per session

#### 13-Category Classifier

Two-pass deterministic heuristic (no LLM calls):

**Pass 1 — Tool pattern matching:**

| Priority | Condition | Category |
|----------|-----------|----------|
| 1 | `EnterPlanMode` tool present | planning |
| 2 | `Agent` tool present | delegation |
| 3 | Bash-only + test keywords (pytest/vitest/jest) | testing |
| 4 | Bash-only + git keywords | git |
| 5 | Bash-only + build keywords (npm build/docker/pm2) | build/deploy |
| 6 | Any edit tool (Edit/Write/NotebookEdit) | coding |
| 7 | Bash + read tools, no edits | exploration |
| 8 | WebSearch/WebFetch/MCP tools | exploration |
| 9 | Read-only tools | exploration |
| 10 | Task tools without edits | planning |
| 11 | Skill tool | general |
| 12 | No tools | → conversation classifier |

**Pass 2 — Keyword refinement:**

- `coding` → check for debug/refactor/feature keywords → reclassify if matched
- `exploration` → check for debug keywords → reclassify if matched
- No-tool turns: brainstorm → exploration → debug → feature → conversation (fallback)

**Tool sets (constants):**

```python
EDIT_TOOLS = {"Edit", "Write", "FileEditTool", "FileWriteTool", "NotebookEdit"}
READ_TOOLS = {"Read", "Grep", "Glob", "FileReadTool", "GrepTool", "GlobTool"}
BASH_TOOLS = {"Bash", "BashTool", "PowerShellTool"}
TASK_TOOLS = {"TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "TaskOutput", "TaskStop", "TodoWrite"}
SEARCH_TOOLS = {"WebSearch", "WebFetch", "ToolSearch"}
```

#### One-Shot Rate Detection

Tracks edit→bash→edit retry cycles within a turn:

```
for each API call in turn:
  if call has edit tools:
    if saw_bash_after_edit: retries += 1
    saw_edit = True; saw_bash_after_edit = False
  if call has bash tools AND saw_edit:
    saw_bash_after_edit = True
```

A turn with `retries == 0` AND `has_edits == True` is a one-shot success.

#### Cost Calculation

```
cost = multiplier * (
    input_tokens * input_price +
    output_tokens * output_price +
    cache_create_tokens * cache_write_price +
    cache_read_tokens * cache_read_price +
    web_search_requests * 0.01
)
```

Where `multiplier` = 1.25 if `speed == "fast"`, else 1.0.

**Pricing source (priority order):**
1. LiteLLM `model_prices_and_context_window.json` from GitHub raw (cached 24h at `~/.cache/codeburn/litellm-pricing.json`)
2. Hardcoded fallback prices covering Claude Opus 4/4.5/4.6, Sonnet 3.5/3.7/4/4.5/4.6, Haiku 4.5

Cache defaults: `cache_write = input_price * 1.25`, `cache_read = input_price * 0.1` when not specified.

Model name normalization: strip `@provider` suffix and `-YYYYMMDD` date suffix before lookup.

#### Caching

Thread-safe in-memory cache with 1-hour TTL (same pattern as `sessions.py`). The full report is cached, not individual sessions. Cache key includes the date range.

### API Route: `GET /api/codeburn?range=7d|30d|52w|all`

New `elif` branch in `api.py` `do_GET`. Delegates to `codeburn.get_codeburn_report(days)`.

**Response shape:**

```json
{
  "period": {"from": "2026-04-07", "to": "2026-04-14"},
  "total_cost_usd": 142.50,
  "total_turns": 1847,
  "categories": [
    {
      "name": "coding",
      "turns": 523,
      "cost_usd": 45.20,
      "edit_turns": 489,
      "oneshot_turns": 412,
      "retries": 94
    }
  ],
  "models": [
    {
      "name": "claude-opus-4-6",
      "calls": 890,
      "cost_usd": 98.40,
      "tokens": {
        "input": 12000000,
        "output": 3400000,
        "cache_read": 8900000,
        "cache_create": 1200000
      }
    }
  ],
  "projects": [
    {"name": "SVG-PAINT", "path": "/Users/.../30_SVG-PAINT", "cost_usd": 38.10, "turns": 412}
  ],
  "tools": [
    {"name": "Edit", "calls": 1240},
    {"name": "Bash", "calls": 980}
  ],
  "mcp_servers": [
    {"name": "serena", "calls": 145}
  ],
  "daily": [
    {"date": "2026-04-14", "cost_usd": 22.30, "turns": 287}
  ],
  "scanned_at": "2026-04-14T12:00:00Z"
}
```

### Dashboard Tab 4: "Activity Burn"

Positioned after Token I/O in the tab bar.

**Components:**

1. **Hero card** — Total cost for period (large number), total turns, overall one-shot rate with color coding (>80% green, >60% amber, <60% red)

2. **Daily cost SVG bar chart** (800x250 viewBox) — Bars colored by dominant category per day. X-axis: dates. Y-axis: USD. Same hand-drawn SVG pattern as existing Token I/O chart.

3. **Activity table** — Sorted by cost descending:

| Activity | Turns | Cost | 1-Shot | Retries |
|----------|-------|------|--------|---------|
| coding | 523 | $45.20 | 84% | 94 |
| debugging | 187 | $28.10 | 71% | 42 |
| ... | ... | ... | ... | ... |

One-shot column uses colored text (green/amber/red). Categories without edits show "—" for 1-shot.

### Dashboard Tab 5: "Project & Model"

**Components:**

1. **Project cost bars** — Horizontal SVG bars, project name + cost label. Top 10 projects sorted by cost desc. Project names derived from directory path (last meaningful segment).

2. **Model cost breakdown** — Horizontal SVG bars with model name + cost + call count. Color-coded per model family (Opus=purple, Sonnet=blue, Haiku=green).

3. **Top tools table** — Tool name | call count. Top 10, sorted by calls desc.

4. **MCP servers table** — Server name | call count. Only shown if MCP tools were used.

5. **Utilization signals card** — Informed by the April 2026 skill diet audit (KP-462/463). Shows:
   - Tools/MCP servers that were *invoked* in the period — these justify their listing cost
   - Comparison context: "X tools invoked out of Y listed" — surfaces bloat candidates for A/B/C re-tiering
   - Note: this component tracks *active invocations* from session JSONL. The *passive listing cost* (per-message overhead from system prompt) is a separate concern measured during manual audits. Together they inform the full cost picture.

### Shared Behavior

- Both tabs fetch `/api/codeburn?range=X` on tab switch or range change
- Range selector follows existing dashboard pattern (query param)
- Polling: refresh every 300s (same as history endpoint)
- Empty state: "No session data found" message when no JSONL files match the range

## Dependencies

- **No new Python packages** — stdlib only (urllib for LiteLLM fetch, json, glob, re, threading)
- **No new JS libraries** — hand-drawn SVG, same pattern as existing tabs
- Imports `sessions._SESSIONS_BASE` for the base path constant

## File Changes

| File | Change |
|------|--------|
| `engine/codeburn.py` | New file — parser, classifier, cost calculator, cache |
| `engine/api.py` | Add `/api/codeburn` route + import |
| `engine/dashboard.html` | Add 2 tab buttons + 2 tab panels + JS fetch/render functions |

## Testing

- Manual: run engine, open dashboard, verify all 5 tabs render with real session data
- Verify date range switching works across tabs
- Verify one-shot rate calculation against a known session with retries

## Acceptance Criteria

- [ ] `/api/codeburn?range=7d` returns valid JSON with all breakdown fields
- [ ] Activity Burn tab renders daily cost chart + activity table with one-shot rates
- [ ] Project & Model tab renders project bars + model bars + tool table
- [ ] LiteLLM pricing loads on first request, falls back to hardcoded on failure
- [ ] Cache refreshes every hour, not on every request
- [ ] No new Python dependencies introduced
- [ ] Existing 3 tabs unaffected
