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
