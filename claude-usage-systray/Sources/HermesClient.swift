import Foundation
import UserNotifications

/// Routes workspace shortcut actions to the hermes-adapter at localhost:9109.
///
/// POST /chat body: {"message": "<prompt>"}
/// On error: non-modal UNUserNotification (no modal NSAlert per spec §6.5).
enum HermesClient {
    private static let adapterURL = URL(string: "http://localhost:9109/chat")!

    static func fire(prompt: String) {
        guard let body = try? JSONEncoder().encode(["message": prompt]) else { return }
        var request = URLRequest(url: adapterURL, timeoutInterval: 5)
        request.httpMethod = "POST"
        request.addValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = body

        URLSession.shared.dataTask(with: request) { _, response, error in
            if let error = error {
                Self.notifyError("Hermes unreachable: \(error.localizedDescription)")
                return
            }
            let status = (response as? HTTPURLResponse)?.statusCode ?? 0
            if status >= 500 {
                Self.notifyError("Hermes returned HTTP \(status)")
            }
        }.resume()
    }

    private static func notifyError(_ message: String) {
        Notifier.post(title: "Hermes Shortcut", body: message, critical: false)
    }
}
