# prompt-usage-ingest — scheduled install

## Purpose

Schedules `python3 -m engine.ingest_prompts` to run once per hour, populating
the Habits tab (US-TB-01) from Claude Code transcripts at
`~/.claude/projects/*/conversations/*.jsonl`. Output logs to
`~/.local/state/prompt-usage-ingest.log` (macOS) or
`%LOCALAPPDATA%\prompt-usage-ingest\` (Windows).

## macOS install

```bash
bash scripts/install-macos-launchd.sh
```

The script detects the repo root, substitutes `__PROJECT_DIR__` and
`__LOG_PATH__` in the template plist, writes the result to
`~/Library/LaunchAgents/com.jcords.prompt-usage-ingest.plist`, and
`launchctl load`s it. `RunAtLoad=true` triggers an immediate first run.

## Windows install

From a PowerShell prompt with privileges to register scheduled tasks:

```powershell
pwsh -NoProfile -File scripts\install-windows-task.ps1
```

Creates a task named `PromptUsageIngest` that repeats every hour with the
repo root as the working directory.

## Uninstall

macOS:

```bash
launchctl unload ~/Library/LaunchAgents/com.jcords.prompt-usage-ingest.plist
rm ~/Library/LaunchAgents/com.jcords.prompt-usage-ingest.plist
```

Windows:

```powershell
Unregister-ScheduledTask -TaskName "PromptUsageIngest" -Confirm:$false
```
