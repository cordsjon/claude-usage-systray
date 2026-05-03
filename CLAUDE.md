# Claude Usage Systray — Operator Notes

## Engine (port 17420)

- Restart + verify freshness (preferred): `engine/restart.sh`
- Hot-reload code after local patches (kill only; watchdog/launchd respawns): `scripts/reload-server.sh`

## Prompt ingest

Ingests Claude Code transcripts (`~/.claude/projects/*.jsonl`) into `token_budget.db`.

- Inspect (counts + samples + config sanity): `python3 -m engine.ingest_prompts --inspect`
- Reset ingest state (watermarks + prompt tables) then ingest: `python3 -m engine.ingest_prompts --reset`

## launchd (macOS)

- Preview rendered plist + dependency check: `scripts/install-macos-launchd.sh --dry-run`
- Bootstrap `.venv` if missing (Py>=3.10 + PyYAML): `scripts/install-macos-launchd.sh --bootstrap`

