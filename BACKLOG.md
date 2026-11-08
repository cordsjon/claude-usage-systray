# BACKLOG — claude-usage-systray

## Critical Path

_(none yet — the automation-debt stories below are independent, no chain)_

## Ideation

### US-CUS-TENETS-01: Tenets tab counts per-tenet referent events, not only [TENET:] tags [0d]

> Origin: Tenets-tab inspection 2026-11-08: dashboard counts only [TENET:] tags in assistant text; same week foundation-first ran bin/foundations open 100x (1 tag), source-transparency produced ~3100 [verified] tags (0 tags), architecture-first ran brainstorming+writing-plans+spec-panel 14x (1 tag). Wired tenets stop producing tag vocabulary, so the tab ranks the best-enforced tenets lowest. See memory feedback_tenet_tag_count_is_not_usefulness.

**As a** governance operator reading the Tenets tab,
**I want** each tenet row to show its referent fires for the period — hook fires (tenet-gate, output-claim-guard), CLI runs (bin/foundations open), skill invocations (brainstorming, writing-plans, spec-panel, log-research, blueprint) and [verified] tags — with [TENET:] tags as one column among them, driven by a small per-tenet referent table in engine/codeburn.py,
**so that** the tab reports which tenets fire through their wiring instead of which tenet words get said, and happy-path-first / abstract-on-third show as the only tenets with no referent at all.

**Acceptance Criteria:**
- [ ] AC-01: A referent table maps each TENETS.md slug to zero or more referents of kind hook|cli|skill|tag with a transcript-detectable pattern; happy-path-first and abstract-on-third map to none and the tab renders them as 'no referent'
- [ ] AC-02: /api/codeburn returns per tenet: tag_count and referent_counts by kind for the selected period; for the week 2026-10-26..11-02 foundation-first reports cli >= 90 and source-transparency reports tag(verified) >= 1000 against the archived transcripts
- [ ] AC-03: The Tenets tab shows referent columns beside the tag column and sorts by total referent fires; the tag-only ranking is no longer the default sort
- [ ] AC-04: tenet-gate logs each block (session, gate, file) to ~/.local/state/tenet-gate/blocks.jsonl so hook fires become countable; the repurpose gate's search_done rate is reported as a diagnostic (measured 1060/1065 True on 2026-11-08)

**Priority:** P2 · **Effort:** M · **State:** Ideation · **Tags:** `[tenets]` `[codeburn]` `[measurement]`
- TODO: show stale usage data as stale in the menu bar. Since c86a8b4 `/api/health` answers `"status": "degraded"` (still HTTP 200) when a poller is down or `usage_age_seconds` exceeds 5 h, and lists each worker. The Swift app still reads only `token_needs_refresh` and shows the last numbers; it needs to read `status` / `usage_age_seconds` and mark the numbers as old.

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
