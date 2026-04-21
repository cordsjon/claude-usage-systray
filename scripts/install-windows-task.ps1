# Install the PromptUsageIngest scheduled task on Windows.
#
# Registers an hourly task that runs `python -m engine.ingest_prompts` from
# the repo root. Creates a log directory under %LOCALAPPDATA%. Idempotent:
# -Force replaces any existing task with the same name.

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path $PSScriptRoot -Parent
$LogDir = Join-Path $env:LOCALAPPDATA "prompt-usage-ingest"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$Action = New-ScheduledTaskAction `
    -Execute "python" `
    -Argument "-m engine.ingest_prompts" `
    -WorkingDirectory $ProjectDir

$Trigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Hours 1)

Register-ScheduledTask `
    -TaskName "PromptUsageIngest" `
    -Action $Action `
    -Trigger $Trigger `
    -Force `
    -ErrorAction Stop | Out-Null

Write-Host "registered task: PromptUsageIngest"
Write-Host "  WorkingDirectory: $ProjectDir"
Write-Host "  Log dir: $LogDir"
