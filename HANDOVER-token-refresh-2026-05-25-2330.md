# HANDOVER — Token Refresh + env-var fallback — 2026-05-25 23:30 CEST

## Session context

User asked: "do token refresh by expiresAt and CLAUDE_CODE_OAUTH_TOKEN env var" — two small hardening items from the Maciek-roboblog issue #202 triage.

Session guard fired at 60% of 375K budget before I could finish; handing over rather than squeezing.

## What shipped earlier in this session (committed + pushed)

- `5ddd3c0` — `fix(poller): persist 429 backoff across restarts; bump POLL_INTERVAL to 60m`
- `1de9500` — `fix(poller): match Claude Code's request signature on /api/oauth/usage` (User-Agent, anthropic-version, x-anthropic-additional-protection, Accept)

Both verified empirically — moved us from `Retry-After: 0` (aggressive throttle bucket) to `Retry-After: ~2700` (proper per-account quota).

## What's done in this slice (UNCOMMITTED, on disk in engine/poller.py)

Three edits applied to `engine/poller.py`:

1. **TokenHolder refactor** (~line 48-105) — added `expires_at_ms` field, `set_credentials(token, expires_at_ms)` method (atomic update + only signals `token_refreshed` when token actually changes), `seconds_until_expiry()` method, `_ASSUMED_LIFETIME_MS = 3600 * 1000` constant. `.token` setter now stamps `expires_at_ms = now + 1h` on hot-swap so we don't immediately re-read Keychain.

2. **`_read_keychain_token()` return type** (~line 112-145) — now returns `tuple[str, int] | None` (token + expires_at_ms epoch ms, 0 if Keychain omits it). Parses `claudeAiOauth.expiresAt`. Logs TTL on success.

3. **`fetch_usage` self-heal** (~line 199-205) — uses `creds = _read_keychain_token()` + `token_holder.set_credentials(*creds)` instead of the old token-only setter.

## What's still TODO (~30 min of work)

### Edit 4 — Proactive expiry check in `poll_loop` (poller.py)

Add ONE block at the very top of the `while not stop_event.is_set():` loop, BEFORE the `data, retry_after = fetch_usage(token_holder)` call:

```python
        # Proactive token refresh: re-read Keychain if the current access token
        # is within 60s of expiry. Avoids the 401 → retry overhead on every
        # expiry and prevents one wasted poll against the rate limit.
        ttl = token_holder.seconds_until_expiry()
        if 0 <= ttl < 60 or ttl < 0:  # near-expiry or unknown
            creds = _read_keychain_token()
            if creds:
                token_holder.set_credentials(*creds)
```

Place this immediately above line that currently reads `while not stop_event.is_set():` body — specifically before `data, retry_after = fetch_usage(token_holder)`. Look at the current `poll_loop` structure (post-edit) around line 320-325.

CAVEAT: `ttl < 0` covers the "unknown expiry" case (launched without Keychain context, e.g., via CLAUDE_CODE_OAUTH_TOKEN env var). That means EVERY poll re-reads Keychain when expiry is unknown. Acceptable: Keychain reads are cheap, and `set_credentials` is a no-op if the token didn't change. If you want to skip Keychain entirely for env-var path, gate this on `ttl >= 0`.

### Edit 5 — server.py startup Keychain read

In `main()` between `db = UsageDB(db_path)` and `token_holder = TokenHolder(args.token)`:

```python
    # Try to read Keychain at startup to seed expires_at. Falls back to
    # --token arg (with unknown expiry) when Keychain is unreadable —
    # covers CLAUDE_CODE_OAUTH_TOKEN env-var path where launcher.sh
    # bypasses Keychain entirely.
    from engine.poller import _read_keychain_token  # already imported but module-private
    creds = _read_keychain_token()
    if creds and creds[0] == args.token:
        token_holder = TokenHolder(args.token, creds[1])
    else:
        token_holder = TokenHolder(args.token)
```

The `_read_keychain_token` import is awkward (underscore prefix); consider either:
- A) Remove the underscore and re-export
- B) Add a public `read_keychain_credentials()` wrapper

Either is fine — A is one rename, B is one alias.

### Edit 6 — launcher.sh env-var fallback

Edit `engine/launcher.sh` (currently reads Keychain only). Add CLAUDE_CODE_OAUTH_TOKEN check FIRST:

```bash
# Token source priority: env var > Keychain
TOKEN="${CLAUDE_CODE_OAUTH_TOKEN:-}"
if [[ -z "$TOKEN" ]]; then
    KEYCHAIN_JSON=$(security find-generic-password -s "Claude Code-credentials" -w 2>/dev/null)
    if [[ -z "$KEYCHAIN_JSON" ]]; then
        echo "$LOG_PREFIX Cannot read Claude Code credentials (no env var, Keychain empty)" >&2
        exit 1
    fi
    TOKEN=$(echo "$KEYCHAIN_JSON" | python3 -c "import sys,json; print(json.load(sys.stdin)['claudeAiOauth']['accessToken'])" 2>/dev/null)
    if [[ -z "$TOKEN" ]]; then
        echo "$LOG_PREFIX Cannot parse OAuth token from Keychain JSON" >&2
        exit 1
    fi
fi
```

Replaces the current `KEYCHAIN_JSON=$(security ...)` + `TOKEN=$(echo ...)` block in `launcher.sh:18-29`.

## Verification plan after applying

1. `cd ~/projects/claude-usage-systray && python3 -c "from engine import poller; print('imports ok')"` — sanity check no syntax errors.
2. Clear backoff state + restart:
   ```bash
   sqlite3 /Users/jcords-macmini/.local/share/token-budget/token_budget.db "DELETE FROM engine_state WHERE key LIKE 'poller_%';"
   ./engine/restart.sh
   ```
3. Check log for new lines: `Read fresh token from Keychain (expires in NNNs)` — proves the new tuple flow works.
4. Optionally test env-var path: `CLAUDE_CODE_OAUTH_TOKEN=<token> ./engine/launcher.sh` (kill the existing one first).

## Engine state at handover

- Engine PID may differ — last restart at 23:11:49, currently sleeping until ~23:57 CEST after `Retry-After: 2710`. Persisted backoff (streak=1) is in `engine_state` table.
- Dashboard still shows Fri 22 May 16:10 CEST data — no successful poll yet.
- Headers commit (`1de9500`) is live in the running engine, so next-poll outcome will tell us whether per-account quota has drained.

## Commit message for the slice (when ready)

```
fix(poller): proactive token refresh + CLAUDE_CODE_OAUTH_TOKEN fallback

OAuth access tokens live ~60min. The engine was discovering expiry only
via 401 → Keychain self-heal, wasting one polled call per expiry cycle
against the rate-limit budget. Now:

- TokenHolder tracks expires_at_ms (parsed from Keychain's
  claudeAiOauth.expiresAt epoch field).
- poll_loop re-reads Keychain when ttl < 60s, before fetch_usage fires.
- _read_keychain_token returns (token, expires_at_ms) tuple.
- TokenHolder.token setter assumes 1-hour lifetime for hot-swap callers
  that don't supply expiry (Swift POST /api/token path).

Also adds CLAUDE_CODE_OAUTH_TOKEN env-var fallback in launcher.sh —
covers cloud/CI environments without Keychain access. server.py falls
back gracefully when Keychain read at startup returns nothing or a
different token (expires_at stays at 0 — every poll re-reads).

Ref: Maciek-roboblog/Claude-Code-Usage-Monitor#202

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
```

## What NOT to do

- Don't bump POLL_INTERVAL again — 60 min was chosen empirically this session.
- Don't restart the engine in a "test now" loop — each restart fires one immediate poll against the rate limit. The natural wake at 23:57 (or whatever streak progression has scheduled) is fine.
- Don't change anything in `engine/db.py` — the `engine_state` KV table from `5ddd3c0` is load-bearing and committed.
