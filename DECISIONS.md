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
