# BACKLOG — claude-usage-systray

## Critical Path

_(none yet — the automation-debt stories below are independent, no chain)_

## Ideation

### US-CUS-OUTCOME-01: Telemetry-only session outcome proxies: hard success signal, hits trouble, abandoned [0d]

> Origin: RESEARCH.md R2 (2026-11-23): expertise-study appendix 'Derived measures' — verified success needs success-signal >= 4 (commit, passing tests, matching command output, explicit confirmation); abandoned = troubled + failed + 0 lines added

**As a** operator tracking whether my operator sessions actually land work,
**I want** CodeBurn to flag per operator session, deterministically from the transcript, a hard success signal (git commit, gh pr create/merge, test command exit 0, explicit user confirmation words), a trouble signal (unrecovered final tool error, failing test at end, frustration keywords) and an abandoned flag (trouble, no hard success, zero lines added), with weekly rates; an LLM classifier using the appendix prompts stays out of scope,
**so that** I get a cheap weekly proxy for the study's verified-success and abandonment rates without paying for per-session model grading.

**Acceptance Criteria:**
- [ ] AC-01: a fixture session ending in git commit + pytest pass is flagged hard_success; one ending in an unrecovered error with no Edit/Write is flagged abandoned
- [ ] AC-02: /api/codeburn returns hard_success_rate, trouble_rate and abandoned_rate for origin=human, computed per session over ORIGIN-01's turn unit
- [ ] AC-03: the panel labels these as telemetry proxies, not the study's classifier-based measures, and shows the study reference (novice 15% / expert 28-33% verified success; novice 19% / others 5-7% abandoned)
- [ ] AC-04: signal rules are documented in the module docstring with the appendix definitions they approximate (success-signal >= 4, failure-signal >= 3, abandoned = failed + 0 lines added)

> _AC block superseded 2026-11-23 23:30 CET by claude-code:87ca0751 — reason: refine 2026-11-23: pin to the corrected turn unit from ORIGIN-01_
> Previous ACs:
> - [ ] AC-01: a fixture session ending in git commit + pytest pass is flagged hard_success; one ending in an unrecovered error with no Edit/Write is flagged abandoned
> - [ ] AC-02: /api/codeburn returns hard_success_rate, trouble_rate and abandoned_rate for origin=human
> - [ ] AC-03: the panel labels these as telemetry proxies, not the study's classifier-based measures


**Priority:** P3 · **Effort:** M · **State:** Ideation · **Tags:** `[codeburn]` `[measurement]` `[outcome]`
> _claude-code:87ca0751, 2026-11-23 23:30 CET_
> DoR 2026-11-23 (20_agentflow dor_gate --track normal): FAIL no-score, missing-panel:architecture-panel. Promotion to Ready needs a spec-panel score and an architecture panel; not run this session.

### US-CUS-AGENTTIME-01: Agent-time panel using the Anthropic employment-report definitions (agent:human hours, idle share, reading load, per-prompt leverage) [0d]

> Origin: RESEARCH.md R2 (2026-11-23): How employment is changing at Anthropic, Fig 2 + Fig 6 notes; scratch replication gave 1.57:1 agent:human, 85.6% idle, 317 h/wk token reading load vs 13 h/wk visible text

**As a** operator comparing my agent use against Anthropic's published internal numbers,
**I want** a CodeBurn panel computing, per week and per origin: agent runtime (inference plus tool use, waits over 30 min excluded, subagents pooled), agent:human hours with human = 40 h/week, idle share = 1 - runtime/wall clock for interactive sessions, reading load = output tokens x 0.75 / 250 wpm next to a visible-text-only variant, and median tool calls and output words per human prompt, each shown beside the study reference value (6:1, ~80%, half a work week, novice 5/600, expert 12/3200),
**so that** the dashboard answers how my setup compares to the study with the study's own definitions instead of ad-hoc scripts.

**Acceptance Criteria:**
- [ ] AC-01: runtime uses a 1800 s wait cap and pools subagent files; for 2026-09-24..2026-11-23 it reports 538 h +/- 5% for interactive+subagent (scratch baseline)
- [ ] AC-02: idle share for the same window reports 85.6% +/- 2 points
- [ ] AC-03: reading load is shown twice, token-based (0.75 words/token, 250 wpm) and visible-text-based, with a note that output tokens include thinking
- [ ] AC-04: each metric renders its study reference value and source (report + figure) as a static label
- [ ] AC-05: per-prompt leverage (median tool calls and output words per prompt) is computed on ORIGIN-01's turn unit and shown per origin; the human-origin medians for 2026-09-24..2026-11-23 are 11 tool calls and 433 visible words (scratch baseline)
- [ ] AC-06: depends on US-CUS-ORIGIN-01 (turn grouping, origin_summary); no new dependencies, stdlib only like the rest of engine/

> _AC block superseded 2026-11-23 23:30 CET by claude-code:87ca0751 — reason: refine 2026-11-23: build on ORIGIN-01 turn unit and origin_summary; add per-origin leverage_
> Previous ACs:
> - [ ] AC-01: runtime uses a 1800 s wait cap and pools subagent files; for 2026-09-24..2026-11-23 it reports 538 h +/- 5% for interactive+subagent (scratch baseline)
> - [ ] AC-02: idle share for the same window reports 85.6% +/- 2 points
> - [ ] AC-03: reading load is shown twice, token-based and visible-text-based, with a note that output tokens include thinking
> - [ ] AC-04: each metric renders its study reference value and source (report + figure) as a static label
> - [ ] AC-05: depends on US-CUS-ORIGIN-01 for the interactive/automated split


**Priority:** P2 · **Effort:** M · **State:** Ideation · **Tags:** `[codeburn]` `[measurement]` `[benchmark]`
> _claude-code:87ca0751, 2026-11-23 23:30 CET_
> DoR 2026-11-23 (20_agentflow dor_gate --track normal): FAIL no-score, missing-panel:architecture-panel. Promotion to Ready needs a spec-panel score and an architecture panel; not run this session.

### US-CUS-ORIGIN-01: CodeBurn and runway split usage by turn origin (operator vs automation vs subagent) [0d]

> Origin: RESEARCH.md R2 (2026-11-23): codeburn.py:891 filters only isSidechain; 60-day sample had 672 operator sessions vs 3,259 automated, all mixed into one-shot rate and cost-per-activity

**As a** operator reading the CodeBurn tab and the runway counter,
**I want** every turn tagged by origin from the JSONL turnOrigin field (human, sdk, subagent; fallback promptSource/entrypoint for records without it), CodeBurn metrics shown for operator turns by default with an origin toggle, and the runway view showing weighted-token burn share per origin plus runway with automation paused,
**so that** one-shot rate and cost per activity describe my own directing, and I can see how much of the 5h/7d quota the eval-runner, nightshift and DoR automation eats.

**Acceptance Criteria:**
- [ ] AC-01: a turn starts only at a prompt record (not tool_result, isMeta or local-command records); a fixture with edit -> bash -> tool_result -> edit yields total_turns == 1 and retries == 1
- [ ] AC-02: classify_origin returns human|sdk from turnOrigin, falling back to promptSource == sdk or entrypoint == sdk-cli; a fixture with one human and one sdk prompt yields one turn per origin
- [ ] AC-03: /api/codeburn accepts origin=human|sdk|all (default all, unchanged totals for existing callers) and the report always carries origin_summary with turns, cost_usd, edit_turns, oneshot_rate, tool_calls, output_tokens per origin
- [ ] AC-04: the Activity Burn tab has an Operator / Automation / All toggle defaulting to Operator; the hero cards show operator vs automation cost and one-shot rate from origin_summary
- [ ] AC-05: /api/status carries current.origin_share_7d, current.origin_share_5h and projection.cycle_runway_hours_human_only; the Runway HUD shows automation's share of 7d burn and the runway if automation paused
- [ ] AC-06: on the live transcripts for 2026-11-16..2026-11-23, /api/codeburn?range=7d&origin=human reports total_turns within 10% of 486 (human prompt count measured 2026-11-23)

> _AC block superseded 2026-11-23 23:30 CET by claude-code:87ca0751 — reason: re-spec 2026-11-23 after reading codeburn.py: turns split on every user-role record (21,926 turns vs 486 prompts in 7d); API default stays all per DECISIONS Q20, fix folded in per Q21_
> Previous ACs:
> - [ ] AC-01: _scan_sessions classifies each turn as human/sdk/subagent; a fixture with one turnOrigin=human and one turnOrigin=sdk session yields one turn per origin
> - [ ] AC-02: /api/codeburn accepts origin=human|sdk|all (default human) and oneshot_rate, cost-per-category honour it
> - [ ] AC-03: the runway panel lists weighted-token share per origin for the current 5h and 7d windows and a runway_hours value recomputed with sdk burn removed
> - [ ] AC-04: against the archived transcripts for 2026-09-24..2026-11-23 the human-origin session count is 672 +/- 5 (scratch benchmark baseline)


**Priority:** P1 · **Effort:** S · **State:** Ideation · **Tags:** `[codeburn]` `[runway]` `[measurement]`
> _claude-code:87ca0751, 2026-11-23 23:30 CET_
> DoR 2026-11-23 (20_agentflow dor_gate --track normal): FAIL no-score, missing-panel:architecture-panel. Promotion to Ready needs a spec-panel score and an architecture panel; not run this session.

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
