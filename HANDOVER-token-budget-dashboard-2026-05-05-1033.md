# HANDOVER — Token Budget Dashboard — 2026-05-05 10:33

## What was done

### Problem
Dashboard was stale — `updated_at` was 16 hours old, `token_needs_refresh: true`.

### Root cause
OAuth token expired; API started returning 401. The `_read_keychain_token()` self-heal in `fetch_usage()` only fires once per 401, not repeatedly. After self-heal failed (same expired token in Keychain at that moment), the poller entered `stop_event.wait(BACKOFF_INTERVAL)` — a 15-min sleep that doesn't wake on token hot-swap.

### Fixes applied

**Immediate fix:** Hot-swapped the fresh Keychain token via `POST /api/token`, then restarted via `engine/restart.sh`.

**Structural fix** (`engine/poller.py`):
- Added `token_refreshed = threading.Event()` to `TokenHolder`
- `token.setter` calls `self.token_refreshed.set()` after updating
- Both `stop_event.wait(BACKOFF_INTERVAL)` calls in `poll_loop` replaced with:
  ```python
  token_holder.token_refreshed.wait(BACKOFF_INTERVAL)
  token_holder.token_refreshed.clear()
  ```
  This means any future token hot-swap via `POST /api/token` immediately interrupts the backoff and retries.

### Current state
- Engine running on port 17420, `updated_at: 2026-05-05T08:33:04Z`
- `7d_util: 97.0%`, `token_needs_refresh: false`
- KICKOFF showed empty pipeline files (INBOX/BACKLOG/TODO/DONE all empty)

## If you continue

- No open tasks from this session
- Changes not committed — commit `engine/poller.py` if desired
- Dashboard accessible at `http://127.0.0.1:17420`
