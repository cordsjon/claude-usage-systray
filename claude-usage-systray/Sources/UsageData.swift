import Foundation

struct AppSettings: Codable {
    var warningThreshold: Double = 80.0
    var criticalThreshold: Double = 90.0
    var notificationsEnabled: Bool = true
    var compactDisplay: Bool = true

    var isConfigured: Bool { true }
}

struct UsageSnapshot {
    let fiveHourUtilization: Int
    let sevenDayUtilization: Int
    let sevenDaySonnetUtilization: Int?
    let fiveHourResetIn: String?
    let sevenDayResetIn: String?
    let lastUpdated: Date
    let weeklySessions: Int
    let weeklyMessages: Int
    let weeklyTokens: Int

    var displayText: String { "\(sevenDayUtilization)%" }
    var menuBarPrimaryText: String { "5hr: \(fiveHourUtilization)%" }
    var menuBarSecondaryText: String { "Week: \(sevenDayUtilization)%" }

    static var placeholder: UsageSnapshot {
        UsageSnapshot(
            fiveHourUtilization: 0,
            sevenDayUtilization: 0,
            sevenDaySonnetUtilization: nil,
            fiveHourResetIn: nil,
            sevenDayResetIn: nil,
            lastUpdated: Date(),
            weeklySessions: 0,
            weeklyMessages: 0,
            weeklyTokens: 0
        )
    }
}
