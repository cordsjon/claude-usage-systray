# Claude Usage Systray — Operator Notes

## Engine (port 17420)

- Restart + verify freshness (preferred): `engine/restart.sh`
- Hot-reload code after local patches (kill only; watchdog/launchd respawns): `scripts/reload-server.sh`

## Prompt ingest

Ingests Claude Code transcripts (`~/.claude/projects/*.jsonl`) into `token_budget.db`.

- Inspect (counts + samples + config sanity): `python3 -m engine.ingest_prompts --inspect`
- Reset ingest state (watermarks + prompt tables) then ingest: `python3 -m engine.ingest_prompts --reset`

## launchd (macOS)

Two agents, two installers:

- **Engine** (`com.claude-usage-engine`): `scripts/install-engine-launchd.sh` — renders
  the plist with absolute paths + token quotas, reloads via bootout+bootstrap.
  `--dry-run` to preview; `TOKEN_BUDGET_QUOTA_7D=… TOKEN_BUDGET_QUOTA_5H=…` to override
  quotas (see `TOKEN-QUOTA-CALIBRATION.md`).
- **Prompt-ingest** (`com.jcords.prompt-usage-ingest`):
  `scripts/install-macos-launchd.sh --dry-run` to preview the rendered plist + dependency
  check; `--bootstrap` to create `.venv` if missing (Py>=3.10 + PyYAML).

