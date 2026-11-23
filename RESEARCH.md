<!-- AI-maintained, append-only -->

## R1 — tokensaver: Claude Code plugin with fused Search/Edit/Sql tools. ~15,000 tool roundtrips saved in 5 weeks of daily use — reject

**Artifact:** tokensaver: Claude Code plugin with fused Search/Edit/Sql tools. ~15,000 tool roundtrips saved in 5 weeks of daily use — https://www.reddit.com/r/coolgithubprojects/comments/1xyomi7/tokensaver_claude_code_plugin_with_fused/
**Question:** does it enhance claude-usage-systray?
**What it does:** [inference] Reject: source evidence is only the word 'Reddit'; no README, license, stack or maintenance signal. Claude-usage-systray is a usage telemetry tray app, not a tool-surface optimizer, so the claimed roundtrip savings don't map to an identified gap.
**Evidence:** stored snapshot SHA-256 eb0050f72303bcdbbdc071e2a3bf810c903be8d05cdf09f7c3106f2b6f9878bf; runtime capability [unmeasured].
**Verdict:** reject
**Justification:** [inference] No verifiable gap. The candidate claims a Claude Code plugin fusing Search/Edit/Sql tools to reduce tool roundtrips, but the fetched artifact contains no technical content. The resolved project claude-usage-systray covers Claude Code usage visibility (telemetry/systray display), not tool-call fusion; a plugin altering the tool surface would belong closer to 40_mcp-gateway, and overlap there is unconfirmed. Registry matches (claude_code_to_gist, claude_to_markdown, install, wa-bridge) are term-only 'claude' hits and do not indicate a need. Adopt gate parked on missing evidence; stack fit is unknown and likely outside the listed stack. Cost of evaluating an unverified third-party plugin that rewrites core tools outweighs an unsubstantiated gap. Re-evaluate only if a repository README with license and maintenance signal is retrieved.
**Cost if adopted:** n/a
**Revisit-if:** —
**Ref:** gap:110 rev:55c69704a4264705b6b52fbd3ed5a5cf
source: gap 110
drafted-by: gap loop (claude-opus-5-5, from untrusted evidence)

## R2 — Anthropic studies "Agentic coding and persistent returns to expertise" (2026-06) + "How employment is changing at Anthropic" (2026-10) — adopt (metric definitions only)

**Artifact:** Two Anthropic research reports — https://www.anthropic.com/research/claude-code-expertise (paper + 27-page appendix with classifier prompts) and https://www.anthropic.com/research/employment-at-anthropic (20-page report). Local copies: ~/Downloads/anthropic-studies-81Jg17dQz1A/ [verified: ls pdf/ -> 4 PDFs + .txt extracts].
**Question:** do their metric definitions improve the runway counter (engine/stats.py runway_hours, stoppage_detection) and the CodeBurn efficiency dashboard (engine/codeburn.py)?
**What it does:** The expertise study classifies ~398k interactive Claude Code sessions (Oct 2025–Apr 2026) with Sonnet 4.6 at temp 0.2 over a 25k-char truncated transcript, joined with telemetry (human prompts, tool calls, output tokens, lines added). The employment report measures agent runtime, idle share and reading load from session telemetry inside Anthropic.
**Evidence:**
- Definitions read from the appendix text [verified: sed appendix lines 735-757]: verified success = outcome classifier "Succeeded" AND success-signal >= 4 (a hard signal: matching commit, passing tests, command output matching the goal, explicit user confirmation); hits trouble = failure-signal >= 3; abandoned = troubled AND Failed AND 0 lines of code added. Sample excludes headless (claude -p) sessions, SDK usage and third-party IDE integrations (appendix lines 9-14).
- Employment-report definitions [verified: sed report lines 173-176, 313-319, 337]: agent working time = runtime incl. inference and tool use, excluding any wait > 30 min, interactive employee-directed sessions with subagents pooled; human hours = headcount x 40 h/week; idle = wall clock minus active time; reading load = output tokens x 0.75 words/token / 250 wpm. "8,500 standing instructions" has no stated unit.
- Classifier validation: 78–98% agreement with a stronger reference model on categorical labels, 53–68% exact on ordinal scales; authors advise relative comparisons only (appendix lines 818-831). Task-value pricer log R² 0.38, used ordinally only.
- Scratch replication on this machine's transcripts, 2026-09-24..2026-11-23, with the study definitions [verified: benchmark_study.py -> "agent interactive+sub=538h ratio=1.57 ... idle share interactive = 85.6% ... reading load: all output tokens 317 h/wk; visible text only 13.0 h/wk"]; 672 interactive vs 3,259 automated sessions; per human prompt median 11 tool calls, 433 visible words, ~6,000 output-token words.
- Gap found in CodeBurn: _scan_sessions filters only isSidechain (codeburn.py:891) and never reads turnOrigin/entrypoint, so one-shot rate and cost-per-activity mix operator turns with eval-runner/nightshift/DoR sessions. In the 60-day sample, filtering on entrypoint alone counted 2,129 "interactive" sessions vs 672 with turnOrigin == "human" [verified: benchmark.py runs bench.json vs bench2.json].
- No outcome/expertise classification was run on local transcripts [unmeasured].
**Verdict:** adopt
**Justification:** Adopt the definitions, not code: they give the dashboard published, comparable denominators (30-min wait cap, 40 h human week, 0.75 x 250 reading rate, verified-success/abandoned rules) and expose the origin-mixing gap above. The deterministic telemetry measures fit token-frugality; the LLM classifiers stay optional and sampled because the authors themselves only trust them for relative comparisons.
**Cost if adopted:** no new dependencies; engine changes in codeburn.py/stats.py plus dashboard panels. Optional sampled classifier would add per-run model cost [unmeasured].
**Revisit-if:** Anthropic publishes a revised appendix or a follow-up with different definitions; or the turnOrigin field disappears from Claude Code JSONL.
**Ref:** BACKLOG US-CUS-ORIGIN-01, US-CUS-AGENTTIME-01, US-CUS-OUTCOME-01 (pending)
