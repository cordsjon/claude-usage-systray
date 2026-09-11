# Claude Usage Systray — Operator Notes

## Engine (port 17420)

- Restart + verify freshness (preferred): `engine/restart.sh`
- Hot-reload code after local patches (kill only; watchdog/launchd respawns): `scripts/reload-server.sh`

## Prompt ingest

Ingests Claude Code transcripts (`~/.claude/projects/*.jsonl`) into `token_budget.db`.

- Inspect (counts + samples + config sanity): `python3 -m engine.ingest_prompts --inspect`
- Reset ingest state (watermarks + prompt tables) then ingest: `python3 -m engine.ingest_prompts --reset`

## PE instance probe

One-shot read-only health check of the PosterEngine instances in
`~/.local/share/token-budget/pe_instances.json` — resolves each instance's Keychain
token, then hits the same `/api/jobs/summary` + `/api/admin/router-metrics` the poller
uses. Use it when `/pe/status` looks wrong, to tell instance/token/engine faults apart.
Never prints the token.

- All instances: `python3 -m engine.pe_probe`
- One instance, machine-readable: `python3 -m engine.pe_probe --instance dev --json`
- Exit codes: `0` all reachable, `1` at least one failed, `2` nothing to probe
  (missing/empty/bad config, unknown `--instance`).

## launchd (macOS)

Two agents, two installers:

- **Engine** (`com.claude-usage-engine`): `scripts/install-engine-launchd.sh` — renders
  the plist with absolute paths + token quotas, reloads via bootout+bootstrap.
  `--dry-run` to preview; `TOKEN_BUDGET_QUOTA_7D=… TOKEN_BUDGET_QUOTA_5H=…` to override
  quotas (see `TOKEN-QUOTA-CALIBRATION.md`).
- **Prompt-ingest** (`com.jcords.prompt-usage-ingest`):
  `scripts/install-macos-launchd.sh --dry-run` to preview the rendered plist + dependency
  check; `--bootstrap` to create `.venv` if missing (Py>=3.10 + PyYAML).

