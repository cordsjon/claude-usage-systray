# Token Quota Calibration

`engine/jsonl_rollup.py` divides weighted local-JSONL token sums by hardcoded plan quotas to render `five_hour_util` / `seven_day_util` on the dashboard. Anthropic doesn't publish the exact "claude-tokens" formula nor per-plan quotas, so the defaults are best-effort and **must be calibrated empirically** to match what `/usage` inside Claude Code reports.

## Defaults (Max20 plan)

| Constant | Default | Env override |
|---|---|---|
| `QUOTA_5H` | `50_000_000` weighted tokens | `TOKEN_BUDGET_QUOTA_5H` |
| `QUOTA_7D` | `1_000_000_000` weighted tokens | `TOKEN_BUDGET_QUOTA_7D` |

Both env vars are read at engine startup — restart with `./engine/restart.sh` after changing them.

## Per-token weights

Approximate Anthropic rate-limit accounting (the basket used for quota math is **not** the same as the $-cost ratio):

```
weighted = input
         + 1.5  × cache_creation_input
         + 0.1  × cache_read_input
         + 5.0  × output
```

These live as `W_INPUT`, `W_CACHE_CREATE`, `W_CACHE_READ`, `W_OUTPUT_OPUS`, `W_OUTPUT_SONNET` constants in `engine/jsonl_rollup.py`. Output weight applies regardless of model family — for rate limiting Anthropic appears to use a model-neutral compute basket; only $-cost differs per model.

## Calibration recipe

The cleanest calibration moment is when both signals are simultaneously trustworthy: a successful `/api/oauth/usage` call (or the official `/usage` slash command inside Claude Code) **and** the local rollup running at the same time.

1. Trigger a known-good util reading. Either:
   - Re-enable the API path temporarily: `TOKEN_BUDGET_USE_API=1 ./engine/restart.sh`, wait for one successful poll, note `seven_day_util` from the resulting `usage_snapshots` row.
   - OR run `/usage` inside Claude Code, read the 7-day percentage shown.
2. Immediately read the corresponding `weighted_7d` from the rollup log line (`grep "Rollup:" ~/Library/Logs/ClaudeUsageSystray/engine.log | tail -1`).
3. Backsolve:
   ```
   QUOTA_7D = weighted_7d / (real_util / 100)
   ```
4. Repeat the same procedure for the 5-hour window if you want both windows pinned.
5. Persist:
   ```bash
   launchctl setenv TOKEN_BUDGET_QUOTA_7D <number>     # global, survives reboot
   launchctl setenv TOKEN_BUDGET_QUOTA_5H <number>
   ./engine/restart.sh
   ```

## Known data points

| When | 7d util (source) | weighted_7d (rollup) | Implied QUOTA_7D | Notes |
|---|---|---|---|---|
| 2026-05-22 14:10 UTC | 48% (API, last good snapshot before UA-gate) | unknown (rollup wasn't running yet) | — | Cannot back-calculate; weighted_7d at that exact instant isn't preserved. Use as a sanity floor only. |
| 2026-05-26 13:50 UTC | 59% (rollup default quota = 1B) | 590,633,599 | 1,000,000,000 (default) | Plausible trajectory from 48% on Fri + 4 days heavy Opus work. Not validated against a fresh API number. |

**This table needs a real calibration row.** The current 59% reading assumes the default quota is correct; a fresh API or `/usage` reading is the only way to confirm.

## When to re-calibrate

- After any **Anthropic plan change** (Pro ↔ Max5 ↔ Max20).
- After any **upstream poller pattern shift** suggesting Anthropic changed weights (5h burn rate jumps without matching activity, or `/usage` and dashboard diverge by >5%).
- After any **major Claude Code version update** that changes the JSONL `message.usage` schema — re-check the keys consumed by `_weighted()` in `jsonl_rollup.py`.

## Schema dependencies

The rollup reads these JSONL fields from `~/.claude/projects/*/*.jsonl`:

- `type == "assistant"`
- `timestamp` (ISO 8601 UTC)
- `message.model` (for Opus/Sonnet family detection)
- `message.usage.input_tokens`
- `message.usage.cache_creation_input_tokens`
- `message.usage.cache_read_input_tokens`
- `message.usage.output_tokens`

If a CC upgrade renames any of these, the rollup will silently under-count and the dashboard will look like you've been quiet — re-verify with the dry-run command:

```bash
python3 -c "from engine.jsonl_rollup import compute_snapshot; import json; print(json.dumps(compute_snapshot(), indent=2, default=str))"
```

Expect non-zero `weighted_5h` and `weighted_7d` proportional to recent activity.

## Why this file exists

Without these numbers, calibration is rediscovered from scratch each time. The pivot from `/api/oauth/usage` to local JSONL (2026-05-26) lost the only reliable cross-check; this file is the place to write down the next one when it happens.
