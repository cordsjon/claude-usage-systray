# BACKLOG — claude-usage-systray

## Critical Path

_(none yet — the automation-debt stories below are independent, no chain)_

## Ideation

### US-CUS-DEPLOY-01: dist-deploy script for the Swift app

> Origin: Automation debt analysis (2026-07-22) — app deploy chain (xcodebuild Release → cp to dist → codesign --force --deep → launchctl kickstart) run manually 2× this session; caused 2 codesign SIGKILLs when the re-sign step was skipped.

**As a** systray developer,
**I want** a `scripts/deploy-app.sh` that builds Release, copies the bundle into `dist/`, applies `xattr -cr` + `codesign --force --deep --sign -` in place, then `launchctl kickstart -k` the login-item service,
**so that** shipping a new app build is one command instead of a 4-step manual chain where forgetting the re-sign kills the relaunch with a Launch Constraint Violation.

**Acceptance Criteria:**
- [ ] Script does: xcodegen generate (if project.yml newer) → xcodebuild -scheme … -configuration Release → cp -R build product to `dist/ClaudeUsageSystray.app` → `xattr -cr` → `codesign --force --deep --sign -` → verify `codesign -v dist/…app` passes → `launchctl kickstart -k gui/$UID/com.claude-usage-systray`
- [ ] Backs up the prior `dist/…app` to `dist/…app.pre-deploy.bak` before overwrite (rollback path)
- [ ] Refuses to kickstart if `codesign -v` fails (never ship an unsigned bundle that SIGKILLs on relaunch)
- [ ] Test: run the script on a clean build; assert the running PID changed and `codesign -v` on the dist bundle exits 0

**Size:** M · **Tags:** `[deploy]` `[swift]` `[automation-debt]`

### US-CUS-TESTRUN-01: Swift test-runner wrapper (xcodegen + xcodebuild test)

> Origin: Automation debt analysis (2026-07-22) — the `xcodegen generate && xcodebuild test && grep filter` chain was run ~6× this session; repo has no test-runner script.

**As a** systray developer,
**I want** a `scripts/test-swift.sh` that regenerates the Xcode project from `project.yml`, runs `xcodebuild test`, and filters output to the pass/fail summary,
**so that** running the Swift suite is one command instead of a 3-command chain retyped every iteration.

**Acceptance Criteria:**
- [ ] Script runs `xcodegen generate` (in `claude-usage-systray/`) then `xcodebuild test -scheme … -destination 'platform=macOS'` and greps to the `Test Suite … passed/failed` summary line
- [ ] Non-zero exit when any test fails (so CI / callers can gate on it)
- [ ] Test: run against current HEAD; assert exit 0 and the summary reports the known passing count (39 as of 2026-07-22)

**Size:** S · **Tags:** `[test]` `[swift]` `[automation-debt]`

### US-CUS-PETOKEN-01: PE supervisor token-mint CLI (in PosterEngine)

> Origin: Automation debt analysis (2026-07-22) — PE supervisor token provisioning (create_api_user + mint_token + Keychain add) done via inline `python3 -c` for both dev and prod instances; no CLI in PosterEngine.
> Note: fix lands in the PosterEngine repo (~/projects/15_SAAS/20_PosterEngine), not this one — this story is tagged for migration.

**As a** systray operator supervising a PE instance,
**I want** a PosterEngine CLI subcommand `pes-cli mint-supervisor-token --email supervisor@pe.local` that idempotently creates the api-role user, mints a token, and prints the plaintext once,
**so that** provisioning a supervisor token for a new PE instance is one command instead of a hand-written inline Python block against `auth.create_api_user` + `api_tokens.mint_token`.

**Acceptance Criteria:**
- [ ] Subcommand creates the api user if absent (idempotent on email), always mints a fresh token, prints plaintext + prefix to stdout exactly once, never persists plaintext
- [ ] Runs inside the target container (`docker exec poster-engine pes-cli mint-supervisor-token …`) against `DEFAULT_DB_PATH`
- [ ] Optional `--keychain-service NAME` flag: when run on the host (not container), stores the minted token in login Keychain under that service and does not print it
- [ ] Test: invoke twice with the same email → 2 distinct tokens, 1 user row; both authenticate against `/api/jobs/summary` (200)

**Size:** M · **Tags:** `[cli]` `[posterengine]` `[automation-debt]` `[migrate:20_PosterEngine]`

## Done

_(none yet)_
