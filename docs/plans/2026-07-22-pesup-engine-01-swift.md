# US-PESUP-ENGINE-01 (Swift half) Implementation Plan

> **For agentic workers:** REQUIRED: Use `/sh:execute` to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a PE (PosterEngine) supervisor section to the claude-usage-systray menu bar popover — reads the engine's `GET /pe/status`, shows per-instance health/spend/budget, fires notifications on stall/budget/op-failure with stable-id dedupe, and offers Retry/Kick controls. Extract a shared `Notifier` helper as part of the work (existing notification code is duplicated across two files with divergent behavior).

**Architecture:** New `PosterEngineService.swift` clones `UsageService.swift`'s one-shot self-rescheduling `Timer` pattern (its own 60s poll against `http://localhost:17420/pe/status`, independent of `UsageService`'s own timer). New `Notifier.swift` consolidates the three existing independent `UNUserNotificationCenter` call sites (`HermesClient.notifyError`/`requestNotificationPermission`, `AppDelegate.setupNotifications`/`sendNotification`) into one authorization request and one `post(title:body:critical:)` function, preserving today's `.defaultCritical` sound behavior. `MenuBarView.swift` gains a new `peSection` computed view, following the existing `modelBreakdown`/`shortcutsSection` pattern.

**Tech Stack:** Swift 5, SwiftUI, `URLSession`, `UNUserNotification`, XCTest. No new dependencies — this is a pure app-code addition, same toolchain as the rest of the app.

**Depends on:** `docs/plans/2026-07-22-pesup-engine-01-poller.md` (engine half) must be implemented, tested, and its `/pe/status` contract confirmed stable before starting Task 3 of this plan (the live-shaped fixture data below is taken directly from that plan's `_handle_pe_status` implementation — if the engine plan's response shape changed during its own review, update this plan's fixtures first).

---

## Premises (verify before implementing)

- `UsageService.swift:136-141` `scheduleTimer(interval:)` is the one-shot, re-scheduled-per-result Timer pattern (`repeats: false`) to clone — verified 2026-07-22 (full file read).
- `AppDelegate.swift:80-91` `setupPopover()` hosts `MenuBarView` via `NSHostingController`; `MenuBarView.swift:12-38` composes sections as private computed `some View` properties in a `VStack` with `Divider()` separators — verified 2026-07-22.
- Three independent notification call sites exist today: `HermesClient.swift:30-41` (`notifyError`, no sound param), `HermesClient.swift:45-48` (`requestNotificationPermission`, `.alert` only), `AppDelegate.swift:93-99` (`setupNotifications`, `.alert, .sound`), `AppDelegate.swift:222-239` (`sendNotification`, `.defaultCritical` for critical) — verified 2026-07-22 (full files read). Both permission requests currently fire independently at every launch (`AppDelegate.swift:30` calls `HermesClient.requestNotificationPermission()` right after its own `setupNotifications()` at line 29).
- `Tests/UsageServiceTests.swift` is the test-file convention to clone: `@testable import ClaudeUsageSystray`, `XCTestCase` subclasses grouped by subject with `// MARK:` — verified 2026-07-22.
- Engine contract this plan consumes (from the engine plan, Task 7's `_handle_pe_status`): `GET /pe/status` → `{"instances": [{"name", "reachable", "counts", "oldest_claimable_queued_s", "stalled", "recent_terminal", "cost": {"d24h_usd", "calls", "available"}, "budget": {"target_24h_usd", "crossed"}, "last_poll"}], "alerts": [{"alert_id", "first_seen", "last_seen", "active"}], "ops": [{"op_id", "instance", "kind", "target", "state", "detail", "ts"}]}` — verified 2026-07-22 against the engine plan's own implementation code (not yet executed as of this writing — re-verify against the live engine once Task 11 of the engine plan lands, per this plan's Task 1 premise check below).
- Control routes: `POST /pe/<instance>/jobs/<id>/retry`, `POST /pe/<instance>/worker/kick` — both return `202 {"accepted": true, "op_id": "..."}` on success, `404` on unknown instance/job, `429` on kick rate-limit — verified 2026-07-22 against the engine plan.

---

## Chunk 1: Shared Notifier extraction

### Task 1: `Notifier.swift` — consolidate permission + send

**Files:**
- Create: `claude-usage-systray/claude-usage-systray/Sources/Notifier.swift`
- Test: `claude-usage-systray/claude-usage-systray/Tests/NotifierTests.swift`

- [ ] **Step 0: Re-verify the engine contract premise before writing any Swift code**

Run: `curl -s http://127.0.0.1:9120/health` (PE dev, unrelated but confirms your dev machine state hasn't drifted) and, if the engine plan has been executed in this session, `curl -s http://localhost:17420/pe/status | python3 -m json.tool`. If the engine isn't running yet or the shape differs from the Premises section above, STOP and reconcile before continuing — this plan's fixtures are load-bearing on that exact shape.

- [ ] **Step 1: Write the failing test**

```swift
// claude-usage-systray/claude-usage-systray/Tests/NotifierTests.swift
import XCTest
import UserNotifications
@testable import ClaudeUsageSystray

final class NotifierTests: XCTestCase {

    func testPostBuildsNonCriticalContentWithDefaultSound() {
        let content = Notifier.buildContent(title: "Test Title", body: "Test body", critical: false)
        XCTAssertEqual(content.title, "Test Title")
        XCTAssertEqual(content.body, "Test body")
        XCTAssertEqual(content.sound, .default)
    }

    func testPostBuildsCriticalContentWithDefaultCriticalSound() {
        let content = Notifier.buildContent(title: "Alert", body: "Something broke", critical: true)
        XCTAssertEqual(content.sound, .defaultCritical)
    }
}
```

Note: `Notifier.post(...)` itself talks to `UNUserNotificationCenter.current()`, which is not meaningfully unit-testable without a live notification center mock (no such mock exists anywhere in this codebase today, per the `UsageServiceTests` note that `urlSession` is an unused-so-far test hook). Splitting `buildContent` out as a pure, testable function — with `post` as a thin wrapper that calls it and hands the result to `UNUserNotificationCenter` — is the same pure/impure split `UsageService.swift` uses for `calculateUtilization`/`formatTimeRemaining` (lines 71-84) vs. its network code. Clone that split here rather than trying to test the notification center call itself.

- [ ] **Step 2: Run test to verify it fails**

Run (from Xcode or `xcodebuild test`, per this project's existing test workflow — check `README.md` for the exact invocation used elsewhere in this repo before assuming `swift test`, since this is an `.xcodeproj`-based app, not a SwiftPM executable):

```bash
cd ~/projects/claude-usage-systray/claude-usage-systray
xcodebuild test -project ClaudeUsageSystray.xcodeproj -scheme ClaudeUsageSystray -destination 'platform=macOS' 2>&1 | tail -40
```

Expected: FAIL / build error — `Notifier` does not exist.

- [ ] **Step 3: Write minimal implementation**

```swift
// claude-usage-systray/claude-usage-systray/Sources/Notifier.swift
import Foundation
import UserNotifications

/// Single consolidation point for all UNUserNotification calls in this app.
///
/// Before this file, three independent call sites requested authorization
/// and posted notifications separately (HermesClient, AppDelegate), with
/// divergent option sets (.alert-only vs .alert+.sound) and no shared sound
/// policy. This preserves AppDelegate's existing .defaultCritical behavior
/// for critical alerts while giving every caller one function to call.
enum Notifier {

    /// Request notification authorization once. Call exactly once per app
    /// launch (AppDelegate.applicationDidFinishLaunching) — do not call from
    /// multiple sites; that was the original duplication this replaces.
    static func requestAuthorization() {
        guard #available(macOS 11.0, *) else { return }
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, error in
            if let error = error {
                AppLogger.error("general", "Notification authorization error: \(error)")
            }
        }
    }

    /// Pure content-building, split out so sound/severity logic is testable
    /// without touching the live notification center (see NotifierTests).
    static func buildContent(title: String, body: String, critical: Bool) -> UNMutableNotificationContent {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = critical ? .defaultCritical : .default
        return content
    }

    static func post(title: String, body: String, critical: Bool = false) {
        guard #available(macOS 11.0, *) else { return }
        let content = buildContent(title: title, body: body, critical: critical)
        let request = UNNotificationRequest(
            identifier: UUID().uuidString,
            content: content,
            trigger: nil
        )
        UNUserNotificationCenter.current().add(request) { error in
            if let error = error {
                AppLogger.error("general", "Notification error: \(error)")
            }
        }
    }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run the same `xcodebuild test` command as Step 2.
Expected: PASS (2 tests)

- [ ] **Step 5: Migrate the three existing call sites**

In `HermesClient.swift`, replace lines 30-41 (`notifyError`):
```swift
    private static func notifyError(_ message: String) {
        Notifier.post(title: "Hermes Shortcut", body: message, critical: false)
    }
```
Delete lines 43-48 (`requestNotificationPermission`) entirely — `AppDelegate` now calls `Notifier.requestAuthorization()` once instead (Step 6 below). Search the repo for any other call site of `HermesClient.requestNotificationPermission()` first:

Run: `grep -rn "requestNotificationPermission" ~/projects/claude-usage-systray/claude-usage-systray/Sources/`
Expected: only `AppDelegate.swift:30` — the one call site being replaced in Step 6.

In `AppDelegate.swift`:
- Delete `setupNotifications()` (lines 93-99) entirely.
- Change line 29-30 from:
  ```swift
      setupNotifications()
      HermesClient.requestNotificationPermission()
  ```
  to:
  ```swift
      Notifier.requestAuthorization()
  ```
- Replace `sendNotification(title:body:isCritical:)` (lines 222-239) with a private forwarding shim so the two existing call sites (`checkForNotifications()` at lines 198-203 and 205-210) don't need their own edits yet — actually, simplest and clearest is to update the two call sites directly and delete the method:

  Delete lines 222-239 (`sendNotification`).

  Change lines 197-203 from:
  ```swift
          if usage >= criticalThreshold && lastCriticalNotified < criticalThreshold {
              sendNotification(
                  title: "Critical: Claude Usage",
                  body: "You've used \(usage)% of your weekly quota. Consider pausing non-essential tasks.",
                  isCritical: true
              )
              lastCriticalNotified = criticalThreshold
  ```
  to:
  ```swift
          if usage >= criticalThreshold && lastCriticalNotified < criticalThreshold {
              Notifier.post(
                  title: "Critical: Claude Usage",
                  body: "You've used \(usage)% of your weekly quota. Consider pausing non-essential tasks.",
                  critical: true
              )
              lastCriticalNotified = criticalThreshold
  ```
  And lines 204-210 from:
  ```swift
          } else if usage >= warningThreshold && lastWarningNotified < warningThreshold && usage < criticalThreshold {
              sendNotification(
                  title: "Warning: Claude Usage",
                  body: "You've used \(usage)% of your weekly quota.",
                  isCritical: false
              )
              lastWarningNotified = warningThreshold
  ```
  to:
  ```swift
          } else if usage >= warningThreshold && lastWarningNotified < warningThreshold && usage < criticalThreshold {
              Notifier.post(
                  title: "Warning: Claude Usage",
                  body: "You've used \(usage)% of your weekly quota."
              )
              lastWarningNotified = warningThreshold
  ```

- [ ] **Step 6: Build and run the full existing test suite**

Run:
```bash
cd ~/projects/claude-usage-systray/claude-usage-systray
xcodebuild test -project ClaudeUsageSystray.xcodeproj -scheme ClaudeUsageSystray -destination 'platform=macOS' 2>&1 | tail -60
```
Expected: build succeeds, all existing tests (`UsageServiceTests`, `ShortcutLoaderTests`) still PASS, plus the 2 new `NotifierTests`.

- [ ] **Step 7: Manual smoke check (notifications are not meaningfully unit-testable end-to-end)**

Launch the app locally (Xcode Run), trigger a warning-threshold notification path if feasible (or trust the build + unit test coverage if not easily reproducible in dev) — note in the PR/commit if this manual check was skipped and why, per this project's "complete the loop" convention (workstyle rule: a change isn't done until verified running, not just compiling).

- [ ] **Step 8: Commit**

```bash
cd ~/projects/claude-usage-systray
git add claude-usage-systray/claude-usage-systray/Sources/Notifier.swift \
        claude-usage-systray/claude-usage-systray/Sources/HermesClient.swift \
        claude-usage-systray/claude-usage-systray/Sources/AppDelegate.swift \
        claude-usage-systray/claude-usage-systray/Tests/NotifierTests.swift
git commit -m "refactor(pe-supervisor): extract shared Notifier, consolidate 3 duplicate notification call sites to 1"
```

---

## Chunk 2: PosterEngineService — poll + decode + state

### Task 2: PE status model + decode (pure, testable)

**Files:**
- Create: `claude-usage-systray/claude-usage-systray/Sources/PosterEngineService.swift`
- Test: `claude-usage-systray/claude-usage-systray/Tests/PosterEngineServiceTests.swift`

- [ ] **Step 1: Write the failing test**

```swift
// claude-usage-systray/claude-usage-systray/Tests/PosterEngineServiceTests.swift
import XCTest
@testable import ClaudeUsageSystray

// MARK: - PEStatus decoding

final class PEStatusDecodingTests: XCTestCase {

    func testDecodesFullStatusResponse() throws {
        let json = """
        {"instances": [{"name": "prod", "reachable": true,
          "counts": {"queued": 0, "running": 1, "complete_24h": 14,
                      "dead": 0, "failed": 2},
          "oldest_claimable_queued_s": 0, "stalled": false,
          "recent_terminal": [{"job_id": "j1", "status": "failed", "topic": "poster",
                                "error": "boom", "updated_at": "2026-07-21T23:00:00Z"}],
          "cost": {"d24h_usd": 0.0008, "calls": 12, "available": true},
          "budget": {"target_24h_usd": 0.5, "crossed": false},
          "last_poll": "2026-07-21T23:00:00Z"}],
         "alerts": [{"alert_id": "stalled:dev:active", "first_seen": "2026-07-21T22:58:00Z",
                      "last_seen": "2026-07-21T23:00:00Z", "active": true}],
         "ops": [{"op_id": "op-1", "instance": "dev", "kind": "retry",
                   "target": "1b4d1c31", "state": "ok", "detail": null, "ts": "2026-07-21T23:00:00Z"}]}
        """.data(using: .utf8)!

        let status = try JSONDecoder().decode(PEStatus.self, from: json)

        XCTAssertEqual(status.instances.count, 1)
        XCTAssertEqual(status.instances[0].name, "prod")
        XCTAssertTrue(status.instances[0].reachable)
        XCTAssertEqual(status.instances[0].counts.running, 1)
        XCTAssertEqual(status.instances[0].recentTerminal.count, 1)
        XCTAssertEqual(status.instances[0].recentTerminal[0].jobId, "j1")
        XCTAssertEqual(status.instances[0].cost.d24hUsd, 0.0008)
        XCTAssertFalse(status.instances[0].budget.crossed)
        XCTAssertEqual(status.alerts.count, 1)
        XCTAssertTrue(status.alerts[0].active)
        XCTAssertEqual(status.ops.count, 1)
        XCTAssertEqual(status.ops[0].state, "ok")
    }

    func testDecodesUnreachableInstanceGracefully() throws {
        let json = """
        {"instances": [{"name": "dev", "reachable": false,
          "counts": {}, "oldest_claimable_queued_s": 0, "stalled": false,
          "recent_terminal": [],
          "cost": {"d24h_usd": 0.0, "calls": 0, "available": false},
          "budget": {"target_24h_usd": 1.0, "crossed": false},
          "last_poll": null}],
         "alerts": [], "ops": []}
        """.data(using: .utf8)!

        let status = try JSONDecoder().decode(PEStatus.self, from: json)
        XCTAssertFalse(status.instances[0].reachable)
        XCTAssertNil(status.instances[0].lastPoll)
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `xcodebuild test -project ClaudeUsageSystray.xcodeproj -scheme ClaudeUsageSystray -destination 'platform=macOS' 2>&1 | tail -40`
Expected: FAIL / build error — `PEStatus` does not exist.

- [ ] **Step 3: Write minimal implementation**

```swift
// claude-usage-systray/claude-usage-systray/Sources/PosterEngineService.swift (partial — model only; service class added in Task 3)
import Foundation

// MARK: - PE status model (decodes GET http://localhost:17420/pe/status)

struct PEStatus: Decodable {
    let instances: [PEInstanceStatus]
    let alerts: [PEAlert]
    let ops: [PEOp]
}

struct PEInstanceStatus: Decodable {
    let name: String
    let reachable: Bool
    let counts: PECounts
    let oldestClaimableQueuedS: Int
    let stalled: Bool
    let recentTerminal: [PETerminalJob]
    let cost: PECost
    let budget: PEBudget
    let lastPoll: String?

    enum CodingKeys: String, CodingKey {
        case name, reachable, counts, stalled, cost, budget
        case oldestClaimableQueuedS = "oldest_claimable_queued_s"
        case recentTerminal = "recent_terminal"
        case lastPoll = "last_poll"
    }
}

struct PECounts: Decodable {
    let queued: Int?
    let running: Int?
    let complete24h: Int?
    let dead: Int?
    let failed: Int?

    enum CodingKeys: String, CodingKey {
        case queued, running, dead, failed
        case complete24h = "complete_24h"
    }
}

struct PETerminalJob: Decodable, Identifiable {
    var id: String { jobId }
    let jobId: String
    let status: String
    let topic: String
    let error: String
    let updatedAt: String

    enum CodingKeys: String, CodingKey {
        case status, topic, error
        case jobId = "job_id"
        case updatedAt = "updated_at"
    }
}

struct PECost: Decodable {
    let d24hUsd: Double
    let calls: Int
    let available: Bool

    enum CodingKeys: String, CodingKey {
        case calls, available
        case d24hUsd = "d24h_usd"
    }
}

struct PEBudget: Decodable {
    let target24hUsd: Double
    let crossed: Bool

    enum CodingKeys: String, CodingKey {
        case crossed
        case target24hUsd = "target_24h_usd"
    }
}

struct PEAlert: Decodable, Identifiable {
    var id: String { alertId }
    let alertId: String
    let firstSeen: String
    let lastSeen: String
    let active: Bool

    enum CodingKeys: String, CodingKey {
        case active
        case alertId = "alert_id"
        case firstSeen = "first_seen"
        case lastSeen = "last_seen"
    }
}

struct PEOp: Decodable, Identifiable {
    var id: String { opId }
    let opId: String
    let instance: String
    let kind: String
    let target: String?
    let state: String
    let detail: String?
    let ts: String

    enum CodingKeys: String, CodingKey {
        case instance, kind, target, state, detail, ts
        case opId = "op_id"
    }
}
```

Note counts fields are optional: the unreachable-instance test payload sends `"counts": {}` (empty object, since the engine's `_handle_pe_status` fallback status dict uses `{}` per the engine plan's Task 7). Non-optional `Int` fields would fail to decode against an empty object.

- [ ] **Step 4: Run test to verify it passes**

Run: `xcodebuild test -project ClaudeUsageSystray.xcodeproj -scheme ClaudeUsageSystray -destination 'platform=macOS' 2>&1 | tail -40`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
cd ~/projects/claude-usage-systray
git add claude-usage-systray/claude-usage-systray/Sources/PosterEngineService.swift \
        claude-usage-systray/claude-usage-systray/Tests/PosterEngineServiceTests.swift
git commit -m "feat(pe-supervisor): add PEStatus Codable model for /pe/status"
```

### Task 3: `PosterEngineService` — poll loop, staleness detection, alert dedupe

**Files:**
- Modify: `claude-usage-systray/claude-usage-systray/Sources/PosterEngineService.swift` (append)
- Test: `claude-usage-systray/claude-usage-systray/Tests/PosterEngineServiceTests.swift` (append)

- [ ] **Step 1: Write the failing tests**

Append to `PosterEngineServiceTests.swift`:

```swift
// MARK: - Staleness detection (pure function)

final class PEStalenessTests: XCTestCase {
    func testNotStaleWithinThreeIntervals() {
        let lastPoll = Date().addingTimeInterval(-90)  // 90s ago, interval=60s -> 1.5 intervals
        XCTAssertFalse(isSupervisorStale(lastPoll: lastPoll, pollInterval: 60, now: Date()))
    }

    func testStaleAfterThreeIntervals() {
        let lastPoll = Date().addingTimeInterval(-200)  // 200s ago, interval=60s -> >3 intervals
        XCTAssertTrue(isSupervisorStale(lastPoll: lastPoll, pollInterval: 60, now: Date()))
    }

    func testNilLastPollIsStale() {
        XCTAssertTrue(isSupervisorStale(lastPoll: nil, pollInterval: 60, now: Date()))
    }
}

// MARK: - Alert seen-id dedupe (pure function over injected UserDefaults-like store)

final class PEAlertDedupeTests: XCTestCase {
    func testUnseenActiveAlertIsNewlyUnseen() {
        let seen: Set<String> = []
        let alerts = [PEAlert(alertId: "stalled:dev:active", firstSeen: "t1", lastSeen: "t2", active: true)]
        let unseen = unseenActiveAlertIds(alerts: alerts, seenIds: seen)
        XCTAssertEqual(unseen, ["stalled:dev:active"])
    }

    func testAlreadySeenAlertIsNotReturnedAgain() {
        let seen: Set<String> = ["stalled:dev:active"]
        let alerts = [PEAlert(alertId: "stalled:dev:active", firstSeen: "t1", lastSeen: "t2", active: true)]
        let unseen = unseenActiveAlertIds(alerts: alerts, seenIds: seen)
        XCTAssertTrue(unseen.isEmpty)
    }

    func testInactiveAlertNeverConsideredUnseen() {
        let seen: Set<String> = []
        let alerts = [PEAlert(alertId: "stalled:dev:active", firstSeen: "t1", lastSeen: "t2", active: false)]
        let unseen = unseenActiveAlertIds(alerts: alerts, seenIds: seen)
        XCTAssertTrue(unseen.isEmpty)
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `xcodebuild test -project ClaudeUsageSystray.xcodeproj -scheme ClaudeUsageSystray -destination 'platform=macOS' 2>&1 | tail -40`
Expected: FAIL — `isSupervisorStale`/`unseenActiveAlertIds` do not exist.

- [ ] **Step 3: Write minimal implementation**

Append to `PosterEngineService.swift`:

```swift
// MARK: - Pure helpers (staleness, dedupe)

let peStalenessMultiplier = 3

/// The monitor must monitor itself: nil lastPoll (never polled, or unparseable
/// timestamp from the engine) counts as stale, never as "all green."
func isSupervisorStale(lastPoll: Date?, pollInterval: TimeInterval, now: Date = Date()) -> Bool {
    guard let lastPoll = lastPoll else { return true }
    return now.timeIntervalSince(lastPoll) > pollInterval * Double(peStalenessMultiplier)
}

/// Alert ids the caller hasn't notified for yet. Only active alerts are
/// eligible — a cleared alert is never "newly unseen."
func unseenActiveAlertIds(alerts: [PEAlert], seenIds: Set<String>) -> [String] {
    alerts.filter { $0.active && !seenIds.contains($0.alertId) }.map { $0.alertId }
}

// MARK: - ISO8601 parsing helper (engine emits e.g. "2026-07-21T23:00:00Z")

func parseISO8601(_ string: String?) -> Date? {
    guard let string = string else { return nil }
    let formatter = ISO8601DateFormatter()
    if let date = formatter.date(from: string) { return date }
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    return formatter.date(from: string)
}

// MARK: - PosterEngineService

final class PosterEngineService: ObservableObject {
    static let shared = PosterEngineService()

    @Published private(set) var status: PEStatus?
    @Published private(set) var supervisorStale: Bool = false
    @Published private(set) var error: String?

    private var refreshTimer: Timer?
    private let pollInterval: TimeInterval = 60

    // Injectable for testing
    var urlSession: URLSession = .shared
    var userDefaults: UserDefaults = .standard

    private let seenAlertIdsKey = "PosterEngineService.seenAlertIds"

    private init() {}

    func startPolling() {
        fetchStatus()
        scheduleTimer()
    }

    func stopPolling() {
        refreshTimer?.invalidate()
        refreshTimer = nil
    }

    private func scheduleTimer() {
        refreshTimer?.invalidate()
        refreshTimer = Timer.scheduledTimer(withTimeInterval: pollInterval, repeats: false) { [weak self] _ in
            self?.fetchStatus()
        }
    }

    func fetchStatus() {
        Task {
            guard let url = URL(string: "http://localhost:17420/pe/status") else { return }
            do {
                let (data, response) = try await urlSession.data(from: url)
                guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                    await handleUnreachable()
                    return
                }
                let decoded = try JSONDecoder().decode(PEStatus.self, from: data)
                await MainActor.run {
                    self.status = decoded
                    self.error = nil
                    self.recomputeStaleness()
                    self.notifyUnseenAlerts(decoded.alerts)
                    self.scheduleTimer()
                }
            } catch {
                AppLogger.error("pe", "PE status fetch failed: \(error.localizedDescription)")
                await handleUnreachable()
            }
        }
    }

    @MainActor
    private func handleUnreachable() {
        self.error = "PE supervisor unreachable"
        self.supervisorStale = true
        notifyEngineStaleOnce()
        scheduleTimer()
    }

    @MainActor
    private func recomputeStaleness() {
        guard let status = status else { supervisorStale = true; return }
        let lastPolls = status.instances.compactMap { parseISO8601($0.lastPoll) }
        let oldest = lastPolls.min()
        let stale = isSupervisorStale(lastPoll: oldest, pollInterval: pollInterval)
        if stale && !supervisorStale {
            notifyEngineStaleOnce()
        }
        supervisorStale = stale
    }

    private func notifyEngineStaleOnce() {
        let today = ISO8601DateFormatter().string(from: Date()).prefix(10)
        let seenId = "engine_stale:\(today)"
        var seen = Set(userDefaults.stringArray(forKey: seenAlertIdsKey) ?? [])
        guard !seen.contains(seenId) else { return }
        seen.insert(seenId)
        userDefaults.set(Array(seen), forKey: seenAlertIdsKey)
        Notifier.post(title: "PE Supervisor", body: "Supervisor is stale or unreachable", critical: true)
    }

    @MainActor
    private func notifyUnseenAlerts(_ alerts: [PEAlert]) {
        var seen = Set(userDefaults.stringArray(forKey: seenAlertIdsKey) ?? [])
        let unseen = unseenActiveAlertIds(alerts: alerts, seenIds: seen)
        for alertId in unseen {
            guard let alert = alerts.first(where: { $0.alertId == alertId }) else { continue }
            Notifier.post(title: "PE Supervisor", body: alertDisplayMessage(alert), critical: alertId.hasPrefix("op_failed"))
            seen.insert(alertId)
        }
        if !unseen.isEmpty {
            userDefaults.set(Array(seen), forKey: seenAlertIdsKey)
        }
    }

    private func alertDisplayMessage(_ alert: PEAlert) -> String {
        // alert_id shape: "kind:instance:..." — split for a readable message.
        let parts = alert.alertId.split(separator: ":")
        guard parts.count >= 2 else { return alert.alertId }
        let kind = parts[0]
        let instance = parts[1]
        switch kind {
        case "stalled": return "\(instance): queue stalled, no worker"
        case "budget": return "\(instance): budget crossed"
        case "op_failed": return "\(instance): a control operation failed"
        default: return alert.alertId
        }
    }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `xcodebuild test -project ClaudeUsageSystray.xcodeproj -scheme ClaudeUsageSystray -destination 'platform=macOS' 2>&1 | tail -40`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
cd ~/projects/claude-usage-systray
git add claude-usage-systray/claude-usage-systray/Sources/PosterEngineService.swift \
        claude-usage-systray/claude-usage-systray/Tests/PosterEngineServiceTests.swift
git commit -m "feat(pe-supervisor): add PosterEngineService poller with staleness + alert dedupe"
```

### Task 4: Retry + Kick control methods

**Files:**
- Modify: `claude-usage-systray/claude-usage-systray/Sources/PosterEngineService.swift` (append)
- Test: `claude-usage-systray/claude-usage-systray/Tests/PosterEngineServiceTests.swift` (append)

- [ ] **Step 1: Write the failing test**

Append to `PosterEngineServiceTests.swift`. This uses a mock `URLProtocol` since `URLSession` needs request-inspection, not just a canned response — check whether the repo already has a `URLProtocol` mock before writing a new one:

Run: `grep -rn "URLProtocol" ~/projects/claude-usage-systray/claude-usage-systray/`
Expected: no hits (confirms this is net-new test infrastructure, not a duplicate).

```swift
// MARK: - Retry / Kick control dispatch

private final class MockURLProtocol: URLProtocol {
    static var requestHandler: ((URLRequest) throws -> (HTTPURLResponse, Data))?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let handler = Self.requestHandler else {
            XCTFail("No request handler set")
            return
        }
        do {
            let (response, data) = try handler(request)
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}

final class PEControlDispatchTests: XCTestCase {
    var session: URLSession!
    var service: PosterEngineService!

    override func setUp() {
        super.setUp()
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [MockURLProtocol.self]
        session = URLSession(configuration: config)
        service = PosterEngineService()
        service.urlSession = session
    }

    override func tearDown() {
        MockURLProtocol.requestHandler = nil
        super.tearDown()
    }

    func testRetryPostsToCorrectPathAndParsesOpId() async throws {
        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.url?.path, "/pe/dev/jobs/job-1/retry")
            XCTAssertEqual(request.httpMethod, "POST")
            let body = try JSONSerialization.data(withJSONObject: ["accepted": true, "op_id": "op-99"])
            let resp = HTTPURLResponse(url: request.url!, statusCode: 202, httpVersion: nil, headerFields: nil)!
            return (resp, body)
        }
        let opId = try await service.retry(instance: "dev", jobId: "job-1")
        XCTAssertEqual(opId, "op-99")
    }

    func testKickPostsToCorrectPath() async throws {
        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.url?.path, "/pe/dev/worker/kick")
            let body = try JSONSerialization.data(withJSONObject: ["accepted": true, "op_id": "op-100"])
            let resp = HTTPURLResponse(url: request.url!, statusCode: 202, httpVersion: nil, headerFields: nil)!
            return (resp, body)
        }
        let opId = try await service.kick(instance: "dev")
        XCTAssertEqual(opId, "op-100")
    }

    func testKickRateLimited429ThrowsDescriptiveError() async throws {
        MockURLProtocol.requestHandler = { request in
            let resp = HTTPURLResponse(url: request.url!, statusCode: 429, httpVersion: nil, headerFields: nil)!
            return (resp, Data("{}".utf8))
        }
        do {
            _ = try await service.kick(instance: "dev")
            XCTFail("expected throw")
        } catch let error as PEControlError {
            XCTAssertEqual(error, .rateLimited)
        }
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `xcodebuild test -project ClaudeUsageSystray.xcodeproj -scheme ClaudeUsageSystray -destination 'platform=macOS' 2>&1 | tail -40`
Expected: FAIL — `service.retry`/`service.kick`/`PEControlError` do not exist.

- [ ] **Step 3: Write minimal implementation**

Append to `PosterEngineService.swift`:

```swift
// MARK: - Controls (retry / kick)

enum PEControlError: Error, Equatable {
    case unknownInstanceOrJob
    case rateLimited
    case httpError(Int)
    case network(String)

    static func == (lhs: PEControlError, rhs: PEControlError) -> Bool {
        switch (lhs, rhs) {
        case (.unknownInstanceOrJob, .unknownInstanceOrJob), (.rateLimited, .rateLimited):
            return true
        case let (.httpError(a), .httpError(b)):
            return a == b
        case let (.network(a), .network(b)):
            return a == b
        default:
            return false
        }
    }
}

private struct PEControlResponse: Decodable {
    let accepted: Bool
    let opId: String

    enum CodingKeys: String, CodingKey {
        case accepted
        case opId = "op_id"
    }
}

extension PosterEngineService {

    /// POST /pe/<instance>/jobs/<jobId>/retry — returns the accepted op_id.
    func retry(instance: String, jobId: String) async throws -> String {
        try await postControl(path: "/pe/\(instance)/jobs/\(jobId)/retry")
    }

    /// POST /pe/<instance>/worker/kick — returns the accepted op_id.
    func kick(instance: String) async throws -> String {
        try await postControl(path: "/pe/\(instance)/worker/kick")
    }

    private func postControl(path: String) async throws -> String {
        guard let url = URL(string: "http://localhost:17420\(path)") else {
            throw PEControlError.network("bad URL")
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = Data("{}".utf8)

        let (data, response): (Data, URLResponse)
        do {
            (data, response) = try await urlSession.data(for: request)
        } catch {
            throw PEControlError.network(error.localizedDescription)
        }

        guard let http = response as? HTTPURLResponse else {
            throw PEControlError.network("no HTTP response")
        }
        switch http.statusCode {
        case 202:
            let decoded = try JSONDecoder().decode(PEControlResponse.self, from: data)
            return decoded.opId
        case 404:
            throw PEControlError.unknownInstanceOrJob
        case 429:
            throw PEControlError.rateLimited
        default:
            throw PEControlError.httpError(http.statusCode)
        }
    }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `xcodebuild test -project ClaudeUsageSystray.xcodeproj -scheme ClaudeUsageSystray -destination 'platform=macOS' 2>&1 | tail -40`
Expected: PASS (all tests)

- [ ] **Step 5: Commit**

```bash
cd ~/projects/claude-usage-systray
git add claude-usage-systray/claude-usage-systray/Sources/PosterEngineService.swift \
        claude-usage-systray/claude-usage-systray/Tests/PosterEngineServiceTests.swift
git commit -m "feat(pe-supervisor): add retry/kick control dispatch with 404/429 error mapping"
```

---

## Chunk 3: Popover PE section + wiring

### Task 5: Start polling from AppDelegate

**Files:**
- Modify: `claude-usage-systray/claude-usage-systray/Sources/AppDelegate.swift`

- [ ] **Step 1: Add the service property and start/stop calls**

Add near the existing `usageService`/`settingsManager` properties (after line 10):
```swift
    private let posterEngineService = PosterEngineService.shared
```

In `applicationDidFinishLaunching` (after line 31's `startUsagePolling()`):
```swift
        posterEngineService.startPolling()
```

In `applicationWillTerminate` (after line 67's `usageService.stopPolling()`):
```swift
        posterEngineService.stopPolling()
```

Pass the service into `MenuBarView`'s init in `setupPopover()` (line 85-90) — see Task 6 for the corresponding `MenuBarView` signature change; both edits must land together since this call site won't compile until Task 6's init parameter exists.

- [ ] **Step 2: Build only (no new tests — this is wiring, covered by Task 6's manual smoke check)**

Run: `xcodebuild build -project ClaudeUsageSystray.xcodeproj -scheme ClaudeUsageSystray -destination 'platform=macOS' 2>&1 | tail -40`
Expected: build FAILS until Task 6 lands (MenuBarView doesn't accept the new param yet) — this is expected; do Task 5 and Task 6 as one commit rather than two, since they're two halves of one wiring change that doesn't compile independently.

### Task 6: `peSection` in MenuBarView

**Files:**
- Modify: `claude-usage-systray/claude-usage-systray/Sources/MenuBarView.swift`

- [ ] **Step 1: Add the service property and wire it into `body`**

Add near line 4-5's existing `@ObservedObject` properties:
```swift
    @ObservedObject var posterEngineService: PosterEngineService
```

Update `AppDelegate.swift`'s `setupPopover()` (from Task 5) to pass it:
```swift
            rootView: MenuBarView(
                usageService: usageService,
                settingsManager: settingsManager,
                posterEngineService: posterEngineService
            )
```

Insert `peSection` into `body` (line 12-38), between `modelBreakdown` and the second `Divider`:
```swift
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            usageHeader

            Divider()
                .padding(.vertical, 4)

            shortcutsSection

            modelBreakdown

            peSection

            Divider()
                .padding(.vertical, 4)

            actionButtons

            Divider()
                .padding(.vertical, 4)

            quitButton
        }
        .padding(.vertical, 8)
        .frame(minWidth: 200)
        .sheet(isPresented: $showSettings) {
            SettingsView(settingsManager: settingsManager, usageService: usageService)
        }
    }
```

- [ ] **Step 2: Write the `peSection` view**

Add as a new private computed property (following `modelBreakdown`'s conditional pattern at lines 90-105):

```swift
    @State private var expandedPEInstance: String? = nil

    @ViewBuilder
    private var peSection: some View {
        if let status = posterEngineService.status, !status.instances.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                if posterEngineService.supervisorStale {
                    HStack {
                        Image(systemName: "exclamationmark.triangle.fill")
                            .foregroundColor(.orange)
                        Text("PE supervisor stale")
                            .font(.caption)
                            .foregroundColor(.secondary)
                    }
                    .padding(.horizontal, 12)
                }
                ForEach(status.instances, id: \.name) { instance in
                    peInstanceRow(instance)
                }
            }
            .padding(.vertical, 2)
        }
    }

    @ViewBuilder
    private func peInstanceRow(_ instance: PEInstanceStatus) -> some View {
        let isExpanded = expandedPEInstance == instance.name
        VStack(alignment: .leading, spacing: 2) {
            Button(action: {
                expandedPEInstance = isExpanded ? nil : instance.name
            }) {
                HStack {
                    Image(systemName: instance.reachable ? "checkmark.circle.fill" : "xmark.circle.fill")
                        .foregroundColor(peInstanceColor(instance))
                        .font(.caption)
                    Text(instance.name)
                        .font(.caption)
                        .fontWeight(.medium)
                    Text("\(instance.counts.running ?? 0) running")
                        .font(.caption2)
                        .foregroundColor(.secondary)
                    Spacer()
                    Text(String(format: "$%.2f / $%.2f", instance.cost.d24hUsd, instance.budget.target24hUsd))
                        .font(.caption2)
                        .foregroundColor(instance.budget.crossed ? .red : .secondary)
                }
            }
            .buttonStyle(.plain)

            if isExpanded || instance.stalled {
                if instance.stalled {
                    Button("Kick worker") {
                        dispatchKick(instance: instance.name)
                    }
                    .font(.caption2)
                    .padding(.leading, 20)
                }
                ForEach(instance.recentTerminal) { job in
                    HStack {
                        Text("\(job.status): \(job.topic)")
                            .font(.caption2)
                            .foregroundColor(.secondary)
                            .lineLimit(1)
                        Spacer()
                        Button("Retry") {
                            dispatchRetry(instance: instance.name, jobId: job.jobId)
                        }
                        .font(.caption2)
                    }
                    .padding(.leading, 20)
                }
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 2)
    }

    private func peInstanceColor(_ instance: PEInstanceStatus) -> Color {
        if !instance.reachable || instance.stalled || instance.budget.crossed { return .red }
        return .green
    }

    private func dispatchRetry(instance: String, jobId: String) {
        Task {
            do {
                _ = try await posterEngineService.retry(instance: instance, jobId: jobId)
            } catch {
                AppLogger.error("pe", "Retry dispatch failed: \(error)")
            }
        }
    }

    private func dispatchKick(instance: String) {
        Task {
            do {
                _ = try await posterEngineService.kick(instance: instance)
            } catch {
                AppLogger.error("pe", "Kick dispatch failed: \(error)")
            }
        }
    }
```

Note: menu-bar red-tint-on-active-alert (spec line 270) is deliberately deferred to Task 7, not folded in here — it touches `AppDelegate.updateStatusItemAppearance()`, a different file/concern than the popover section itself.

- [ ] **Step 3: Build and run the full test suite**

Run:
```bash
cd ~/projects/claude-usage-systray/claude-usage-systray
xcodebuild test -project ClaudeUsageSystray.xcodeproj -scheme ClaudeUsageSystray -destination 'platform=macOS' 2>&1 | tail -60
```
Expected: build succeeds (this is the point where Task 5's dangling reference resolves), all tests PASS.

- [ ] **Step 4: Manual smoke check — REQUIRED, not optional**

Per this project's "complete the loop" workstyle rule, a UI change isn't done until verified running. Launch the app (Xcode Run or the built `.app`), open the popover, and confirm:
- The PE section renders (requires the engine to actually be running on :17420 with `pe_instances.json` configured — if not yet operator-provisioned, the section correctly stays empty/hidden per the `!status.instances.isEmpty` guard, which is itself worth confirming doesn't crash).
- If PE dev (:9120) is live and instances are configured, the row shows real counts/cost.
- Clicking a row expands/collapses.

If the engine side isn't fully operator-configured yet (per the engine plan's Task 6 Step 3's `pe_instances.json` + Keychain provisioning note), it's acceptable to verify only the empty-state rendering here and flag the live-data check as deferred to the E2E task (Task 8).

- [ ] **Step 5: Commit**

```bash
cd ~/projects/claude-usage-systray
git add claude-usage-systray/claude-usage-systray/Sources/MenuBarView.swift \
        claude-usage-systray/claude-usage-systray/Sources/AppDelegate.swift
git commit -m "feat(pe-supervisor): add PE popover section with retry/kick controls"
```

### Task 7: Menu-bar red tint on active alert

**Files:**
- Modify: `claude-usage-systray/claude-usage-systray/Sources/AppDelegate.swift`

- [ ] **Step 1: Add alert-awareness to the status item icon**

`updateStatusItemAppearance()` (lines 139-177) currently colors based only on `UsageService` thresholds. Add a PE-alert check that overrides to red regardless of usage state. Insert near the top of the method (after line 140's `guard let button = ...`):

```swift
        let peHasActiveAlert = posterEngineService.status?.alerts.contains(where: { $0.active }) ?? false
```

In the compact-display branch (lines 145-159) and the icon branch (lines 160-176), the simplest integration point given the existing branching is to force red when `peHasActiveAlert` — for the icon branch specifically, override `symbolName` selection:

```swift
        } else {
            let config = NSImage.SymbolConfiguration(pointSize: 12, weight: .medium)
            let symbolName: String
            if peHasActiveAlert || weekUsage >= 80 { symbolName = "exclamationmark.triangle.fill" }
            else if weekUsage >= 50 { symbolName = "chart.pie.fill" }
            else { symbolName = "chart.pie" }

            button.image = NSImage(systemSymbolName: symbolName, accessibilityDescription: "Claude Usage")?
                .withSymbolConfiguration(config)
            button.attributedTitle = NSAttributedString(
                string: "\(weekUsage)%",
                attributes: [
                    .font: NSFont.monospacedDigitSystemFont(ofSize: 11, weight: .medium),
                    .foregroundColor: peHasActiveAlert ? NSColor.systemRed : usageColor(for: weekUsage)
                ]
            )
        }
```

For the compact-display branch, this plan leaves the existing 5h/7d percentage display untouched (spec only calls for "red tint on the existing icon," and the compact mode has no single icon to tint — flag this as a scoping note for review, not a silent gap: compact-display users won't see the PE alert tint from the menu bar icon alone, only from the popover section itself).

Also hook the PE service's published changes into the existing Combine pipeline so the icon updates live. In `applicationDidFinishLaunching`, after the existing `usageService.$currentUsage` subscription (lines 36-42), add:

```swift
        posterEngineService.$status
            .receive(on: RunLoop.main)
            .sink { [weak self] _ in
                self?.updateStatusItemAppearance()
            }
            .store(in: &cancellables)
```

- [ ] **Step 2: Build and run the full test suite**

Run: `xcodebuild test -project ClaudeUsageSystray.xcodeproj -scheme ClaudeUsageSystray -destination 'platform=macOS' 2>&1 | tail -40`
Expected: PASS

- [ ] **Step 3: Manual smoke check**

Confirm the menu-bar icon does NOT change when there's no active PE alert (regression check — this touches shared icon logic that also drives the existing usage-threshold coloring).

- [ ] **Step 4: Commit**

```bash
cd ~/projects/claude-usage-systray
git add claude-usage-systray/claude-usage-systray/Sources/AppDelegate.swift
git commit -m "feat(pe-supervisor): tint menu-bar icon red while a PE alert is active"
```

---

## Chunk 4: E2E + wrap-up

### Task 8: Manual E2E script (per spec's Testing Strategy)

**Files:** none — this is a manual verification task, not code.

- [ ] **Step 1: Run the spec's stated E2E sequence against dev**

Per the spec (`Testing Strategy` section): enqueue a PE job → kill the dev worker (`launchctl bootout gui/$(id -u)/com.poster-worker`) → observe `stalled` flip to true within ~4 minutes in the popover → click Kick worker → observe recovery → let orphan-reclaim convert a stuck job to `failed` (or manually force one via the PE admin API if available) → click Retry → observe it reach `complete`.

Document the actual observed timings and outcomes — this satisfies AC-1/AC-2/AC-3 from the spec, which are explicitly E2E-only criteria the unit tests in this plan and the engine plan cannot cover.

- [ ] **Step 2: Re-bootstrap com.poster-worker if this test killed it**

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.poster-worker.plist
```

Dev must not be left worker-less after this manual test.

- [ ] **Step 3: Report results, do not commit code for this task**

This task's output is a verification report (in chat / the eventual PR description), not a git commit.

---

## Known deviations / open items for review

1. **Windows porting note** (spec line 168): out of scope for this plan entirely — Swift/macOS only. The spec's own scope already limits this to a "porting note" in a guide, which isn't part of either implementation plan; flag as a documentation follow-up if not already tracked elsewhere.
2. **Compact-display menu-bar tint gap** (Task 7, Step 1): compact mode shows raw 5h/7d percentages with no single icon to red-tint; PE alert visibility in that mode is popover-only. Confirm with the operator whether this is acceptable or needs a follow-up (e.g. a small red dot overlay).
3. **`PosterEngineService.userDefaults` and `PEStatus` empty-instances guard**: if `pe_instances.json` is never configured (engine plan's Task 6 Step 3 dependency), `/pe/status` still returns `{"instances": [], "alerts": [], "ops": []}` per the engine's `_handle_pe_status` (iterating an empty `pe_instances` list) — the popover's `!status.instances.isEmpty` guard means the whole PE section silently stays hidden rather than showing a "not configured" message. Acceptable for v1 per the spec's minimal scope, but worth a one-line mention in the operator resume checklist for the next session.
