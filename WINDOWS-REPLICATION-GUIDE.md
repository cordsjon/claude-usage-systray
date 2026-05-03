# Token Budget Dashboard — Windows Replication Guide

This document explains every piece of the dashboard in enough detail that
someone on Windows (no macOS Keychain, no Swift, no launchd) can rebuild
a functionally equivalent version using only Python 3.11+ and a browser.

---

## Table of Contents

1. [What the Dashboard Is](#1-what-the-dashboard-is)
2. [Architecture Overview](#2-architecture-overview)
3. [The One macOS-Specific Part](#3-the-one-macos-specific-part)
4. [Windows Setup — Step by Step](#4-windows-setup--step-by-step)
5. [Module Deep-Dives](#5-module-deep-dives)
   - 5.1 [db.py — SQLite Persistence Layer](#51-dbpy--sqlite-persistence-layer)
   - 5.2 [poller.py — API Polling Loop](#52-pollerpy--api-polling-loop)
   - 5.3 [stats.py — Pure Projection Math](#53-statspy--pure-projection-math)
   - 5.4 [sessions.py — Raw Token History Scanner](#54-sessionspy--raw-token-history-scanner)
   - 5.5 [codeburn.py — Activity Cost Analyzer](#55-codeburnpy--activity-cost-analyzer)
   - 5.6 [api.py — HTTP Server and JSON Endpoints](#56-apipy--http-server-and-json-endpoints)
6. [Dashboard Screens](#6-dashboard-screens)
   - 6.1 [Tab 1: Runway Horizon](#61-tab-1-runway-horizon)
   - 6.2 [Tab 2: Budget Cards](#62-tab-2-budget-cards)
   - 6.3 [Tab 3: Token I/O](#63-tab-3-token-io)
   - 6.4 [Tab 4: Activity Burn (CodeBurn)](#64-tab-4-activity-burn-codeburn)
7. [Every API Endpoint](#7-every-api-endpoint)
8. [Key Numbers and Thresholds](#8-key-numbers-and-thresholds)
9. [Minimal Windows Replication Checklist](#9-minimal-windows-replication-checklist)

---

## 1. What the Dashboard Is

This is a **self-hosted web dashboard** that tells you how fast you are burning
your Claude.ai plan quota. Claude's web interface already shows this at
`claude.ai/settings/usage`, but this dashboard does three things that page
cannot do:

- **Keeps a history** — every data point is stored in a local SQLite database.
  After a few days, you can see whether you burned more on Monday or Friday,
  whether any cycle ended in a hard stop (100% hit), and what your average peak
  per weekly cycle is.

- **Projects into the future** — using OLS linear regression over the last
  50 data points, it calculates how many hours of headroom you have left before
  you hit 100% utilization. This is the "runway" number.

- **Reads your local Claude Code session files** — the `~/.claude/projects/`
  directory contains every conversation transcript as JSONL files. The dashboard
  reads those files to show you which type of work (feature building, debugging,
  planning, etc.) costs the most tokens, and which AI models you actually used.

The architecture is a small Python HTTP server running on your local machine
on port 17420. You open `http://localhost:17420` in a browser. There is no
cloud component, no sign-in, and no data leaves your machine except the one
API call to Anthropic every minute.

---

## 2. Architecture Overview

```
  Your Browser
  http://localhost:17420
         │
         │ HTTP (stdlib http.server)
         ▼
  ┌─────────────────────────────────────┐
  │         Python Engine               │
  │                                     │
  │  api.py          ← routes requests  │
  │    ├── poller.py ← polls Anthropic  │
  │    ├── db.py     ← SQLite storage   │
  │    ├── stats.py  ← math             │
  │    ├── sessions.py ← JSONL reader   │
  │    └── codeburn.py ← cost analysis  │
  └──────────────────┬──────────────────┘
                     │  every 60 seconds
                     ▼
     https://api.anthropic.com/api/oauth/usage
     (same endpoint powering claude.ai/settings/usage)
```

There is no FastAPI, no external web framework, and no npm. The server is
built entirely from Python's `http.server` standard library. The entire
frontend is one HTML file — `engine/dashboard.html` — which the server reads
from disk and sends to the browser on every request to `/`.

---

## 3. The One macOS-Specific Part

On macOS, the OAuth token needed to call the Anthropic usage API is stored in
the macOS Keychain under the service name `Claude Code-credentials`. The engine
reads it with the `security` CLI tool:

```bash
security find-generic-password -s "Claude Code-credentials" -w
```

This returns a JSON blob. The engine extracts `.claudeAiOauth.accessToken`.

**On Windows, do this instead:**

1. Open `%APPDATA%\Claude\` (or wherever Claude Code stores its config on
   Windows; the exact path may vary by installer version).

2. Find the credentials file. On Windows it is likely a plain JSON file rather
   than a Keychain entry. Look for a file named something like
   `credentials.json` or `claude-credentials.json`.

3. Extract the `accessToken` value from it.

4. Pass it as an environment variable when you start the server:

   ```cmd
   set CLAUDE_OAUTH_TOKEN=sk-ant-oaXXXXXXXXXXXXXXX
   python -m engine
   ```

5. Modify `poller.py` to read from `os.environ["CLAUDE_OAUTH_TOKEN"]` instead
   of calling `subprocess.run(["security", ...])`.

The Keychain read is only used in two places:
- `launcher.sh` — reads the token at startup before spawning Python
- `poller.py`:`_read_keychain_token()` — called automatically when the
  server gets a 401 or 403 from Anthropic, as a self-healing mechanism

Once you replace those two points with a Windows-compatible token source, every
other module works unchanged on Windows.

---

## 4. Windows Setup — Step by Step

### Prerequisites

- Python 3.11 or newer. Check with: `python --version`
- PyYAML: `pip install pyyaml` (only needed for the Habits tab)
- Claude Code installed and logged in (so the session JSONL files exist)

### Step 1 — Clone or copy the engine directory

Copy the entire `engine/` directory to a folder on your Windows machine.
The structure must look like this:

```
my-token-dashboard/
  engine/
    __init__.py
    __main__.py
    api.py
    codeburn.py
    classification.py
    db.py
    eval_label.py
    ingest_prompts.py
    patterns.py
    poller.py
    providers/
    redact.py
    server.py
    sessions.py
    stats.py
    dashboard.html
    data/
```

### Step 2 — Get your OAuth token

Find your Claude Code OAuth token. It is a long string starting with
`sk-ant-oa`. On macOS it lives in the Keychain. On Windows, look in:

```
%APPDATA%\Claude\
%LOCALAPPDATA%\Claude\
```

Search for a file containing `accessToken` or `claudeAiOauth`. The token
refreshes periodically; if you see 401 errors, get a fresh one.

### Step 3 — Patch poller.py for Windows

Open `engine/poller.py` and find the `_read_keychain_token()` function.
Replace its entire body with:

```python
def _read_keychain_token() -> str | None:
    """Read the OAuth token from an environment variable (Windows compatible)."""
    token = os.environ.get("CLAUDE_OAUTH_TOKEN", "")
    return token if token else None
```

### Step 4 — Start the server

```cmd
cd my-token-dashboard
set CLAUDE_OAUTH_TOKEN=sk-ant-oaXXXXXXXXXX
python -m engine
```

Open `http://localhost:17420` in any browser. You should see the dashboard.

### Step 5 — Keep it running (optional)

On Windows, use Task Scheduler to run the server at logon:

1. Open Task Scheduler → Create Basic Task
2. Trigger: At log on
3. Action: Start a program
4. Program: `python.exe`
5. Arguments: `-m engine`
6. Start in: `C:\path\to\my-token-dashboard`
7. In Environment Variables (in the Advanced settings), add:
   `CLAUDE_OAUTH_TOKEN=sk-ant-oaXXXXXXXXXX`

Or use a `.bat` file:

```bat
@echo off
set CLAUDE_OAUTH_TOKEN=sk-ant-oaXXXXXXXXXX
cd /d C:\path\to\my-token-dashboard
python -m engine
```

---

## 5. Module Deep-Dives

### 5.1 db.py — SQLite Persistence Layer

**File:** `engine/db.py`  
**What it does:** Manages the SQLite database file `usage.db` (created
automatically in the current working directory).

#### Database tables

| Table | What it stores |
|-------|---------------|
| `usage_snapshots` | One row per API poll: timestamp, 5hr%, 7day%, Sonnet%, reset times, cycle ID |
| `prompt_usage` | One row per Claude Code user message that matched a known pattern |
| `prompt_unmatched` | One row per user message that did NOT match any pattern |
| `prompt_pattern_eval` | Precision scores for pattern evaluation (Habits tab) |
| `prompt_pattern_eval_labels` | Human labels for evaluating pattern accuracy |
| `ingest_watermark` | How far the engine has read into each JSONL transcript file |

#### Key design decisions

**WAL mode** (`PRAGMA journal_mode=WAL`): SQLite's Write-Ahead Logging mode
allows simultaneous reads while a write is in progress. Without WAL, the
poller thread (which writes every 60 seconds) would block the HTTP server
thread from reading. With WAL, they never block each other.

**`check_same_thread=False`**: Python's sqlite3 module normally throws an
error if you use a connection from multiple threads. The engine deliberately
shares one connection across the poller thread and the HTTP handler threads.
Setting this to False disables the check; the WAL mode ensures the database
itself is safe.

**`cycle_id`**: Derived from the first 10 characters of `seven_day_resets_at`,
which is an ISO date string like `2026-04-30`. This groups all snapshots within
the same weekly billing cycle, making it easy to compute per-cycle peak
utilization and stoppage rates.

**355-day retention** (`RETENTION_DAYS = 355`): Old snapshots are automatically
pruned. This keeps the database small (a few MB even after a year of use).

#### Critical methods

`insert_snapshot(...)` — called every 60 seconds by the poller. Inserts one
row with the current utilization percentages and reset timestamps.

`get_recent_snapshots(limit=50)` — called by the poller to get data for the
burn rate calculation. Returns the 50 most recent rows, newest first.

`get_cycle_peaks()` — returns the maximum utilization reached in each weekly
cycle, plus a `stoppage` flag (1 if the peak five-hour utilization ever
reached 95% or higher). Used by the Budget Cards tab.

`get_weekday_averages(since)` — returns average utilization grouped by day of
week (Sunday through Saturday). Powers the heatmap in the Budget Cards tab.

---

### 5.2 poller.py — API Polling Loop

**File:** `engine/poller.py`  
**What it does:** Runs in a background thread. Every 60 seconds, it calls
the Anthropic API, stores the result, and updates the shared `_current_status`
dict that the HTTP server reads.

#### The Anthropic API endpoint

```
GET https://api.anthropic.com/api/oauth/usage
Authorization: Bearer <oauth_token>
anthropic-beta: oauth-2025-04-20
```

This is the same internal endpoint that powers `claude.ai/settings/usage`.
It returns a JSON object with two top-level keys:

```json
{
  "five_hour": {
    "utilization": 42.7,
    "resets_at": "2026-04-30T15:00:00Z"
  },
  "seven_day": {
    "utilization": 61.3,
    "resets_at": "2026-05-04T00:00:00Z"
  },
  "seven_day_sonnet": {
    "utilization": 38.1
  }
}
```

The engine also handles a legacy flat format where these values appear directly
at the top level without nesting.

#### Burn rate calculation

The poller collects the last 50 snapshots from the database and calculates the
burn rate using OLS (Ordinary Least Squares) linear regression. This is
essentially fitting a straight line to the historical utilization data and
reading the slope.

The formula is:
```
slope = (n × Σ(x·y) - Σx × Σy) / (n × Σx² - (Σx)²)
```
where x is time in hours from the first snapshot, and y is utilization
percentage.

A positive slope means utilization is growing. A negative slope means you
stopped working. The slope in units of `%/hour` is the burn rate.

#### Stale token detection

There is a subtle failure mode: sometimes, when the OAuth token expires,
the Anthropic API returns HTTP 200 with all zeros instead of a 401. This
looks like a successful response but the data is useless.

The engine detects this by counting consecutive "all-zero" responses
(zero utilization AND no reset timestamps). After 3 consecutive all-zero
responses (`ZERO_STREAK_THRESHOLD = 3`), it flags the token as needing
refresh. On macOS, it can automatically re-read the Keychain for a fresh
token. On Windows, you would need to restart the server with a new token.

#### The `_current_status` dict

All polling results are placed into a module-level dict protected by a
`threading.Lock`. The HTTP server reads this dict on every request to
`/api/status`. This is the central data structure the dashboard depends on:

```python
{
  "version": 2,
  "current": {
    "five_hour_util": 42.7,       # percentage, 0-100
    "seven_day_util": 61.3,       # percentage, 0-100
    "sonnet_util": 38.1,          # percentage, 0-100 (or null)
    "five_hour_resets_at": "...", # ISO datetime string
    "five_hour_resets_in": "2h 15m", # human readable
    "seven_day_resets_at": "...",
    "seven_day_resets_in": "3d 4h"
  },
  "projection": {
    "runway_hours": 5.2,           # hours until 100% or reset
    "burn_rate_per_hour": 3.1,     # %/hour burn rate
    "stoppage_likely": false,
    "hours_short": 0.0,
    "projected_util_at_reset": 72.4
  },
  "budget": {
    "daily_avg_this_cycle": 8.7,   # % consumed per day so far
    "recommended_daily": 9.3,      # % per day to stay on pace
    "days_remaining": 3.8,
    "active_hours_per_day": 14,
    "headroom_hours": 21.2,
    "target_at_reset": 98
  },
  "pacing": { ... },    # vs optimal linear ramp
  "benchmarks": { ... }, # historical cycle averages
  "updated_at": "2026-04-30T12:00:00Z"
}
```

---

### 5.3 stats.py — Pure Projection Math

**File:** `engine/stats.py`  
**What it does:** Contains only pure functions — no database access, no I/O,
no global state. All inputs come in as arguments, all outputs are return values.
This makes every function independently testable and easy to port to any language.

#### `burn_rate(timestamps, utils) → float`

Given a list of ISO datetime strings and corresponding utilization percentages,
returns the slope of the best-fit line in units of percent per hour.

Uses the closed-form OLS formula (no dependencies needed):
- Convert each timestamp to hours elapsed since the first timestamp
- Apply the OLS slope formula
- Return the slope (positive = growing utilization)

#### `runway_hours(current_util, burn_rate_per_hour, hours_to_reset) → float`

Given where you are now, how fast you are burning, and how many hours until
the counter resets — returns the number of hours until you either:
- Hit 100% utilization, OR
- The counter resets (whichever comes first)

If burn rate is zero or negative, you have all the time until reset.

Example: 70% utilization, burning at 5%/hour, 8 hours to reset.
- Hours to exhaust: (100 - 70) / 5 = 6 hours
- Hours to reset: 8
- Runway = min(6, 8) = **6 hours**

#### `stoppage_detection(current_util, burn_rate_per_hour, hours_to_reset) → dict`

Projects where utilization will be at the moment the counter resets:
```
projected_util = current_util + (burn_rate × hours_to_reset)
```

If `projected_util > 100`, a stoppage is predicted. It also calculates how many
hours early you would exhaust your budget, which powers the "you will hit the
limit X hours before reset" message in the dashboard.

#### `recommended_daily_budget(current_util, hours_to_reset) → dict`

Calculates how many percentage points per day you should aim to consume in
order to finish the weekly cycle at exactly 98% (not 100%, to leave a small
buffer).

```
remaining_util = 98 - current_util
days_remaining = hours_to_reset / 24
recommended_daily = remaining_util / days_remaining
```

#### `pacing_benchmark(current_util, hours_to_reset, ...) → dict`

Compares your actual utilization against what a perfectly linear ramp would
produce. The ideal strategy is to consume exactly 98% over 7 days at a
constant rate — like burning a candle evenly. At any point in the cycle, the
"optimal" utilization is:

```
optimal = (elapsed_hours / 168 hours) × 98%
```

The delta between actual and optimal determines your pacing grade:
- Within ±3%: **A** (on pace)
- ±3-8%: **B**
- ±8-15%: **C**
- ±15-25%: **D**
- More than ±25%: **F**

Being "ahead" means you burned fast early and risk hitting 100% before reset.
Being "behind" means you are not using your subscription's value.

#### `cycle_benchmarks(cycles) → dict`

Takes historical cycle data (list of peak utilization per cycle) and computes:
- Average peak across all cycles
- Best (highest) peak
- Stoppage rate (what % of cycles hit the limit)
- Wasted capacity (how much % you didn't use on average)
- Overall grade (combines waste penalty and stoppage penalty)

---

### 5.4 sessions.py — Raw Token History Scanner

**File:** `engine/sessions.py`  
**What it does:** Scans all Claude Code session transcript files in
`~/.claude/projects/` and aggregates raw token counts by day.

#### What are session JSONL files?

Every time you have a conversation with Claude Code, the entire conversation
is saved as a JSONL (JSON Lines) file. Each line is one JSON object
representing either a user message or an assistant response.

The path structure is:
```
~/.claude/projects/<encoded-project-path>/<session-uuid>.jsonl
```

The `<encoded-project-path>` part replaces `/` with `-` in the original
filesystem path. For example, a project at `/Users/me/projects/MyApp` would
be encoded as `-Users-me-projects-MyApp`.

#### What data does each line contain?

For assistant messages, the JSON object includes a `usage` field:

```json
{
  "timestamp": "2026-04-30T12:00:00Z",
  "message": {
    "role": "assistant",
    "usage": {
      "input_tokens": 45231,
      "output_tokens": 1847,
      "cache_creation_input_tokens": 8200,
      "cache_read_input_tokens": 39000
    }
  }
}
```

The engine reads every file, sums these four token counts per session, and
groups them by date. This gives you a day-by-day picture of raw token volume.

#### The four token types

- **input_tokens** — tokens in the prompt that were NOT served from cache
  (fresh, billed at full price)
- **output_tokens** — tokens generated by the model (most expensive per token)
- **cache_creation_input_tokens** — tokens written into the prompt cache
  (billed at 1.25× input price once, then cheap to read back)
- **cache_read_input_tokens** — tokens served from the prompt cache
  (billed at 0.1× input price — very cheap)

#### Caching strategy

Scanning hundreds of JSONL files is slow (hundreds of milliseconds to seconds
depending on disk speed). The engine caches the result in memory for 1 hour.
On subsequent requests within that hour, it returns the cached data immediately.
When the cache expires, it starts a background thread to re-scan and serves
the stale cached data in the meantime.

---

### 5.5 codeburn.py — Activity Cost Analyzer

**File:** `engine/codeburn.py`  
**What it does:** Reads the same JSONL session files as sessions.py, but does
a much deeper analysis: it groups messages into "turns" (one user message plus
the assistant responses that follow), classifies each turn by what type of
work was done, and calculates the dollar cost of each turn using live pricing
data from LiteLLM.

#### Turn grouping

A "turn" is one logical exchange: the user says something, Claude responds with
potentially multiple tool calls and assistant messages.

The engine iterates through all messages in chronological order. Every time it
sees a user message, it starts a new turn. Every assistant message that follows
is attached to that turn until the next user message arrives.

#### Category classification — 2-pass heuristic

**Pass 1: Tool pattern matching**

The engine looks at which tools Claude used in the turn and classifies it:

| Tools Used | Category |
|------------|----------|
| `EnterPlanMode` | `planning` |
| `Agent` | `delegation` |
| `Bash` + test keywords (pytest, jest...) | `testing` |
| `Bash` + git keywords (push, commit...) | `git` |
| `Bash` + build keywords (docker, npm build...) | `build/deploy` |
| Any of: `Edit`, `Write`, `NotebookEdit` | `coding` |
| `Bash` + `Read`/`Grep`, no edits | `exploration` |
| `WebSearch`, `WebFetch`, or any MCP tool | `exploration` |
| `Read`/`Grep`/`Glob` only | `exploration` |
| Task tools without edits | `planning` |
| No tools | `no_tools` |

**Pass 2: Keyword refinement**

Some categories get refined by looking at the user's message text:

- `coding` + debug keywords → `debugging`
- `coding` + refactor keywords → `refactoring`
- `coding` + feature keywords → `feature`
- `no_tools` + brainstorm keywords → `brainstorming`
- `no_tools` + research keywords → `exploration`
- `no_tools` + others → `conversation`

#### Cost calculation

For each API call within a turn, the engine looks up the model name (e.g.,
`claude-sonnet-4-6`) in the LiteLLM pricing database (fetched from GitHub
once per day and cached locally). It multiplies token counts by the
per-token price for each token type:

```
cost = (input_tokens × input_price)
     + (output_tokens × output_price)
     + (cache_creation_tokens × cache_write_price)
     + (cache_read_tokens × cache_read_price)
     + (web_search_requests × $0.01)
```

If a turn was done in "fast mode", costs are multiplied by 1.25.

#### One-shot rate

The engine also tracks whether edits were made on the "first try" or required
back-and-forth correction. It counts retry cycles by detecting the pattern:
Edit → Bash → Edit (Claude edited something, ran a test, then edited again
because the test failed). The one-shot rate is the fraction of editing turns
where no retry cycle occurred.

#### Project attribution

Each JSONL file is associated with a project directory. The engine decodes the
path from the encoded directory name and normalizes it through an alias table
(because the same project may appear under several name variants depending on
how Claude Code was invoked). This gives you a per-project cost breakdown.

---

### 5.6 api.py — HTTP Server and JSON Endpoints

**File:** `engine/api.py`  
**What it does:** Defines the HTTP server and all routes. Uses Python's
built-in `http.server.BaseHTTPRequestHandler` — no Flask, no FastAPI.

#### How the server works

The server is created with `HTTPServer(("127.0.0.1", 17420), Handler)`. It
binds only to localhost for security — it is not accessible from other machines
on the network.

The handler class is generated dynamically (via a factory function) so that it
can hold references to the database and token holder without using module-level
globals. This is a standard Python pattern for injecting dependencies into
the old-style `BaseHTTPRequestHandler` API.

#### Dashboard caching

The dashboard HTML file is ~3,900 lines long. Reading it from disk on every
request would be wasteful. Instead, the server caches the file contents in
memory as bytes. It checks the file's modification time (`os.path.getmtime`)
on each request — if the file has changed on disk (e.g., you edited it), it
re-reads it. Otherwise, it serves the cached bytes directly.

---

## 6. Dashboard Screens

All screens share a common navigation bar fixed at the top of the page. The
nav bar has four tab buttons. The active tab gets an underline in cyan color.
A green dot in the top-right corner shows the connection is live. A red dot
means the server is not responding.

### 6.1 Tab 1: Runway Horizon

**Purpose:** See the entire current weekly cycle's burn curve at a glance,
understand whether you are on track or burning too fast, and know exactly how
many hours of budget you have left.

#### What it shows

**HUD (heads-up display) at the top:**

- Giant number on the left: runway in hours (e.g., `5.2h`). This is the
  number of hours until you either exhaust your budget or the weekly counter
  resets, whichever comes first.

- Sub-label: the current 7-day utilization percentage, and how long until
  the weekly counter resets.

- Two stats on the right: burn rate in %/hour, and today's recommended
  daily budget percentage.

**Pacing strip:**

A thin horizontal progress bar below the HUD. Two things are shown on it:
- The fill bar represents your actual utilization (green if safe, amber if
  warning, red if danger).
- A cyan tick mark shows where the "optimal" utilization would be right now
  if you had been consuming at a perfectly linear rate since the start of the
  cycle.

Below the bar: your delta from optimal (e.g., "+4.2% ahead of pace") and
your letter grade (A, B, C, D, or F).

**Main chart:**

An SVG line chart showing the full current cycle's utilization history. The
data comes from `GET /api/history?range=7d`.

Three lines are drawn:
- **Cyan line**: actual 7-day utilization over time, with a light cyan fill
  area beneath it.
- **Green dashed line**: the optimal linear ramp from 0% to 98% over the 7-day
  cycle. Where the actual line is above this, you are ahead. Below it, you
  are behind.
- **Red dashed projection**: extends the current burn rate forward to show
  where utilization is heading. If this line would cross 100% before the cycle
  ends, a stoppage is predicted.

The x-axis shows time labels; the y-axis shows utilization from 0 to 100%.

**Legend:**
- Cyan solid line: actual
- Green dashed line: optimal pace
- Red dashed line: projected

---

### 6.2 Tab 2: Budget Cards

**Purpose:** Detailed numbers and history charts for careful budget management.

#### Gauge row (3 circular gauges)

Three SVG ring gauges arranged horizontally:

- **5hr gauge**: The current 5-hour session utilization. This window resets
  approximately every 5 hours. When you are deeply in a session, this climbs
  quickly.

- **Weekly gauge**: The 7-day rolling utilization. This is the primary budget
  metric.

- **Sonnet gauge**: Sonnet-specific utilization within the weekly window
  (separate from total usage if you are on a plan with distinct Sonnet limits).

Each gauge shows the percentage inside the ring. Color: green below the warning
threshold, amber between warning and critical, red above critical.

Below each gauge: the reset time in human-readable format (e.g., "2h 15m",
"3d 4h").

#### Stoppage warning banner

If the projection module predicts that utilization will exceed 100% before the
cycle resets, a red banner appears above the gauges with the message:

```
⚠ Stoppage likely — projected to hit 100% in X.X h (Y.Y h before reset)
```

#### Burn rate card

Shows:
- Current burn rate: `X.X%/hr`
- Projected utilization at reset: `X%`
- Stoppage warning: yes/no

#### Budget advisor card

Shows the recommended daily budget calculation:
- Your average daily consumption so far this cycle
- The recommended daily maximum to reach 98% at reset (not 100%)
- Days remaining in the cycle
- "Headroom hours" — the difference between total cycle hours and your
  estimated active working hours (14 hours/day assumed active)
- Target at reset: 98%

#### Pacing card (full card version)

Larger version of the pacing strip from Tab 1. Shows:
- The progress track with current vs optimal marker
- Percentages at each end (0% and 98%)
- Current delta (e.g., "+4.2% ahead")

#### 7-day sparkline chart

A small line chart below the budget cards showing the full utilization history
for the current range (7 days, 30 days, or 52 weeks — selectable with toggle
buttons).

Historical cycle peaks are shown as vertical bars at the bottom of the chart,
color-coded by whether the cycle ended in a stoppage (red) or not (green).

#### Weekday heatmap

A 7-cell grid (Sun–Sat) showing average utilization by day of week. Cells are
colored from dark (low average) to bright green (high average). This reveals
patterns like "I always burn heavily on Tuesdays."

#### Benchmarks card

Shows historical statistics across all recorded cycles:
- Average peak utilization
- Best (highest) peak
- Stoppage rate (% of cycles that hit 100%)
- Average wasted capacity (100% minus average peak)
- Overall grade

---

### 6.3 Tab 3: Token I/O

**Purpose:** See the raw token volume (not utilization percentage, but actual
token counts) and how it breaks down by token type and day.

The data here comes from reading the Claude Code session JSONL files directly
(`GET /api/token-history`).

#### Hero stat row (4 cards)

Four large number cards:
- **Total Input Tokens** — fresh input tokens (not from cache)
- **Total Output Tokens** — tokens generated by the model
- **Cache Write** — tokens written into the prompt cache
- **Cache Read** — tokens served from cache (10% of input price)

Below each value: the percentage this token type represents of the total.

#### Daily token chart

A stacked area chart where each day is a vertical column with four colored
segments:
- Blue: input tokens
- Green: output tokens
- Amber: cache write tokens
- Cyan: cache read tokens

This shows at a glance whether a particular day was heavy on fresh context
(tall blue area) or benefiting from cache hits (tall cyan area).

#### Token ratio chart

Shows the input:output ratio over time. This is a proxy for how "efficient"
each conversation was. A high ratio means you sent a lot of context to get
a small response (planning sessions, document analysis). A low ratio means
Claude generated a lot relative to what you sent (code generation, writing).

#### Raw data table

A day-by-day table with columns:
- Date
- Input tokens
- Output tokens
- Cache write tokens
- Cache read tokens
- Number of sessions
- Total tokens for the day

#### Usage profile comparison card

Compares your usage pattern against several reference profiles:
- Light user: mostly short questions, low cache reads
- Power developer: heavy tooling, many cache reads
- Research mode: high input-to-output ratio

This positions your actual usage on a spectrum scale.

---

### 6.4 Tab 4: Activity Burn (CodeBurn)

**Purpose:** Understand where your token budget actually goes by activity type
and project. This is the most detailed tab — it reads and analyzes every
session transcript.

The data comes from `GET /api/codeburn?range=7d` (also supports `30d`, `52w`,
`all`).

#### Range selector

Four buttons at the top: `7d`, `30d`, `52w`, `all`. Clicking changes the
analysis window. The server re-scans and re-groups the session data for the
selected range.

#### Hero stat row (3 cards)

- **Total Cost** — total USD cost across all sessions in the range, computed
  from actual token counts × per-model pricing
- **Total Turns** — number of user→Claude exchanges
- **One-Shot Rate** — percentage of editing turns where Claude got it right
  on the first try (no edit→test→re-edit cycle)

#### Cost by category chart

A horizontal bar chart showing each activity category sorted by cost:
- `feature` — building new things
- `debugging` — fixing things that are broken
- `exploration` — reading code, searching, using web/MCP tools
- `refactoring` — restructuring existing code
- `coding` — generic editing (didn't match feature/debug/refactor)
- `planning` — plan mode, task management
- `testing` — running test suites
- `git` — commit, push, merge operations
- `build/deploy` — building and deploying
- `delegation` — using the Agent tool to spawn subagents
- `brainstorming` — open-ended thinking without tool use
- `conversation` — text-only exchanges
- `general` — unclassified

Each bar shows both the cost in USD and the number of turns in that category.

#### Daily cost chart

A stacked bar chart where each day's bar is segmented by activity category
(each category gets its own color). This shows whether expensive days were
expensive because of debugging, feature work, or something else.

#### Category breakdown table

A sortable table with one row per category:
- Category name
- Total cost
- Number of turns
- One-shot rate (for editing categories)
- Edit efficiency score

#### Model usage table

Shows which Claude models were used and their costs:
- Model name
- API calls count
- Input / output / cache tokens
- USD cost

This reveals if you accidentally used Opus (expensive) when Sonnet (cheaper)
would have sufficed.

#### Project cost table

Shows which project directories consumed the most budget:
- Project name (decoded from the file path)
- Number of turns
- USD cost

#### Tool usage table

Shows which Claude Code tools were invoked most often:
- Tool name (Read, Edit, Bash, Agent, etc.)
- Number of calls

This can reveal inefficiencies, such as reading the same file 40 times per
session when it could be read once.

#### MCP server usage table

If you have MCP (Model Context Protocol) servers configured, shows how often
each was called:
- MCP server name
- Number of tool calls

#### Weekly efficiency table

Aggregates daily data into ISO calendar weeks:
- Week number
- Total tokens
- Tool calls
- Average tokens per tool call (lower = more efficient)
- USD cost

#### Context overhead card

Shows an estimate of how many tokens go to "boilerplate" that Claude Code
injects on every turn but that doesn't appear in the session JSONL files:
- Total input / output tokens
- Cache hit rate (higher = better — means less re-reading the same context)
- Input:output ratio
- Estimated overhead per turn (from CLAUDE.md size + skill catalog + MCP
  instructions + memory index)
- Estimated total overhead across all turns

#### Tenet citations card (if available)

If Claude referenced any architectural tenets (written as `[TENET: name]`
in responses), they are listed here with counts. This is a project-specific
feature tied to a custom CLAUDE.md system.

---

## 7. Every API Endpoint

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Serves `dashboard.html` with `no-cache` headers |
| GET | `/api/health` | Server uptime and token freshness status |
| GET | `/api/status` | Current utilization, projections, budget — the main data feed |
| GET | `/api/history?range=7d\|30d\|52w\|all` | Historical snapshots, cycle peaks, weekday averages |
| GET | `/api/token-history` | Daily token counts from session JSONL files |
| GET | `/api/codeburn?range=7d\|30d\|52w\|all` | Full CodeBurn activity analysis |
| GET | `/api/prompts` | Ranked prompt patterns for the Habits tab |
| GET | `/api/prompts/unmatched?limit=10&days=7` | Unmatched user messages for pattern discovery |
| GET | `/api/overview?range=7d\|...` | Multi-provider budget snapshot |
| POST | `/api/token` | `{"token": "sk-ant-oa..."}` — hot-swap the OAuth token |
| POST | `/api/prompts/classify` | Move a pattern between everyday/case-by-case sections |
| POST | `/api/prompts/dry-run` | `{"regex": "..."}` — test a regex against recent unmatched messages |
| POST | `/api/prompts/pattern` | Append a new pattern to the YAML file |

All endpoints return JSON. All GET endpoints include `Access-Control-Allow-Origin: *`.

---

## 8. Key Numbers and Thresholds

| Constant | Value | Where | Meaning |
|----------|-------|-------|---------|
| `POLL_INTERVAL` | 60 seconds | poller.py | How often to call the Anthropic API |
| `BACKOFF_INTERVAL` | 900 seconds | poller.py | Wait time after a failed poll |
| `ZERO_STREAK_THRESHOLD` | 3 | poller.py | Consecutive all-zero responses before token refresh |
| `RETENTION_DAYS` | 355 | db.py | Days of history to keep in SQLite |
| `_CACHE_TTL` (sessions) | 3600 seconds | sessions.py | How long to cache the token history scan |
| `_CACHE_TTL` (codeburn) | 3600 seconds | codeburn.py | How long to cache the codeburn report |
| `_PRICING_TTL` | 86400 seconds | codeburn.py | How long to cache LiteLLM pricing data |
| `active_hours_per_day` | 14 | stats.py | Assumed active working hours per day for budget math |
| `target` | 98% | stats.py | Target utilization at cycle reset (not 100%, for buffer) |
| Port | 17420 | api.py | Local HTTP server port |
| Pacing grade A | < 3% delta | stats.py | Within 3% of optimal = A grade |
| Pacing grade F | > 25% delta | stats.py | More than 25% off optimal = F grade |
| Sonnet pricing (fallback) | $3.00/$15.00 per 1M tokens | codeburn.py | Input/output price when LiteLLM lookup fails |
| Opus pricing (fallback) | $15.00/$75.00 per 1M tokens | codeburn.py | Input/output price when LiteLLM lookup fails |
| Fast mode multiplier | 1.25× | codeburn.py | Cost premium when `usage.speed == "fast"` |
| Cache write premium | 1.25× input | codeburn.py | Cache creation costs 25% more than fresh input |
| Cache read discount | 0.1× input | codeburn.py | Cache reads cost 10% of fresh input |

---

## 9. Minimal Windows Replication Checklist

If you want just the core dashboard without the Habits tab (which requires
PyYAML and the prompt ingest pipeline), here is the minimum you need:

- [ ] Python 3.11+
- [ ] Copy: `engine/__init__.py`, `engine/__main__.py`, `engine/server.py`,
      `engine/api.py`, `engine/poller.py`, `engine/db.py`, `engine/stats.py`,
      `engine/sessions.py`, `engine/codeburn.py`, `engine/dashboard.html`
- [ ] Create empty: `engine/providers/__init__.py` with a stub `get_overview()`
      function that returns `{}`
- [ ] Patch `poller.py:_read_keychain_token()` to read from
      `os.environ["CLAUDE_OAUTH_TOKEN"]`
- [ ] Set the environment variable and run: `python -m engine`
- [ ] Open `http://localhost:17420`

The Habits tab (`/api/prompts`, `/api/prompts/unmatched`) requires the
`ingest_prompts.py` pipeline, `patterns.py`, `classification.py`, `redact.py`,
and PyYAML. That pipeline reads your Claude Code session files to extract
and classify the questions you ask most often. It is useful but not required
for the core budget tracking dashboard.
