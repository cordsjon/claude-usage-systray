import SwiftUI

struct MenuBarView: View {
    @ObservedObject var usageService: UsageService
    @ObservedObject var settingsManager: SettingsManager
    @State private var showSettings = false
    @State private var showDashboard = false
    @State private var projectionLine: String = ""
    @State private var shortcutsConfig: WorkspaceConfig? = nil
    private let enginePort = 17420

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            usageHeader
            
            Divider()
                .padding(.vertical, 4)

            shortcutsSection

            modelBreakdown

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

    private var usageHeader: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Image(systemName: usageIconName)
                    .foregroundColor(usageColor)
                Text("5hr: \(usageService.currentUsage.fiveHourUtilization)%")
                    .fontWeight(.medium)
                Spacer()
                if let timeLeft = usageService.currentUsage.fiveHourResetIn {
                    Text(timeLeft)
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
            }

            HStack {
                Image(systemName: "calendar")
                    .foregroundColor(weeklyColor)
                Text("Week: \(usageService.currentUsage.sevenDayUtilization)%")
                    .fontWeight(.medium)
                Spacer()
                if let timeLeft = usageService.currentUsage.sevenDayResetIn {
                    Text(timeLeft)
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
            }

            if !projectionLine.isEmpty {
                Text(projectionLine)
                    .font(.caption)
                    .foregroundColor(.cyan)
                    .padding(.top, 2)
            }

            if let error = usageService.error {
                Text(error)
                    .font(.caption)
                    .foregroundColor(.red)
                    .lineLimit(2)
            } else if usageService.isLoading {
                ProgressView()
                    .scaleEffect(0.5)
                    .frame(height: 10)
            }
        }
        .padding(.horizontal, 12)
        .onAppear { fetchProjection() }
    }

    private var modelBreakdown: some View {
        Group {
            if let sonnetUsage = usageService.currentUsage.sevenDaySonnetUtilization {
                HStack {
                    Image(systemName: "cpu")
                        .font(.caption)
                        .foregroundColor(.blue)
                    Text("Sonnet: \(sonnetUsage)%")
                        .font(.caption)
                    Spacer()
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 2)
            }
        }
    }

    private var actionButtons: some View {
        VStack(spacing: 0) {
            Button(action: openDashboard) {
                HStack {
                    Image(systemName: "chart.bar")
                    Text("Open Dashboard")
                    Spacer()
                }
            }
            .buttonStyle(.plain)
            .padding(.horizontal, 12)
            .padding(.vertical, 6)

            Button(action: refreshUsage) {
                HStack {
                    Image(systemName: "arrow.clockwise")
                    Text("Refresh")
                    Spacer()
                }
            }
            .buttonStyle(.plain)
            .padding(.horizontal, 12)
            .padding(.vertical, 6)

            Button(action: { showSettings = true }) {
                HStack {
                    Image(systemName: "gear")
                    Text("Settings")
                    Spacer()
                }
            }
            .buttonStyle(.plain)
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
        }
    }

    private var quitButton: some View {
        Button(action: quitApp) {
            HStack {
                Image(systemName: "power")
                Text("Quit")
                Spacer()
            }
        }
        .buttonStyle(.plain)
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
    }

    private var usageIconName: String {
        let usage = usageService.currentUsage.fiveHourUtilization
        if usage >= 80 { return "exclamationmark.triangle.fill" }
        if usage >= 50 { return "chart.pie.fill" }
        return "chart.pie"
    }

    private var usageColor: Color {
        let usage = usageService.currentUsage.fiveHourUtilization
        if usage >= 90 { return .red }
        if usage >= 70 { return .orange }
        return .primary
    }

    private var weeklyColor: Color {
        let usage = usageService.currentUsage.sevenDayUtilization
        let criticalThreshold = Int(settingsManager.settings.criticalThreshold)
        let warningThreshold = Int(settingsManager.settings.warningThreshold)
        if usage >= criticalThreshold { return .red }
        if usage >= warningThreshold { return .orange }
        return .primary
    }

    private func openDashboard() {
        if let url = URL(string: "http://localhost:\(enginePort)") {
            NSWorkspace.shared.open(url)
        }
    }

    private func refreshUsage() {
        usageService.fetchUsage()
        fetchProjection()
    }

    private func quitApp() {
        NSApplication.shared.terminate(nil)
    }

    @ViewBuilder
    private var shortcutsSection: some View {
        if let config = shortcutsConfig {
            let activeProjects = config.projects.filter { $0.isActive }
            if !activeProjects.isEmpty {
                Menu("Projects") {
                    ForEach(activeProjects, id: \.id) { project in
                        Menu(project.label) {
                            ForEach(project.actions, id: \.label) { action in
                                if let steps = action.steps, !steps.isEmpty {
                                    Button(action.label) {
                                        fireWithInput(action: action)
                                    }
                                } else {
                                    Button(action.label) {
                                        HermesClient.fire(prompt: action.prompt)
                                    }
                                }
                            }
                        }
                    }
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 2)
            }

            if !config.clis.isEmpty {
                Menu("Quick CLIs") {
                    ForEach(config.clis, id: \.id) { cli in
                        Button(cli.label) {
                            if let steps = cli.steps, !steps.isEmpty,
                               let first = steps.first(where: { $0.required }) {
                                fireCliWithInput(cli: cli, stepPrompt: first.prompt)
                            } else {
                                HermesClient.fire(prompt: cli.prompt)
                            }
                        }
                    }
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 2)

                Divider()
                    .padding(.vertical, 4)
            }
        }
        EmptyView()
            .onAppear {
                shortcutsConfig = ShortcutLoader.load()
            }
    }

    private func fireWithInput(action: WorkspaceAction) {
        guard let step = action.steps?.first(where: { $0.required }) else {
            HermesClient.fire(prompt: action.prompt)
            return
        }
        let alert = NSAlert()
        alert.messageText = action.label
        alert.informativeText = step.prompt
        alert.addButton(withTitle: "Send")
        alert.addButton(withTitle: "Cancel")
        let input = NSTextField(frame: NSRect(x: 0, y: 0, width: 300, height: 24))
        alert.accessoryView = input
        alert.window.initialFirstResponder = input
        if alert.runModal() == .alertFirstButtonReturn {
            let filled = action.prompt.replacingOccurrences(
                of: "{\(step.field)}",
                with: input.stringValue
            )
            HermesClient.fire(prompt: filled)
        }
    }

    private func fireCliWithInput(cli: WorkspaceCli, stepPrompt: String) {
        guard let step = cli.steps?.first(where: { $0.required }) else {
            HermesClient.fire(prompt: cli.prompt)
            return
        }
        let alert = NSAlert()
        alert.messageText = cli.label
        alert.informativeText = step.prompt
        alert.addButton(withTitle: "Send")
        alert.addButton(withTitle: "Cancel")
        let input = NSTextField(frame: NSRect(x: 0, y: 0, width: 300, height: 24))
        alert.accessoryView = input
        alert.window.initialFirstResponder = input
        if alert.runModal() == .alertFirstButtonReturn {
            let filled = cli.prompt.replacingOccurrences(
                of: "{\(step.field)}",
                with: input.stringValue
            )
            HermesClient.fire(prompt: filled)
        }
    }

    private func fetchProjection() {
        guard let url = URL(string: "http://localhost:\(enginePort)/api/status") else { return }
        URLSession.shared.dataTask(with: url) { data, response, error in
            if let error = error {
                AppLogger.error("engine", "Engine fetch error: \(error.localizedDescription)")
                return
            }
            guard let data = data else { return }

            let httpStatus = (response as? HTTPURLResponse)?.statusCode ?? 0
            guard httpStatus == 200 else {
                let body = String(data: data, encoding: .utf8) ?? ""
                AppLogger.error("engine", "Engine HTTP \(httpStatus): \(String(body.prefix(200)))")
                DispatchQueue.main.async {
                    self.projectionLine = "Engine: HTTP \(httpStatus)"
                }
                return
            }

            guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let projection = json["projection"] as? [String: Any],
                  let budget = json["budget"] as? [String: Any] else {
                let body = String(data: data, encoding: .utf8) ?? ""
                AppLogger.error("engine", "Engine parse error. Body: \(String(body.prefix(300)))")
                return
            }

            let runway = projection["runway_hours"] as? Double ?? 0
            let daily = budget["recommended_daily"] as? Double ?? 0
            let stoppage = projection["stoppage_likely"] as? Bool ?? false

            let runwayStr: String
            if runway >= 24 {
                let days = Int(runway / 24)
                let hours = Int(runway.truncatingRemainder(dividingBy: 24))
                runwayStr = "\(days)d \(hours)h runway"
            } else if runway >= 1 {
                let hours = Int(runway)
                let minutes = Int((runway - Double(hours)) * 60)
                runwayStr = minutes > 0 ? "\(hours)h \(minutes)m runway" : "\(hours)h runway"
            } else {
                let minutes = max(1, Int(runway * 60))
                runwayStr = "\(minutes)m runway"
            }
            let budgetStr = String(format: "≤%.0f%%/day budget", daily)
            let line = stoppage ? "⚠ \(runwayStr) · \(budgetStr)" : "\(runwayStr) · \(budgetStr)"

            DispatchQueue.main.async {
                self.projectionLine = line
            }
        }.resume()
    }
}
