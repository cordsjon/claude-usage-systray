import AppKit
import SwiftUI
import UserNotifications
import Combine

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var popover: NSPopover!
    private let usageService = UsageService.shared
    private let settingsManager = SettingsManager.shared
    private let posterEngineService = PosterEngineService.shared

    private var lastWarningNotified: Int = 0
    private var lastCriticalNotified: Int = 0

    // Keep Combine subscriptions alive
    private var cancellables = Set<AnyCancellable>()

    // Python engine process management
    private var engineProcess: Process?
    private var healthCheckTimer: Timer?
    private let enginePort = EngineConfig.port
    private var spawnFailureCount = 0
    private var lastSpawnAttempt: Date = .distantPast
    private let maxSpawnBackoff: TimeInterval = 300  // 5 minutes cap

    func applicationDidFinishLaunching(_ notification: Notification) {
        setupStatusItem()
        setupPopover()
        Notifier.requestAuthorization()
        startUsagePolling()
        posterEngineService.startPolling()
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

        posterEngineService.$status
            .receive(on: RunLoop.main)
            .sink { [weak self] _ in
                self?.updateStatusItemAppearance()
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
        posterEngineService.stopPolling()
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
                settingsManager: settingsManager,
                posterEngineService: posterEngineService
            )
        )
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

        let peHasActiveAlert = posterEngineService.status?.alerts.contains(where: { $0.active }) ?? false
        let snapshot = usageService.currentUsage
        let weekUsage = snapshot.sevenDayUtilization

        if settingsManager.settings.compactDisplay {
            let fiveH = min(snapshot.fiveHourUtilization, 100)
            let sevenD = min(snapshot.sevenDayUtilization, 100)
            let font = NSFont.monospacedDigitSystemFont(ofSize: 11, weight: .medium)

            let str = NSMutableAttributedString()
            str.append(NSAttributedString(string: "\(fiveH)%",
                attributes: [.font: font, .foregroundColor: usageColor(for: snapshot.fiveHourUtilization)]))
            str.append(NSAttributedString(string: " · ",
                attributes: [.font: font, .foregroundColor: NSColor.secondaryLabelColor]))
            str.append(NSAttributedString(string: "\(sevenD)%",
                attributes: [.font: font, .foregroundColor: usageColor(for: snapshot.sevenDayUtilization)]))

            button.image = nil
            button.attributedTitle = str
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
            Notifier.post(
                title: "Critical: Claude Usage",
                body: "You've used \(usage)% of your weekly quota. Consider pausing non-essential tasks.",
                critical: true
            )
            lastCriticalNotified = criticalThreshold
        } else if usage >= warningThreshold && lastWarningNotified < warningThreshold && usage < criticalThreshold {
            Notifier.post(
                title: "Warning: Claude Usage",
                body: "You've used \(usage)% of your weekly quota."
            )
            lastWarningNotified = warningThreshold
        }

        // Reset notified state when usage drops back below thresholds (e.g. after weekly reset)
        if usage < warningThreshold {
            lastWarningNotified = 0
            lastCriticalNotified = 0
        } else if usage < criticalThreshold {
            lastCriticalNotified = 0
        }
    }

    // MARK: - Python Engine Process Management

    /// Check if the engine is already running externally (e.g. via launchd).
    /// True if something is already listening on the engine port.
    ///
    /// Uses a raw TCP connect (the kernel answers instantly) rather than the
    /// HTTP health check below, which has a 2s timeout and can false-negative
    /// while the engine is doing heavy work (scanning thousands of session
    /// files, codeburn). A false "engine is down" makes the supervisor spawn a
    /// duplicate that cannot bind the port held by the launchd-managed engine,
    /// dies, and respawns every 60s — re-reading the Keychain each time.
    ///
    /// Probes `EngineConfig.host`, not a hardcoded 127.0.0.1: the engine binds
    /// a single interface address (the tailnet IP by default), so a loopback
    /// probe reports "down" for a perfectly healthy engine and triggers exactly
    /// the respawn loop described above.
    ///
    /// Uses `getaddrinfo` rather than `inet_addr` because the configured host
    /// may be a name ("localhost") and not a dotted-quad literal; `inet_addr`
    /// returns INADDR_NONE for names, which would silently probe 255.255.255.255.
    private func isEnginePortInUse() -> Bool {
        var hints = addrinfo()
        hints.ai_family = AF_UNSPEC       // accept IPv4 or IPv6
        hints.ai_socktype = SOCK_STREAM

        var info: UnsafeMutablePointer<addrinfo>?
        guard getaddrinfo(EngineConfig.host, "\(enginePort)", &hints, &info) == 0,
              let head = info else {
            return false
        }
        defer { freeaddrinfo(info) }

        // A host can resolve to several addresses; the port is in use if any
        // of them accepts a connection.
        var candidate: UnsafeMutablePointer<addrinfo>? = head
        while let entry = candidate {
            let fd = socket(entry.pointee.ai_family,
                            entry.pointee.ai_socktype,
                            entry.pointee.ai_protocol)
            if fd >= 0 {
                let rc = connect(fd, entry.pointee.ai_addr, entry.pointee.ai_addrlen)
                close(fd)
                if rc == 0 { return true }
            }
            candidate = entry.pointee.ai_next
        }
        return false
    }

    private func isEngineAlreadyRunning() -> Bool {
        guard let url = EngineConfig.url("/api/health") else { return false }
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
        // Skip if the port is already owned (e.g. the launchd-managed engine).
        // Checked first, and before reading the Keychain, because a reliable
        // port check here is what prevents the duplicate-spawn respawn loop.
        if isEnginePortInUse() {
            AppLogger.info("engine", "Port \(enginePort) already in use, skipping spawn")
            return
        }

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

        // The engine needs Python >= 3.10 (engine/db.py uses PEP 604 `float | None`
        // annotations). A GUI app inherits a PATH where /usr/bin/env python3 can
        // resolve to Xcode's bundled Python 3.9, which raises
        //   TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'
        // at import time and crash-loops. Resolve an explicit interpreter instead
        // of trusting PATH; fall back to bare "python3" so a machine without
        // Homebrew still behaves as before rather than failing outright.
        let interpreterCandidates = [
            "/opt/homebrew/bin/python3",   // Apple silicon Homebrew
            "/usr/local/bin/python3",      // Intel Homebrew
        ]
        let pythonExecutable = interpreterCandidates.first {
            FileManager.default.isExecutableFile(atPath: $0)
        } ?? "python3"
        AppLogger.info("engine", "Engine interpreter: \(pythonExecutable)")

        // Resolve engine directory: prefer next to .app bundle, fall back to source repo.
        let bundlePath = Bundle.main.bundlePath
        let bundleParent = (bundlePath as NSString).deletingLastPathComponent
        let engineDirCandidates = [
            bundleParent,  // production: engine/ copied next to .app
            NSHomeDirectory() + "/projects/claude-usage-systray",  // dev: source repo
        ]
        let engineDir = engineDirCandidates.first {
            FileManager.default.fileExists(atPath: "\($0)/engine")
        } ?? bundleParent

        AppLogger.info("engine", "Engine working dir: \(engineDir)")
        process.arguments = [
            pythonExecutable, "-m", "engine.server",
            "--port", "\(enginePort)",
        ]
        // Pass the OAuth token via the environment, not argv, so it does not
        // appear in `ps` output (process arguments are world-readable).
        var env = ProcessInfo.processInfo.environment
        env["CLAUDE_OAUTH_TOKEN"] = token
        // Bind the child engine to the same host this app polls. Without this the
        // engine would fall back to its own default in engine/api.py and could
        // bind an address we never talk to — the drift this refactor removes.
        env["ENGINE_HOST"] = EngineConfig.host
        process.environment = env
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
        guard let url = EngineConfig.url("/api/token") else { return }
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
        guard let url = EngineConfig.url("/api/health") else { return }
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
