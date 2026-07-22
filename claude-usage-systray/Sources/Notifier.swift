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
