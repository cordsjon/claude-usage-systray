# PE Supervisor + Token Budget in the Systray — Design Spec

**Date:** 2026-07-21
**Status:** Approved design (brainstorm 2026-07-21, operator-approved: approach A, options 2/2/3/1)
**Origin:** Capabilities #3 (web supervisor) + #6 (token budgeting) from the 2026-07-21 capability-gap session — operator directive: fold both into the existing token counter (claude-usage-systray)

## Hierarchy

- **Capability:** Unattended-operation visibility for PosterEngine (capability list #3 + #6)
- **Solution:** Extend claude-usage-systray (Swift menu bar + Python engine :17420) into the PE supervision + spend surface
- **Epic:** `epic:PE-SUPERVISOR` — PE supervisor v1
- **User Stories (proposed):**
  - US-PESUP-ENGINE-01 — engine poller/aggregator/controls + Swift popover section + alerts + budget (claude-usage-systray repo)
  - US-PESUP-PE-01 — `POST /api/jobs/{job_id}/retry` admin endpoint + `com.poster-worker` dev LaunchAgent (20_PosterEngine repo)

## Problem Statement

PosterEngine generations run unattended on two instances (dev `localhost:9120`,
prod `poster.getaccess.cloud`). Failures are silent: a missing/crashed worker
leaves jobs `queued` forever (bit twice — the sweep audit and the 2026-07-21
AC-3 session), dead jobs are only discovered by manually polling `/api/jobs`,
and router spend is only visible by curling the hermes-adapter. There is no
push surface that says "something is wrong at PE" while the operator works on
other projects.

The operator already glances at one surface all day: the claude-usage-systray
menu bar app. Folding PE supervision and token budgeting into it reuses its
exact architecture — Python engine polls and persists, Swift displays and
notifies — instead of building a new dashboard.

## Design Decisions (from brainstorm)

1. **Scope: watch + alert + minimal control** (option 2). Two controls only:
   retry a dead job, kick the worker. No worker lifecycle management beyond
   kick; no job cancel; no enqueue.
2. **Budget: display + soft alert** (option 2). User-set daily/weekly USD
   targets; crossing flips UI red and fires one notification per target per
   day. **No enforcement** — the kill switch / budget rails stay with the
   PE-MCP spec (`2026-07-21-mcp-server-design.md`), which owns enforcement.
3. **Instances: both, parameterized** (option 3). Engine config lists dev +
   prod; every poller/control/alert is instance-tagged. Prod is the value
   driver (real users, real money); dev rides the same code path.
4. **Dev worker gets daemonized as part of this capability** (option 1):
   LaunchAgent `com.poster-worker` mirroring the existing `com.poster-engine`
   server agent. Without it, dev supervision would permanently alarm on a
   known gap, and "kick" would have nothing to kick.
5. **Architecture: engine-centric** (approach A). Secrets (Bearer token, SSH)
   and persistence stay in the engine; Swift remains a thin client, exactly
   like the existing usage path. Swift-direct (B) rejected: credentials in the
   GUI process, no persistence. PE-side supervisor service (C) rejected:
   deploys personal budget preferences into a customer product; enforcement
   home is the MCP spec.

## Architecture

```
                       ┌────────────────────────────┐
                       │  Swift menu bar app         │
                       │  PosterEngineService (new)  │──── UNUserNotification
                       │  popover PE section (new)   │     (stalled/dead/budget/
                       └──────────┬─────────────────┘      unreachable)
                                  │ GET /pe/status
                                  │ POST /pe/<i>/jobs/<id>/retry
                                  │ POST /pe/<i>/worker/kick
                       ┌──────────▼─────────────────┐
                       │  Python engine :17420       │
                       │  engine/posterengine.py     │
                       │  (poller, aggregator,       │
                       │   budget, controls)         │
                       │  SQLite: pe_cost_snapshot   │
                       └───┬──────────┬─────────┬───┘
              /api/jobs    │          │         │  launchctl kickstart (dev)
            (Bearer/inst.) │          │         │  ssh docker restart (prod)
                ┌──────────▼──┐  ┌────▼──────┐  │
                │ PE dev :9120│  │ PE prod   │  │
                │ + LaunchAgent│ │ (VPS)     │◄─┘
                │ com.poster-  │ └───────────┘
                │ worker (new) │   hermes-adapter :9109/status → cost
                └─────────────┘
```

### Engine module (`engine/posterengine.py`)

- **Instance config** (engine config file): `[{name, base_url, token_ref,
  kick_method: "launchctl"|"ssh", ssh_host?, budget: {daily_usd, weekly_usd}}]`.
  `token_ref` names a macOS Keychain item (existing engine Keychain pattern);
  tokens never appear in config plaintext. Windows replication guide gets a
  note: env-var fallback.
- **Poll loop:** per instance, `GET {base_url}/api/jobs?limit=60` every 30 s
  with `Authorization: Bearer <token>`; hermes-adapter `GET :9109/status`
  every 60 s. Failures increment a consecutive-miss counter (unreachable at 3).
- **Persistence:** `pe_cost_snapshot(ts, instance, cost_24h_usd, calls,
  waste_usd)` in the engine's existing SQLite. Spend-today = delta between
  today's first and latest snapshot (adapter's 24 h window is rolling; the
  snapshot delta is the honest day figure).
- **Stall rule:** `stalled = oldest_queued_s > 180 AND running == 0`, computed
  from `/api/jobs` rows only. No separate liveness protocol.
- **Aggregate route** `GET /pe/status`:

```json
{"instances": [{"name": "prod", "reachable": true,
  "counts": {"queued": 0, "running": 1, "complete_24h": 14, "dead": 0},
  "oldest_queued_s": 0, "stalled": false,
  "recent_dead": [{"job_id": "…", "topic": "…", "error": "…", "at": "…"}],
  "cost": {"d24h": 0.0008, "spent_today": 0.0002},
  "budget": {"daily_usd": 0.5, "weekly_usd": 2.0,
              "crossed_daily": false, "crossed_weekly": false},
  "last_poll": "2026-07-21T23:00:00Z"}]}
```

- **Controls:**
  - `POST /pe/<instance>/jobs/<job_id>/retry` → proxies to PE
    `POST /api/jobs/{job_id}/retry` with the instance's Bearer token; relays
    PE's status code.
  - `POST /pe/<instance>/worker/kick` → dev: `launchctl kickstart -k
    gui/<uid>/com.poster-worker`; prod: `ssh <ssh_host> docker restart
    poster-worker` (same SSH identity deploy.sh uses). Rate-limited to one
    kick per 60 s per instance (429 otherwise).
- **Alert dedupe (engine-side, survives Swift restarts):** budget-crossing
  notifications keyed by (instance, target, date); dead-job notifications
  keyed by job_id.

### Swift app

- `PosterEngineService.swift`: polls `/pe/status` on the existing poll cadence,
  decodes into `PEStatus` models.
- Popover: one PE section — per instance a line (`prod ✓ 1 running · $0.12 /
  $0.50`), expandable to recent dead jobs with a **Retry** button; a **Kick
  worker** button appears only when `stalled` or on the expanded view.
- Menu-bar: red tint on the existing icon when any instance is `stalled`,
  `crossed_*`, or unreachable.
- Notifications via the existing `UNUserNotification` helper pattern
  (`HermesClient.notifyError` precedent): stalled, new dead job, budget
  crossed, unreachable (after debounce). Swift raises what the engine flags;
  dedupe lives engine-side.

### PE-side (20_PosterEngine)

- `POST /api/jobs/{job_id}/retry` — same auth gate as `GET /api/jobs`
  (admin/api role, 401 otherwise). Only `status='dead'` is retryable → 409
  for any other status, 404 unknown. Effect: `attempts=0`, `retry_at=NULL`,
  `status='queued'`, job_json mirrored, event appended
  `{"status": "queued", "reason": "manual_retry"}`. Reuses `job_queue.py`
  write conventions (single UPDATE + event, busy_timeout).
- `com.poster-worker` LaunchAgent (dev): `KeepAlive=true`, runs
  `.venv/bin/python -m poster_engine.jobs.worker` with
  `POSTER_OUTPUT_DIR=<repo>/output` (the module default `/app/output` is a
  container path — verified failing locally 2026-07-21) and
  `API_ROUTER_URL=http://127.0.0.1:9111/v1`. Installed by a small script
  mirroring the existing server-agent install; documented in README.

## Acceptance Criteria

- **AC-1 (supervise):** with both instances configured, `/pe/status` reports
  per-instance counts/stall/cost within one poll interval of DB truth;
  killing the dev worker while a job is queued flips `stalled=true` within
  4 minutes and raises exactly one notification.
- **AC-2 (retry):** a `dead` dev job retried from the popover reaches
  `complete` end-to-end (engine → PE endpoint → requeue → LaunchAgent worker),
  with the `manual_retry` event in the job's event stream; retry on a
  non-dead job returns 409 and changes nothing.
- **AC-3 (kick):** kick on dev restarts the LaunchAgent worker (new worker_id
  claims the next job); a second kick within 60 s returns 429; prod kick path
  covered by a unit test with the SSH command faked (no live prod restart in
  CI).
- **AC-4 (budget):** with a daily target set below current spend, the popover
  shows red + `crossed_daily=true` and exactly one notification fires per day
  per target (engine restart does not re-fire).
- **AC-5 (PE tests):** retry endpoint unit tests green (401 non-admin, 404
  unknown, 409 non-dead, dead→queued + event); full PE suite stays green.

## Testing Strategy

- Engine: pytest + `pytest_httpserver` faking PE `/api/jobs` and adapter
  `/status` — stall math, unreachable debounce, budget crossing + dedupe,
  snapshot delta math. The aggregation logic is the seam under test; PE is
  outside it.
- PE: endpoint unit tests against the real dev DB fixture (never mock the
  job_queue seam).
- E2E (dev, manual-triggered script): enqueue → kill worker → observe
  `stalled` → kick → observe recovery → mark job dead → retry → complete.
- Swift: decode + state-mapping tests beside `UsageServiceTests`.

## Out of Scope (v1)

- `batch_job` supervision (generation jobs only)
- Enforcement of budgets / kill switch (PE-MCP spec owns this)
- Job cancel, enqueue, priority controls
- Windows dashboard parity (guide gets a porting note only)
- More than the two configured instances; instance auto-discovery
- UI dropdown or web UI in PE itself

## Premises (verify before implementing)

- `GET /api/jobs` exists, admin-gated, returns per-job status rows —
  verified 2026-07-21 (`poster_engine/web/app.py:1410`).
- hermes-adapter `GET :9109/status` returns `cost_24h_usd` + `costs` block —
  verified 2026-07-21.
- Engine has a Keychain-backed secret pattern and SQLite persistence —
  per WINDOWS-REPLICATION-GUIDE §3/§5.1 (re-verify at implementation).
- `com.poster-engine` LaunchAgent exists as the install template —
  re-verify label/path at implementation.
