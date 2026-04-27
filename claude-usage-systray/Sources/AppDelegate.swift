import AppKit
import SwiftUI
import UserNotifications
import Combine

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var popover: NSPopover!
    private let usageService = UsageService.shared
    private let settingsManager = SettingsManager.shared

    private var lastWarningNotified: Int = 0
    private var lastCriticalNotified: Int = 0

    // Keep Combine subscriptions alive
    private var cancellables = Set<AnyCancellable>()

    // Python engine process management
    private var engineProcess: Process?
    private var healthCheckTimer: Timer?
    private let enginePort = 17420
    private var spawnFailureCount = 0
    private var lastSpawnAttempt: Date = .distantPast
    private let maxSpawnBackoff: TimeInterval = 300  // 5 minutes cap

    func applicationDidFinishLaunching(_ notification: Notification) {
        setupStatusItem()
        setupPopover()
        setupNotifications()
        HermesClient.requestNotificationPermission()
        startUsagePolling()
        spawnEngineProcess()
        startHealthCheck()

        // Observe usage changes to keep the menu bar numbers up to date
        usageService.$currentUsage
            .receive(on: RunLoop.main)
            .sink { [weak self] _ in
                self?.updateStatusItemAppearance()
                self?.checkForNotifications()
            }
            .store(in: &cancellables)
        
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(usageDidUpdate),
            name: NSNotification.Name("UsageDidUpdate"),
            object: nil
        )
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(closePopover),
            name: NSApplication.didResignActiveNotification,
            object: nil
        )
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(settingsDidChange),
            name: UserDefaults.didChangeNotification,
            object: nil
        )
    }

    func applicationWillTerminate(_ notification: Notification) {
        stopEngineProcess()
        healthCheckTimer?.invalidate()
        usageService.stopPolling()
    }

    private func setupStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        
        if let button = statusItem.button {
            button.image = NSImage(systemSymbolName: "chart.pie.fill", accessibilityDescription: "Claude Usage")
            button.action = #selector(togglePopover)
            button.target = self
        }
    }

    private func setupPopover() {
        popover = NSPopover()
        popover.contentSize = NSSize(width: 240, height: 200)
        popover.behavior = .transient
        popover.animates = true
        popover.contentViewController = NSHostingController(
            rootView: MenuBarView(
                usageService: usageService,
                settingsManager: settingsManager
            )
        )
    }

    private func setupNotifications() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { granted, error in
            if let error = error {
                AppLogger.error("general", "Notification authorization error: \(error)")
            }
        }
    }

    private func startUsagePolling() {
        if settingsManager.settings.isConfigured {
            usageService.startPolling()
        }
        
        Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            self?.checkForNotifications()
        }
    }

    @objc private func togglePopover() {
        if popover.isShown {
            closePopover()
        } else {
            showPopover()
        }
    }

    private func showPopover() {
        if let button = statusItem.button {
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
            NSApp.activate(ignoringOtherApps: true)
        }
    }

    @objc private func closePopover() {
        popover.performClose(nil)
    }

    @objc private func settingsDidChange() {
        updateStatusItemAppearance()
    }

    @objc private func usageDidUpdate() {
        updateStatusItemAppearance()
        checkForNotifications()
    }

    private func updateStatusItemAppearance() {
        guard let button = statusItem.button else { return }

        let snapshot = usageService.currentUsage
        let weekUsage = snapshot.sevenDayUtilization

        if settingsManager.settings.compactDisplay {
            let fiveH = snapshot.fiveHourUtilization
            let sevenD = snapshot.sevenDayUtilization
            let font = NSFont.monospacedDigitSystemFont(ofSize: 11, weight: .medium)

            let str = NSMutableAttributedString()
            str.append(NSAttributedString(string: "\(fiveH)%",
                attributes: [.font: font, .foregroundColor: usageColor(for: fiveH)]))
            str.append(NSAttributedString(string: " · ",
                attributes: [.font: font, .foregroundColor: NSColor.secondaryLabelColor]))
            str.append(NSAttributedString(string: "\(sevenD)%",
                attributes: [.font: font, .foregroundColor: usageColor(for: sevenD)]))

            button.image = nil
            button.attributedTitle = str
        } else {
            let config = NSImage.SymbolConfiguration(pointSize: 12, weight: .medium)
            let symbolName: String
            if weekUsage >= 80 { symbolName = "exclamationmark.triangle.fill" }
            else if weekUsage >= 50 { symbolName = "chart.pie.fill" }
            else { symbolName = "chart.pie" }

            button.image = NSImage(systemSymbolName: symbolName, accessibilityDescription: "Claude Usage")?
                .withSymbolConfiguration(config)
            button.attributedTitle = NSAttributedString(
                string: "\(weekUsage)%",
                attributes: [
                    .font: NSFont.monospacedDigitSystemFont(ofSize: 11, weight: .medium),
                    .foregroundColor: usageColor(for: weekUsage)
                ]
            )
        }
    }

    private func usageColor(for percentage: Int) -> NSColor {
        let criticalThreshold = Int(settingsManager.settings.criticalThreshold)
        let warningThreshold = Int(settingsManager.settings.warningThreshold)
        if percentage >= criticalThreshold {
            return .systemRed
        } else if percentage >= warningThreshold {
            return .systemOrange
        }
        return .labelColor
    }

    private func checkForNotifications() {
        guard settingsManager.settings.notificationsEnabled else { return }
        
        let usage = usageService.currentUsage.sevenDayUtilization
        let warningThreshold = Int(settingsManager.settings.warningThreshold)
        let criticalThreshold = Int(settingsManager.settings.criticalThreshold)

        if usage >= criticalThreshold && lastCriticalNotified < criticalThreshold {
            sendNotification(
                title: "Critical: Claude Usage",
                body: "You've used \(usage)% of your weekly quota. Consider pausing non-essential tasks.",
                isCritical: true
            )
            lastCriticalNotified = criticalThreshold
        } else if usage >= warningThreshold && lastWarningNotified < warningThreshold && usage < criticalThreshold {
            sendNotification(
                title: "Warning: Claude Usage",
                body: "You've used \(usage)% of your weekly quota.",
                isCritical: false
            )
            lastWarningNotified = warningThreshold
        }
    }

    private func sendNotification(title: String, body: String, isCritical: Bool) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = isCritical ? .defaultCritical : .default

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

    // MARK: - Python Engine Process Management

    /// Check if the engine is already running externally (e.g. via launchd).
    private func isEngineAlreadyRunning() -> Bool {
        guard let url = URL(string: "http://localhost:\(enginePort)/api/health") else { return false }
        var request = URLRequest(url: url)
        request.timeoutInterval = 2
        let semaphore = DispatchSemaphore(value: 0)
        var alive = false
        URLSession.shared.dataTask(with: request) { _, response, _ in
            if let http = response as? HTTPURLResponse, http.statusCode == 200 {
                alive = true
            }
            semaphore.signal()
        }.resume()
        _ = semaphore.wait(timeout: .now() + 3)
        return alive
    }

    private func spawnEngineProcess() {
        // Skip if engine is already running externally (standalone launchd service)
        if isEngineAlreadyRunning() {
            AppLogger.info("engine", "Engine already running on port \(enginePort), skipping spawn")
            return
        }

        lastSpawnAttempt = Date()
        guard let token = try? readOAuthCredentials().accessToken else {
            spawnFailureCount += 1
            let backoff = min(Double(spawnFailureCount) * 60.0, maxSpawnBackoff)
            AppLogger.error("engine", "Cannot read OAuth token (attempt \(spawnFailureCount)), next retry in \(Int(backoff))s")
            return
        }
        spawnFailureCount = 0

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")

        // Resolve engine directory: prefer next to .app bundle, fall back to source repo.
        let bundlePath = Bundle.main.bundlePath
        let bundleParent = (bundlePath as NSString).deletingLastPathComponent
        let engineDirCandidates = [
            bundleParent,  // production: engine/ copied next to .app
            "/Users/jcords-macmini/projects/claude-usage-systray",  // dev: source repo
        ]
        let engineDir = engineDirCandidates.first {
            FileManager.default.fileExists(atPath: "\($0)/engine")
        } ?? bundleParent

        AppLogger.info("engine", "Engine working dir: \(engineDir)")
        process.arguments = [
            "python3", "-m", "engine.server",
            "--port", "\(enginePort)",
            "--token", token
        ]
        process.currentDirectoryURL = URL(fileURLWithPath: engineDir)

        // Capture stderr for debugging, discard stdout (no more stdout signaling)
        process.standardError = FileHandle.standardError
        process.standardOutput = FileHandle.nullDevice

        do {
            try process.run()
            engineProcess = process
            AppLogger.info("engine", "Engine started on port \(enginePort), PID \(process.processIdentifier)")
        } catch {
            AppLogger.error("engine", "Failed to start engine: \(error)")
        }
    }

    private func stopEngineProcess() {
        guard let process = engineProcess, process.isRunning else {
            engineProcess = nil
            return
        }
        process.terminate() // SIGTERM
        DispatchQueue.global().asyncAfter(deadline: .now() + 2) { [weak self] in
            if let p = self?.engineProcess, p.isRunning {
                kill(p.processIdentifier, SIGKILL)
            }
        }
        engineProcess = nil
    }

    /// Post a fresh OAuth token to the engine's hot-swap endpoint.
    private func hotSwapEngineToken() {
        guard let token = try? readOAuthCredentials().accessToken else {
            AppLogger.error("engine", "Cannot read OAuth token for hot-swap")
            return
        }
        guard let url = URL(string: "http://localhost:\(enginePort)/api/token") else { return }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["token": token])
        request.timeoutInterval = 5

        URLSession.shared.dataTask(with: request) { data, response, error in
            if let error = error {
                AppLogger.error("engine", "Token hot-swap failed: \(error.localizedDescription)")
                return
            }
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            if status == 200 {
                AppLogger.info("engine", "Token hot-swapped successfully")
            } else {
                let body = data.flatMap { String(data: $0, encoding: .utf8) } ?? ""
                AppLogger.error("engine", "Token hot-swap HTTP \(status): \(String(body.prefix(200)))")
            }
        }.resume()
    }

    private func startHealthCheck() {
        healthCheckTimer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            guard let self = self else { return }

            // If engine is running externally (launchd), just check token health
            if self.engineProcess == nil && self.isEngineAlreadyRunning() {
                self.checkEngineHealth()
                return
            }

            // If process died, respawn it
            if let process = self.engineProcess, !process.isRunning {
                AppLogger.error("engine", "Engine process died, respawning")
                self.engineProcess = nil
                self.spawnEngineProcess()
                return
            }

            if self.engineProcess == nil {
                // Engine not running — either never started or spawn failed
                let backoff = min(Double(max(self.spawnFailureCount, 1)) * 60.0, self.maxSpawnBackoff)
                let elapsed = Date().timeIntervalSince(self.lastSpawnAttempt)
                if elapsed >= backoff {
                    AppLogger.info("engine", "Retrying engine spawn after \(Int(elapsed))s (\(self.spawnFailureCount) prior failures)")
                    self.spawnEngineProcess()
                }
                return
            }

            // Engine is alive — check if it needs a token refresh
            self.checkEngineHealth()
        }
    }

    /// Poll /api/health and hot-swap the token if the engine reports it needs refresh.
    private func checkEngineHealth() {
        guard let url = URL(string: "http://localhost:\(enginePort)/api/health") else { return }
        URLSession.shared.dataTask(with: url) { [weak self] data, _, error in
            guard error == nil, let data = data,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                return
            }
            if json["token_needs_refresh"] as? Bool == true {
                AppLogger.info("engine", "Engine reports token needs refresh, hot-swapping")
                self?.hotSwapEngineToken()
            }
        }.resume()
    }
}
