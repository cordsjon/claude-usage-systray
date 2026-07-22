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

/// Fields are optional because an unreachable instance's fallback status
/// carries `"counts": {}` (empty object) — non-optional Ints would fail
/// to decode it.
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

    // Injectable for testing (init is internal, not private, so tests can
    // build isolated instances with a mocked URLSession — unlike
    // UsageService, whose tests never construct one).
    var urlSession: URLSession = .shared
    var userDefaults: UserDefaults = .standard

    private let seenAlertIdsKey = "PosterEngineService.seenAlertIds"

    init() {}

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

// MARK: - Controls (retry / kick)

enum PEControlError: Error, Equatable {
    case unknownInstanceOrJob
    case rateLimited
    case httpError(Int)
    case network(String)
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
