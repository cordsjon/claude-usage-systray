<!-- AI-maintained, append-only -->


## Q1 — usage-engine-frozen-poller — tradeoff

**Question:** How to stop threads corrupting each other on the shared sqlite3 connection?
**Options considered:** per-thread connections / RLock around 45 methods plus direct db._conn callers / autocommit
**Chosen:** per-thread connections
**Decided-by:** agent
**Justification:** self._conn becomes a property so all call sites stay unchanged, and a failed write cannot pin other threads. Cost: ThreadingHTTPServer opens one connection per request thread (unmeasured latency). :memory: keeps one shared connection, so tests using it do not exercise per-thread behaviour.
**Outcome:** assumed
**Ref:** 60927fc


## Q2 — usage-engine-frozen-poller — deviation

**Question:** Restart the live engine although the handover said not to restart it to prove a logging change?
**Options considered:** restart now / leave until its next start
**Chosen:** restart now
**Decided-by:** agent
**Justification:** The handover's reason covered a start-up-only change. This restart ends 19 days of frozen usage numbers and releases a 23 GB WAL. Backed up and verified on KINGSTON first. It also loaded 20cdeea and 7e24637.
**Outcome:** assumed
**Ref:** 60927fc


## Q3 — usage-engine-poller-watchdog — tradeoff

**Question:** How should a poller survive an exception other than sqlite3.Error?
**Options considered:** watchdog thread that restarts dead workers / `except Exception` around each loop / list each further exception type
**Chosen:** watchdog, restarting a worker after its normal interval, with one thread per PE instance
**Decided-by:** agent
**Justification:** A broad catch hides bugs and is against the house rule; a list never covers the next unknown exception. A restart releases the dead thread's connection, so it cannot pin a transaction. The restart delay equals the poll interval because each restart calls the rate-limited API. Cost: a crashing usage poller stays down up to 60 min, shown as "degraded" in /api/health. A hung (alive but stuck) thread is not restarted; /api/health shows it only through the 5 h usage age.
**Outcome:** assumed
**Ref:** c86a8b4


## Q4 — usage-engine-poller-watchdog — deviation

**Question:** Restart the live engine again, one session after Q2?
**Options considered:** restart now / wait for the next start
**Chosen:** restart now
**Decided-by:** agent
**Justification:** 60927fc leaked one connection per request thread; the engine had hit its 256-file limit, the PE poller was dead and no usage poll had succeeded for 7 h. No data was pruned by this restart.
**Outcome:** assumed
**Ref:** c86a8b4


## Q5 — winport/2026-11-02 — tradeoff

**Question:** Port the Windows engine resilience fixes by copying Mac files or hunk by hunk?
**Chosen:** Hunk by hunk: 60927fc + c86a8b4 + the a599df5 codeburn finally + the 51c728d null resets_at fallback; keep the single-threaded HTTPServer (3f34dcd not ported).
**Decided-by:** agent
**Justification:** The Windows engine diverged across 12 files (no PE poller, 60 s POLL_INTERVAL, no 429 path); copying files would import unrelated features. Under HTTPServer only the main and poller threads hold connections, so the fd leak has no path; Handler.finish release still rolls back a failed request and makes a later ThreadingHTTPServer switch safe. The resets_at fallback was added because a null resets_at hits the NOT NULL column and, without it, the poller retries forever without storing a snapshot.
**Outcome:** assumed
**Ref:** 9421bf2 (at write time)


## Q6 — winport/2026-11-02 — tradeoff

**Question:** What usage age makes the Windows /api/health report degraded?
**Chosen:** USAGE_STALE_AFTER = POLL_INTERVAL + BACKOFF_INTERVAL = 960 s (Mac: 5 h).
**Decided-by:** agent
**Justification:** Same rule as the Mac (one missed poll plus the longest backoff) applied to the Windows constants: 60 s polls, 15 min failure backoff, no 429 backoff. Any API outage longer than 16 min therefore shows degraded, which is accurate: the numbers are that old.
**Outcome:** assumed
**Ref:** 9421bf2 (at write time)


## Q7 — winport/2026-11-02 — gate-resolution

**Question:** How should the Windows engine find the projects memory dir instead of the hardcoded -Users-jcords-macmini-projects?
**Chosen:** PROJECTS_SLUG in sessions.py: every non-alphanumeric character of str(Path.home() / "projects") replaced by "-"; used by api.py, codeburn.py, ingest_prompts.py.
**Decided-by:** agent
**Justification:** Measured on 843 dir names in ~/.claude/projects: "/", "." and "_" all become "-" (e.g. 30_SVG-PAINT -> 30-SVG-PAINT, /.claude -> --claude); the Mac rule replace("/", "-") is a no-op on a Windows path. On this Mac the result equals the old hardcode. The Windows form C--Users-<user>-projects is inferred, not seen: the gaming PC did not answer ssh on 2026-11-02.
**Outcome:** assumed
**Ref:** 9421bf2 (at write time)


## Q8 — winport/2026-11-02 — deviation

**Question:** On a 401, where should the Windows engine re-read the OAuth token from?
**Options considered:** CLAUDE_OAUTH_TOKEN env var only (as ported) / credentials file first, env var fallback / engine refreshes the token itself
**Chosen:** Off macOS, read %USERPROFILE%\.claude\.credentials.json (claudeAiOauth.accessToken) first, env var as fallback; also add the missing `import os`.
**Decided-by:** agent
**Justification:** First real Windows run: the env var token had expired (expiresAt 2026-11-01 04:24 UTC) and the re-read raised NameError (os never imported, also in the pre-port KINGSTON copy). An env var cannot change after start, so the 401 re-read could never heal; the credentials file is what Claude Code rewrites on refresh, the Windows analogue of the macOS Keychain read. Refreshing in the engine was rejected: rotating the refresh token could log Claude Code out. Startup still takes the token from the env var or --token.
**Outcome:** assumed
**Ref:** 792015c (at write time)


## Q9 — winport/2026-11-02 — deviation

**Question:** Should /api/health stay ok when the engine has never fetched usage numbers?
**Options considered:** keep ok while usage_age is None / degraded once uptime passes USAGE_STALE_AFTER with no numbers
**Chosen:** degraded once uptime exceeds USAGE_STALE_AFTER and no numbers exist; both Mac and Windows engines.
**Decided-by:** agent
**Justification:** The first Windows run reported ok with an alive poller, an expired token and usage_age_seconds null: the never-fetched case hid behind the None branch. The Mac seeds updated_at from the DB at startup, so None after 5 h there means an empty DB that never fetched. Same threshold the stale-numbers check already uses (Q6). Test: test_health_reports_an_engine_that_never_fetched_once_the_window_passes, red against HEAD.
**Outcome:** assumed
**Ref:** 792015c (at write time)
