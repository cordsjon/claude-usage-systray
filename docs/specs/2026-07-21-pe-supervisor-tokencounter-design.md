# PE Supervisor + Token Budget in the Systray — Design Spec

**Date:** 2026-07-21 (v3 — post Codex pre-panel rounds 1+2)
**Status:** Approved design (brainstorm 2026-07-21, operator-approved: approach A, options 2/2/3/1)
**Origin:** Capabilities #3 (web supervisor) + #6 (token budgeting) from the 2026-07-21 capability-gap session — operator directive: fold both into the existing token counter (claude-usage-systray)

## Hierarchy

- **Capability:** Unattended-operation visibility for PosterEngine (capability list #3 + #6)
- **Solution:** Extend claude-usage-systray (Swift menu bar + Python engine :17420) into the PE supervision + spend surface
- **Epic:** `epic:PE-SUPERVISOR` — PE supervisor v1
- **User Stories (proposed):**
  - US-PESUP-ENGINE-01 — engine poller/aggregator/controls + Swift popover section + alerts + budget (claude-usage-systray repo)
  - US-PESUP-PE-01 — PE-side API additions (`/api/jobs/summary`, `/api/jobs/{id}/retry`, `/api/admin/router-metrics`) + `com.poster-worker` dev LaunchAgent template (20_PosterEngine repo)

## Problem Statement

PosterEngine generations run unattended on two instances (dev `localhost:9120`,
prod `poster.getaccess.cloud`). Failures are quiet: when no worker is alive,
the web process's orphan-reclaim loop converts queued jobs to terminal
`failed` only after 1,800 s (`reclaim_orphaned_jobs`,
`poster_engine/db/job_queue.py`) — a 30-minute blind window, after which the
job fails silently with "no worker was available". Dead/failed jobs are only
discovered by manually polling `/api/jobs`, and router spend is only visible
by curling per-host telemetry. There is no push surface that says "something
is wrong at PE" while the operator works on other projects.

The operator already glances at one surface all day: the claude-usage-systray
menu bar app. Folding PE supervision and token budgeting into it follows its
existing division of labor — Python engine polls and persists, Swift displays
and notifies — instead of building a new dashboard.

## Design Decisions (from brainstorm + grounding round 1)

1. **Scope: watch + alert + minimal control** (option 2). Two controls only:
   retry a dead/failed job, kick the worker. No worker lifecycle management
   beyond kick; no job cancel; no enqueue.
2. **Budget: display + soft alert, rolling-24h target** (option 2, revised).
   User-set **rolling-24h** USD target per instance — this matches the only
   window the router's `/metrics` truthfully serves (its `window` param maps
   everything except `1h` to 24 h). Calendar-day and weekly budgets are out of
   scope v1 (would require new router telemetry). Crossing flips UI red and
   fires an edge-triggered notification. **No enforcement** — kill switch /
   budget rails stay with the PE-MCP spec (`2026-07-21-mcp-server-design.md`).
3. **Instances: both, parameterized** (option 3). Engine config lists dev +
   prod; every poller/control/alert is instance-tagged. Per-instance spend
   attribution is structural: each host runs its **own** api-router on
   `127.0.0.1:9111` (verified: VPS router `/health` responds; Mini router
   local), and PE tags calls `X-Consumer-ID: posterengine`
   (`poster_engine/llm/router_backend.py`), so each router's `call_log`
   contains exactly that instance's PE spend.
4. **Dev worker gets daemonized as part of this capability** (option 1):
   LaunchAgent `com.poster-worker` following the existing
   `scripts/com.poster-engine.plist.template` + manual-bootstrap convention
   (PE ships plist templates with documented `launchctl bootstrap` steps, not
   an installer). Without it, dev supervision would permanently alarm on a
   known gap, and "kick" would have nothing to kick.
5. **Architecture: engine-centric** (approach A). PE Bearer tokens and the
   SSH kick path live only in the engine; Swift remains a display/notify
   client. (Note: this is a design choice for the *new* path, not a claim
   about the existing usage path — `UsageService` currently reads the
   Keychain itself for the Claude OAuth token; that stays untouched.)
   Swift-direct (B) rejected: credentials in the GUI process, no persistence.
   PE-side supervisor service (C) rejected: deploys personal budget
   preferences into a customer product; enforcement home is the MCP spec.
6. **Uniform transport (grounding round 1):** the engine reaches *everything*
   per instance through the PE web API with one Bearer token — jobs summary
   AND cost (PE proxies its host-local router metrics). SSH appears only in
   the prod kick path, never in the poll loop.

## Architecture

```
                       ┌────────────────────────────┐
                       │  Swift menu bar app         │
                       │  PosterEngineService (new)  │──── UNUserNotification
                       │  popover PE section (new)   │     via shared Notifier
                       └──────────┬─────────────────┘     (extracted helper)
                                  │ GET /pe/status
                                  │ POST /pe/<i>/jobs/<id>/retry
                                  │ POST /pe/<i>/worker/kick
                       ┌──────────▼─────────────────┐
                       │  Python engine :17420       │
                       │  engine/posterengine.py     │
                       │  (poller thread, aggregator,│
                       │   budget, async controls)   │
                       │  SQLite: pe_cost_snapshot,  │
                       │          pe_alert_state     │
                       └───┬──────────────┬─────────┘
        Bearer, per inst.: │              │ kick only:
        /api/jobs/summary  │              │ launchctl kickstart (dev)
        /api/admin/        │              │ ssh root@VPS docker restart (prod)
        router-metrics     │              │ (background thread, bounded timeouts)
                ┌──────────▼──┐  ┌────────▼──┐
                │ PE dev :9120│  │ PE prod    │
                │ + LaunchAgent│ │ (VPS)      │
                │ com.poster-  │ │ poster-    │
                │ worker (new) │ │ worker ctr │
                └──────┬──────┘  └─────┬─────┘
                       │ proxies       │ proxies
                ┌──────▼──────┐  ┌─────▼─────┐
                │ Mini router  │  │ VPS router │   (each host-local :9111,
                │ call_log     │  │ call_log   │    consumer=posterengine)
                └─────────────┘  └───────────┘
```

### PE-side (20_PosterEngine — US-PESUP-PE-01)

All three endpoints reuse the existing `GET /api/jobs` auth gate (admin/api
role, 401 otherwise; `poster_engine/web/app.py:1422-1424`).

- **`GET /api/jobs/summary`** — DB-truth aggregation the poller consumes
  (the existing `/api/jobs` returns only the newest ≤300 rows inside a
  `db.recent` envelope and omits topic/error unless `full=true`, so row
  counting cannot give truth; grounding round 1 finding 7):
  `SELECT status, COUNT(*) ... GROUP BY status` plus `complete_24h`
  (updated_at-bounded), **`oldest_claimable_queued_s`** — age of the oldest
  queued job that a worker could claim *now*, i.e. filtered by
  `retry_at IS NULL OR retry_at <= datetime('now')` (jobs inside retry
  backoff deliberately sit in `queued` with a future `retry_at`,
  `job_queue.py:170-180`, and must not trip the stall rule) — and
  `recent_terminal`: the newest ≤10 `dead`/`failed` jobs as
  `{job_id, status, topic, error, updated_at}` with topic/error extracted
  server-side from `job_json`. Total queued count still includes backoff
  rows. Single read-only connection, `busy_timeout`, same conventions as
  `/api/jobs`.
- **`POST /api/jobs/{job_id}/retry`** — retryable statuses: **`dead` and
  `failed`** (orphan-reclaimed "no worker was available" jobs are `failed`
  and are the prime retry case; grounding round 1 finding 6). 409 for any
  other status, 404 unknown. Effect: `attempts=0`, `retry_at=NULL`,
  `status='queued'`, `job_json` mirrored, event appended
  `{"status": "queued", "reason": "manual_retry"}`. Reuses `job_queue.py`
  write conventions (single UPDATE + event, busy_timeout, WAL).
- **`GET /api/admin/router-metrics`** — queries host-local
  `http://127.0.0.1:9111/metrics?consumer=posterengine&window=24h` (httpx,
  3 s timeout) and **normalizes** — it is not a raw proxy. Response contract:
  `{"available": true, "cost_24h_usd": <float>, "calls": <int>}`, where cost
  and calls are summed **only over the provider blocks of
  `consumers.posterengine`** (today DeepSeek-only per `routing.yaml`; a
  provider block with no calls is `{"calls": 0}` and contributes 0). The
  router's top-level `totals` block is host-wide (no `consumer_id` predicate,
  `api_router/main.py:190-216`) and MUST NOT be read — pinning this here
  closes the attribution bug class for good. Router unreachable → 200 with
  `{"available": false}` — cost unavailable is never reported as zero. Unit
  test seeds a fake router response containing another consumer's paid call
  and asserts it is excluded. Works identically on both hosts because both
  run a local router and PE runs host-network.
- **`com.poster-worker` LaunchAgent template** —
  `scripts/com.poster-worker.plist.template`: `KeepAlive=true`,
  `.venv/bin/python -m poster_engine.jobs.worker`, env
  `POSTER_OUTPUT_DIR=<repo>/output` (module default `/app/output` is a
  container path — verified failing locally 2026-07-21) and
  `API_ROUTER_URL=http://127.0.0.1:9111/v1`. Bootstrap instructions live in
  the template's own header comment, exactly matching the existing
  `com.poster-engine.plist.template` convention (there is no scripts README
  to append to; the template header IS the documentation).

### Engine module (`engine/posterengine.py` — US-PESUP-ENGINE-01)

- **Instance config — new code, no existing loader** (grounding round 1
  finding 1): JSON file `~/.local/share/token-budget/pe_instances.json`:
  `[{name, base_url, token_ref, kick_method: "launchctl"|"ssh", ssh_host?,
  budget_24h_usd}]`. `token_ref` names a Keychain service resolved via the
  existing generic `keychain_get` (`engine/providers/__init__.py`); tokens
  never appear in config plaintext. Windows guide gets an env-var-fallback
  porting note.
- **Poll loop:** one daemon thread (registered next to the existing poller
  threads in `engine/server.py`): per instance every 30 s
  `GET /api/jobs/summary`, every 60 s `GET /api/admin/router-metrics`, both
  `Authorization: Bearer <token>`, bounded timeouts (5 s). Consecutive-miss
  counter → `reachable=false` at 3 misses.
- **Persistence (engine SQLite, existing `UsageDB` database file):**
  `pe_cost_snapshot(ts, instance, cost_24h_usd, calls, available)` — history
  for the popover sparkline and post-hoc inspection; **the budget signal is
  the router's own rolling-24h figure, not a snapshot delta** (rolling-window
  deltas can go negative and are not day-spend; grounding round 1 finding 9).
  `pe_alert_state(alert_id, first_seen, last_seen, active)` — alert lifecycle.
  `pe_op_log(op_id, instance, kind, target, state, detail, ts)` — control
  operation results (see Controls).
- **Stall rule:** `stalled = oldest_claimable_queued_s > 180 AND running == 0`
  from the summary endpoint — backoff-parked jobs never trip it. (Reclaim
  converts stuck jobs to `failed` at 1,800 s; the supervisor exists to cut
  that 30-minute blind window to 4 minutes.)
- **Budget rule:** edge-triggered with hysteresis — alert activates when
  `cost_24h_usd ≥ budget_24h_usd`, re-arms when it falls below 90 % of
  target. `available=false` never triggers (unavailable ≠ zero ≠ over).
- **Aggregate route `GET /pe/status`** (added to the hand-rolled
  `BaseHTTPRequestHandler` dispatch in `engine/api.py` — explicit path
  parsing for instance/job segments; no framework):

```json
{"instances": [{"name": "prod", "reachable": true,
  "counts": {"queued": 0, "running": 1, "complete_24h": 14,
              "dead": 0, "failed": 2},
  "oldest_claimable_queued_s": 0, "stalled": false,
  "recent_terminal": [{"job_id": "…", "status": "failed", "topic": "…",
                        "error": "…", "updated_at": "…"}],
  "cost": {"d24h_usd": 0.0008, "calls": 12, "available": true},
  "budget": {"target_24h_usd": 0.5, "crossed": false},
  "last_poll": "2026-07-21T23:00:00Z"}],
 "alerts": [{"id": "stalled:dev:2026-07-21T22:58:00Z", "instance": "dev",
             "kind": "stalled", "message": "dev: 2 queued, no worker",
             "active": true}],
 "ops": [{"op_id": "…", "instance": "dev", "kind": "retry",
          "target": "1b4d1c31", "state": "ok", "detail": null, "ts": "…"}]}
```

- **Alert contract** (grounding round 1 finding 11): every alert carries a
  **stable id** (`kind:instance:first-seen-ts`; dead/failed jobs use
  `dead:instance:job_id`). The engine keeps ids stable across polls in
  `pe_alert_state`; **Swift persists seen ids in `UserDefaults`** and
  notifies once per unseen id. "Exactly once" is therefore Swift-side dedupe
  on engine-stable ids; engine restarts don't re-mint ids for still-active
  conditions (rehydrated from `pe_alert_state`).
- **Controls (async with observable results — the engine's HTTP server is a
  single-threaded `HTTPServer`; grounding rounds 1+2):** control routes run
  a **synchronous preflight** against engine state (unknown instance → 404;
  retry target not present in cached `recent_terminal` → 404; kick
  rate-limited → 429), then record an operation
  `{op_id, instance, kind, target, state: "pending", detail: null, ts}` in
  `pe_op_log` (engine SQLite), spawn a daemon thread, and return
  `202 {"accepted": true, "op_id": …}`. The thread updates the record to
  `ok` or `failed` with `detail` (PE's 404/409 body, HTTP status, or
  `timeout`). `/pe/status` carries the last 10 ops as `ops: [...]`; a
  `failed` op mints an alert (`op_failed:<instance>:<op_id>`) so the outcome
  reaches the UI through the normal notification path. Definite PE-side
  4xx semantics (404 unknown / 409 non-retryable) are asserted directly
  against the PE endpoint in PE's own tests; the engine relays them as op
  results, not as its own HTTP status.
  - retry: thread POSTs the instance's `/api/jobs/{id}/retry` (5 s timeout);
    success also surfaces on the next poll (job leaves `recent_terminal`).
  - kick: dev `launchctl kickstart -k gui/<uid>/com.poster-worker`; prod
    `ssh <ssh_host> docker restart poster-worker` with
    `-o ConnectTimeout=5` + 30 s subprocess timeout (same SSH identity
    deploy.sh uses, `root@72.61.159.117`). Rate-limited to one kick per 60 s
    per instance (429 at preflight).

### Swift app

- `PosterEngineService.swift`: **copies the `UsageService` pattern** — its own
  one-shot 60 s `Timer` against `http://localhost:17420/pe/status`,
  rescheduled per result, started from `AppDelegate` (there is no central
  poll coordinator to attach to; grounding round 1 finding 3).
- **Shared `Notifier` helper extracted** (grounding rounds 1+2): the
  existing private helpers in `HermesClient` and `AppDelegate` both wrap
  `UNUserNotification` with hard-coded titles, and `AppDelegate` uses the
  `.defaultCritical` sound for critical alerts — so the shared helper is
  `Notifier.post(title:body:critical: Bool = false)` (critical maps to
  `.defaultCritical`, preserving current behavior). Both call sites migrate;
  permission is requested exactly once (today it is requested from two
  places).
- Popover: one PE section — per instance a line
  (`prod ✓ 1 running · $0.12 / $0.50`), expandable to `recent_terminal` with
  a **Retry** button; **Kick worker** appears when `stalled` or expanded.
- Menu-bar: red tint on the existing icon while any alert is `active`.

## Acceptance Criteria

- **AC-1 (supervise):** with both instances configured, `/pe/status` matches
  a direct `/api/jobs/summary` read within one poll interval; killing the dev
  worker with a job queued flips `stalled=true` within 4 minutes and raises
  exactly one notification (id-deduped across engine and Swift restarts).
- **AC-2 (retry):** a `failed` (orphan-reclaimed) dev job retried from the
  popover reaches `complete` end-to-end (engine 202 → PE requeue →
  LaunchAgent worker), with the `manual_retry` event in its event stream;
  retry on a `complete` job returns 409 and changes nothing — the 409 leg is
  asserted **directly against the PE endpoint** (PE tests); the engine leg
  asserts the failed op appears in `/pe/status` `ops` with the relayed
  detail.
- **AC-3 (kick):** kick on dev restarts the LaunchAgent worker (new
  `worker_id` claims the next job); a second kick within 60 s returns 429;
  prod kick unit-tested with the SSH subprocess faked (no live prod restart
  in tests); a hung SSH cannot block `/pe/status` (async control thread).
- **AC-4 (budget):** with `budget_24h_usd` set below current rolling-24h
  spend, the popover shows red + `crossed=true` and exactly one notification
  fires per crossing edge (re-arm below 90 %); `available=false` never
  triggers the budget alert.
- **AC-5 (PE tests):** unit tests green for all three new endpoints
  (401 non-admin; summary counts vs seeded DB; 404/409/dead→queued+event for
  retry; router-metrics proxy with fake router incl. unavailable path); full
  PE suite stays green.

## Testing Strategy

- Engine: pytest + `pytest_httpserver` faking PE `/api/jobs/summary` and
  `/api/admin/router-metrics` — stall math, unreachable debounce, budget
  edge+hysteresis, alert-id stability across simulated restarts, 202-async
  control dispatch. The aggregation logic is the seam under test; PE is
  outside it.
- PE: endpoint tests against the real seeded dev-DB fixture (never mock the
  job_queue seam); router-metrics tests fake only the router HTTP (outside
  the seam).
- E2E (dev, manual script): enqueue → kill worker → observe `stalled` →
  kick → recovery → orphan-reclaim a job to `failed` → retry → complete.
- Swift: decode + state-mapping + seen-id dedupe tests beside
  `UsageServiceTests`.

## Out of Scope (v1)

- Calendar-day / weekly budgets (router serves 1h/24h windows only; needs
  router-side telemetry work first)
- `batch_job` supervision (generation jobs only)
- Enforcement of budgets / kill switch (PE-MCP spec owns this)
- Job cancel, enqueue, priority controls
- Windows dashboard parity (guide gets a porting note only)
- More than the two configured instances; instance auto-discovery
- Engine HTTP server threading rework (controls go async instead)

## Premises (verify before implementing)

- `GET /api/jobs` admin gate at `poster_engine/web/app.py:1422-1424`; response
  is an envelope with `db.recent` — verified 2026-07-21.
- Engine Keychain reader `keychain_get` in `engine/providers/__init__.py`;
  `UsageDB` SQLite at `~/.local/share/token-budget/token_budget.db` via
  `engine/server.py` — verified 2026-07-21 (Codex round 1).
- Engine HTTP layer is a hand-rolled single-threaded `HTTPServer` dispatch in
  `engine/api.py` — verified 2026-07-21 (Codex round 1).
- Both hosts run an api-router on `127.0.0.1:9111`; VPS router `/health`
  verified live 2026-07-21; PE sends `X-Consumer-ID: posterengine`
  (`poster_engine/llm/router_backend.py`); router `/metrics` accepts
  `consumer` + maps windows to 1h/24h (`api_router/main.py:178-206`);
  `call_log.consumer_id` indexed (`api_router/db.py`).
- `reclaim_orphaned_jobs`: queued > 1,800 s → `failed`
  (`poster_engine/db/job_queue.py:202-259`) — verified 2026-07-21.
- Prod worker container `poster-worker`, SSH identity `root@72.61.159.117`
  (deploy.sh) — verified 2026-07-21.
- `com.poster-engine` plist template + manual bootstrap convention in PE
  `scripts/` — verified 2026-07-21 (Codex round 1).

## Codex review — pre-panel (round 2)

Grounded against `claude-usage-systray@5153767`,
`20_PosterEngine@2809132`, and `api-router@d4dfb70` on 2026-07-21.

### Findings

1. **[P1] The router proxy still does not define which response field is PE
   spend.** `GET /metrics?consumer=posterengine&window=24h` scopes only
   `consumers.posterengine.{groq,deepseek}`
   (`api_router/main.py:178-188`, with the consumer predicate in
   `api_router/metrics.py:53-77`). Its top-level `totals` query has no
   `consumer_id` predicate and therefore remains host-wide
   (`api_router/main.py:190-216`). The current `posterengine` route is
   DeepSeek-only (`api_router/routing.yaml:21-29`), so today's correct values
   are `consumers.posterengine.deepseek.cost_usd` and `.calls`; when there are
   no calls the provider block is only `{"calls": 0}`
   (`api_router/metrics.py:82-83`). The PE endpoint is described merely as a
   proxy, and neither its response contract nor the engine parser pins this
   path. That leaves implementation free to consume the misleading top-level
   total and recreates the attribution bug this revision is meant to close.
   Specify a normalized PE response such as
   `{"available": true, "cost_24h_usd": 0.0, "calls": 0}` sourced only from
   the consumer block, and add a test with another consumer's paid call to
   prove it is excluded. The per-host premise itself is grounded: PE sends
   `X-Consumer-ID: posterengine` (`poster_engine/llm/router_backend.py:24-38,75-78`)
   and prod PE uses host networking with router URL `127.0.0.1:9111`
   (`docker-compose.yml:2-17,51-65`). Each host's `call_log` contains that
   instance's PE rows, separable by `consumer_id`; it does not contain only PE
   spend.

2. **[P1] Async `202` fixes server blocking but drops the control-result
   contract.** The engine is indeed a single-threaded `HTTPServer`
   (`engine/api.py:13,481-500`; `engine/server.py:157-163`), so moving the 5 s
   PE request and up-to-30 s SSH command off the request thread is correct.
   However, the specified retry route returns `202` before PE can return its
   required 404/409, and the only stated success signal is that the job later
   leaves `recent_terminal`. A failed asynchronous retry changes nothing, so
   neither `/pe/status` nor the Swift caller receives a definite failure.
   This also makes AC-2's “retry on a complete job returns 409” ambiguous: the
   PE endpoint can return 409, but the engine control endpoint cannot relay it
   under the stated contract. Define cached preflight semantics plus an
   operation/result record surfaced by `/pe/status` (or otherwise specify how
   async 404/409/timeout failures reach the UI), and state explicitly which
   endpoint AC-2 exercises.

3. **[P2] `oldest_queued_s` must exclude jobs still inside retry backoff for
   the stall rule.** PE deliberately leaves retrying jobs in `queued` with a
   future `retry_at` (`poster_engine/db/job_queue.py:170-180`) and the worker
   cannot claim them until that timestamp (`poster_engine/db/job_queue.py:313-318`).
   Counting such a row as the oldest actionable queue item can report
   `stalled=true` while a healthy worker is correctly waiting. Define the
   summary field used by the stall rule as the oldest *claimable* queued job,
   with `retry_at IS NULL OR retry_at <= datetime('now')`; total queued counts
   may still include backoff rows.

4. **[P2] The proposed shared notifier signature loses existing critical-sound
   behavior.** The extraction premise is correct: `HermesClient` has a private
   notification helper and permission request
   (`HermesClient.swift:30-47`), while `AppDelegate` separately requests
   permission and posts notifications (`AppDelegate.swift:93-99,222-239`).
   But `AppDelegate` currently selects `.defaultCritical` for critical alerts
   (`AppDelegate.swift:222-226`), which `Notifier.post(title:body:)` cannot
   represent. Give the shared helper a sound/severity argument (with a normal
   default), then migrate both callers and retain a single permission request.

5. **[P3] The LaunchAgent convention is grounded, but the named documentation
   target is not.** `scripts/com.poster-engine.plist.template:4-12` is exactly
   a template with manual copy/bootstrap instructions, confirming the revised
   convention. There is no README or other documentation file in `scripts/`
   to which instructions can be “appended.” Say that the new template's
   header will contain its bootstrap instructions, matching the existing
   template, or explicitly create `scripts/README.md`.

### Resolution re-check

| Round-1 issue | Round-2 status | Grounding result |
|---|---|---|
| Hermes `/status` spend attribution | **Partial** | Per-host router transport is correct, but the consumer-scoped response field is not pinned; finding 1 remains blocking. |
| Rolling-snapshot delta budget math | **Resolved, contingent on finding 1** | Router maps `1h` to one hour and `24h` to 24 hours (`api_router/main.py:178-180`); using the absolute rolling-24h consumer cost with edge/hysteresis is coherent. |
| `/api/jobs` row counting | **Resolved for aggregation** | Existing rows are capped at 300 and nested under `db.recent` (`poster_engine/web/app.py:1410-1479`); a DB aggregation endpoint removes that truncation. Apply finding 3 to its stall-age field. |
| Queued-forever premise | **Resolved** | The web cleanup loop runs every 60 s and invokes orphan reclaim (`poster_engine/web/app.py:367-385`); queued jobs older than the default 1,800 s become `failed` with the stated message (`poster_engine/db/job_queue.py:202-259`). |
| Retry only accepted `dead` | **Resolved** | `dead` is the exhausted-retry terminal state (`job_queue.py:142-199`) and orphan reclaim produces `failed`; accepting both matches the repository state machine. |
| Private notification helper | **Partial** | Shared extraction and one permission request are the right correction, subject to preserving severity/sound per finding 4. |
| Blocking controls on single-threaded server | **Partial** | Immediate `202` removes the blocking defect, but asynchronous failure/result visibility is unspecified; finding 2 remains blocking. |
| LaunchAgent installer convention | **Resolved with documentation correction** | The repo uses a plist template with inline manual-bootstrap instructions, not an installer; fix the nonexistent scripts-doc target per finding 5. |
| Existing instance-config loader premise | **Resolved** | No PE instance loader exists in the systray repo; explicitly naming `pe_instances.json` and its loader as new code is accurate. The generic Keychain service lookup exists at `engine/providers/__init__.py:67-77`. |

The other rewritten grounding points also match source: `UsageService` owns a
one-shot, result-rescheduled 60 s timer (`UsageService.swift:98-100,126-141,
143-205`), and the proposed stable-alert-id/UserDefaults design no longer
claims an existing notification coordinator or engine-only exactly-once
behavior.

**Round-2 verdict: FAIL.** Findings 1 and 2 leave core spend correctness and
control failure observability ambiguous. Resolve those before panel review;
findings 3-5 should be folded into the same edit.

## Codex review — pre-panel (round 3)

Grounded against `claude-usage-systray@5153767`,
`20_PosterEngine@2809132`, and `api-router@d4dfb70` on 2026-07-21.

### Findings

1. **[P2] The claimable-queue rename is not carried through the aggregate
   response contract.** The PE summary definition and engine stall formula
   now correctly use `oldest_claimable_queued_s` with
   `retry_at IS NULL OR retry_at <= datetime('now')`
   (design lines 117-121 and 180-181), exactly matching the worker's claim
   predicate (`poster_engine/db/job_queue.py:313-318`). However, the
   normative `/pe/status` JSON example still publishes
   `"oldest_queued_s"` (design line 195), with no statement that it is an
   alias carrying the claimable-only value. Since Swift consumes
   `/pe/status`, this leaves the downstream decode contract and semantics
   ambiguous. Use `oldest_claimable_queued_s` end-to-end, or explicitly
   define the aggregate alias and require its value to come from the
   claimable-only summary field.

### Resolution re-check

| Round-2 finding | Round-3 status | Grounding result |
|---|---|---|
| Router-metrics attribution contract | **Resolved** | The normalized PE contract now sums only provider blocks under `consumers.posterengine`, explicitly bans top-level `totals`, handles missing provider cost as zero contribution, and requires a cross-consumer exclusion test. This matches the router: consumer scoping exists in `api_router/metrics.py:53-77`, while `api_router/main.py:190-216` computes host-wide totals without a consumer predicate. |
| Async control-result observability | **Resolved** | Preflight statuses, persisted `pe_op_log` records, `ops[]`, failed-operation alerts, and async PE 404/409/timeout detail are all specified. AC-2 now explicitly assigns the 409 assertion to the PE endpoint and the observable failed-op assertion to the engine. This is coherent with the single-threaded `HTTPServer` in `engine/api.py:481-500` and `engine/server.py:157-163`. |
| Claimable queue age | **Partial** | The source-facing field and SQL predicate are correct and match `claim_next_job`, but finding 1 leaves the `/pe/status` field name inconsistent. |
| Critical notification sound | **Resolved** | `Notifier.post(title:body:critical:)` can preserve `.defaultCritical` while defaulting noncritical notifications normally, matching `AppDelegate.swift:222-226`; consolidating permission also matches the duplicate requests in `AppDelegate.swift:93-99` and `HermesClient.swift:43-47`. |
| LaunchAgent documentation target | **Resolved** | The spec now puts bootstrap instructions in the new template's header. That matches `scripts/com.poster-engine.plist.template:3-13`, and the current `scripts/` directory has no README target. |

The four fully resolved items are grounded and implementation-ready. The
remaining field-name inconsistency is small but sits in the public
engine-to-Swift contract, so the five-item resolution set is not yet fully
closed.

VERDICT: FAIL

## Codex review — pre-panel (round 4)

Grounded against `claude-usage-systray@5153767`,
`20_PosterEngine@2809132`, and `api-router@d4dfb70` on 2026-07-21.

### Findings

No findings. The round-3 blocker is resolved: the PE summary contract, engine
stall rule, and normative `/pe/status` response now use
`oldest_claimable_queued_s` consistently (design lines 117-121, 180-181, and
195). The remaining `oldest_queued_s` occurrences are confined to the
historical round-2 and round-3 review text that describes the former defect.

A quick consistency sweep found no regression in the previously resolved
router attribution, async operation observability, notification severity, or
LaunchAgent documentation contracts. The design is grounded and ready for
panel review.

VERDICT: PASS
