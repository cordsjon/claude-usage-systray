import Foundation
import Security

// MARK: - OAuth Keychain

private struct KeychainCredentials: Decodable {
    let claudeAiOauth: OAuthData

    struct OAuthData: Decodable {
        let accessToken: String
        let expiresAt: Double
    }
}

struct OAuthCredentials {
    let accessToken: String
    let expiresAt: Double
}

func readOAuthCredentials() throws -> OAuthCredentials {
    var result: AnyObject?
    let query: [String: Any] = [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: "Claude Code-credentials",
        kSecReturnData as String: true,
        kSecMatchLimit as String: kSecMatchLimitOne
    ]
    let status = SecItemCopyMatching(query as CFDictionary, &result)
    guard status == errSecSuccess, let data = result as? Data else {
        throw NSError(domain: "Keychain", code: Int(status),
                      userInfo: [NSLocalizedDescriptionKey: "Claude Code credentials not found in Keychain. Make sure Claude Code is installed and logged in. (status: \(status))"])
    }
    let decoded = try JSONDecoder().decode(KeychainCredentials.self, from: data)
    return OAuthCredentials(accessToken: decoded.claudeAiOauth.accessToken,
                            expiresAt: decoded.claudeAiOauth.expiresAt)
}

// MARK: - API Response Model

struct OAuthUsageResponse: Decodable {
    let fiveHour: UsagePeriod?
    let sevenDay: UsagePeriod?
    let sevenDaySonnet: UsagePeriod?

    enum CodingKeys: String, CodingKey {
        case fiveHour = "five_hour"
        case sevenDay = "seven_day"
        case sevenDaySonnet = "seven_day_sonnet"
    }

    struct UsagePeriod: Decodable {
        let utilization: Double
        let resetsAt: String?

        enum CodingKeys: String, CodingKey {
            case utilization
            case resetsAt = "resets_at"
        }

        var resetsAtDate: Date? {
            guard let resetsAt = resetsAt else { return nil }
            let formatter = ISO8601DateFormatter()
            formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            return formatter.date(from: resetsAt)
        }
    }
}

// MARK: - Utilization helpers (pure, testable)

/// Returns utilization percentage (0–100) given token count and limit.
func calculateUtilization(tokens: Int, limit: Int) -> Int {
    guard limit > 0 else { return 0 }
    return min(100, tokens * 100 / limit)
}

/// Formats a future date as a human-readable countdown string.
func formatTimeRemaining(until date: Date, from now: Date = Date()) -> String {
    let interval = date.timeIntervalSince(now)
    if interval <= 0 { return "now" }
    let hours = Int(interval) / 3600
    let minutes = (Int(interval) % 3600) / 60
    return hours > 0 ? "\(hours)h \(minutes)m" : "\(minutes)m"
}

// MARK: - UsageService

final class UsageService: ObservableObject {
    static let shared = UsageService()

    @Published private(set) var currentUsage: UsageSnapshot = .placeholder
    @Published private(set) var error: String?
    @Published private(set) var isLoading: Bool = false
    @Published private(set) var weeklySessions: Int = 0
    @Published private(set) var weeklyMessages: Int = 0
    @Published private(set) var weeklyTokens: Int = 0

    private var refreshTimer: Timer?
    private let normalInterval: TimeInterval = 60        // 1 minute — matches engine poll interval
    private let backoffInterval: TimeInterval = 15 * 60 // 15 minutes after 429

    // Injectable for testing
    var urlSession: URLSession = .shared

    private var cachedToken: String?
    private var cachedTokenExpiresAt: Double?

    private init() {}

    private func accessToken(forceRefresh: Bool = false) throws -> String {
        if !forceRefresh, let token = cachedToken,
           let expiresAt = cachedTokenExpiresAt, Date().timeIntervalSince1970 < expiresAt - 60 {
            return token
        }
        let creds = try readOAuthCredentials()
        cachedToken = creds.accessToken
        cachedTokenExpiresAt = creds.expiresAt
        return creds.accessToken
    }

    func clearCachedToken() {
        cachedToken = nil
        cachedTokenExpiresAt = nil
    }

    func startPolling() {
        fetchUsage()
        scheduleTimer(interval: normalInterval)
    }

    func stopPolling() {
        refreshTimer?.invalidate()
        refreshTimer = nil
    }

    private func scheduleTimer(interval: TimeInterval) {
        refreshTimer?.invalidate()
        refreshTimer = Timer.scheduledTimer(withTimeInterval: interval, repeats: false) { [weak self] _ in
            self?.fetchUsage()
        }
    }

    func fetchUsage() {
        DispatchQueue.main.async { self.isLoading = true }

        Task {
            // Primary source: engine's /api/status (already polling Anthropic).
            // Falls back to direct Anthropic API only when engine has no data yet (503).
            if let snapshot = await fetchFromEngine() {
                await MainActor.run {
                    self.currentUsage = snapshot
                    self.error = nil
                    self.isLoading = false
                    self.scheduleTimer(interval: self.normalInterval)
                }
                return
            }

            // Fallback: direct Anthropic API (engine not yet warmed up)
            do {
                let response = try await fetchWithAuthRetry()

                let fiveHourUtil = Int(response.fiveHour?.utilization ?? 0)
                let sevenDayUtil = Int(response.sevenDay?.utilization ?? 0)
                let sonnetUtil: Int? = response.sevenDaySonnet.map { Int($0.utilization) }

                let fiveHourReset = response.fiveHour?.resetsAtDate
                let sevenDayReset = response.sevenDay?.resetsAtDate

                let snapshot = UsageSnapshot(
                    fiveHourUtilization: fiveHourUtil,
                    sevenDayUtilization: sevenDayUtil,
                    sevenDaySonnetUtilization: sonnetUtil,
                    fiveHourResetIn: fiveHourReset.map { formatTimeRemaining(until: $0) },
                    sevenDayResetIn: sevenDayReset.map { formatTimeRemaining(until: $0) },
                    lastUpdated: Date(),
                    weeklySessions: 0,
                    weeklyMessages: 0,
                    weeklyTokens: 0
                )

                AppLogger.info("api", "Fallback poll OK: 5h=\(fiveHourUtil)% 7d=\(sevenDayUtil)%")

                await MainActor.run {
                    self.currentUsage = snapshot
                    self.error = nil
                    self.isLoading = false
                    self.scheduleTimer(interval: self.normalInterval)
                }
            } catch {
                let nsError = error as NSError
                let isRateLimit = nsError.code == 429
                AppLogger.error("api", "fetchUsage failed: \(error)")
                await MainActor.run {
                    if isRateLimit {
                        self.clearCachedToken()
                        self.error = "Rate limited — retrying in 15 min"
                        self.scheduleTimer(interval: self.backoffInterval)
                    } else {
                        self.error = nsError.localizedDescription
                        self.scheduleTimer(interval: self.normalInterval)
                    }
                    self.isLoading = false
                }
            }
        }
    }

    /// Read current utilization from the local Python engine.
    /// Returns nil if the engine is unreachable or has no data yet (503).
    private func fetchFromEngine() async -> UsageSnapshot? {
        guard let url = URL(string: "http://localhost:17420/api/status") else { return nil }
        do {
            let (data, response) = try await urlSession.data(from: url)
            guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                return nil  // 503 = engine not warmed up yet; any other error = skip
            }
            guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let current = json["current"] as? [String: Any] else { return nil }

            let fiveHourUtil = Int((current["five_hour_util"] as? Double) ?? 0)
            let sevenDayUtil = Int((current["seven_day_util"] as? Double) ?? 0)
            let sonnetUtil: Int? = (current["sonnet_util"] as? Double).map { Int($0) }

            let fiveHourResetIn = current["five_hour_resets_in"] as? String
            let sevenDayResetIn = current["seven_day_resets_in"] as? String

            AppLogger.info("api", "Engine poll OK: 5h=\(fiveHourUtil)% 7d=\(sevenDayUtil)%")

            return UsageSnapshot(
                fiveHourUtilization: fiveHourUtil,
                sevenDayUtilization: sevenDayUtil,
                sevenDaySonnetUtilization: sonnetUtil,
                fiveHourResetIn: fiveHourResetIn,
                sevenDayResetIn: sevenDayResetIn,
                lastUpdated: Date(),
                weeklySessions: 0,
                weeklyMessages: 0,
                weeklyTokens: 0
            )
        } catch {
            AppLogger.error("api", "Engine unreachable: \(error.localizedDescription)")
            return nil
        }
    }

    /// Try the API call; on 401, clear cached token, re-read from keychain, retry once.
    private func fetchWithAuthRetry() async throws -> OAuthUsageResponse {
        let token = try accessToken()
        do {
            return try await fetchOAuthUsage(accessToken: token)
        } catch {
            let nsError = error as NSError
            guard nsError.code == 401 else { throw error }
            AppLogger.info("api", "401 — clearing cached token and retrying with fresh keychain read")
            clearCachedToken()
            let freshToken = try accessToken(forceRefresh: true)
            return try await fetchOAuthUsage(accessToken: freshToken)
        }
    }

    func fetchOAuthUsage(accessToken: String) async throws -> OAuthUsageResponse {
        var request = URLRequest(url: URL(string: "https://api.anthropic.com/api/oauth/usage")!)
        request.setValue("Bearer \(accessToken)", forHTTPHeaderField: "Authorization")
        request.setValue("oauth-2025-04-20", forHTTPHeaderField: "anthropic-beta")

        AppLogger.info("api", "GET /api/oauth/usage")

        let (data, response) = try await urlSession.data(for: request)
        let body = String(data: data, encoding: .utf8) ?? "<binary>"

        guard let http = response as? HTTPURLResponse else {
            throw URLError(.badServerResponse)
        }

        AppLogger.info("api", "HTTP \(http.statusCode) — \(String(body.prefix(300)))")

        guard http.statusCode == 200 else {
            throw NSError(domain: "OAuthUsage", code: http.statusCode,
                          userInfo: [NSLocalizedDescriptionKey: "HTTP \(http.statusCode): \(body)"])
        }

        do {
            return try JSONDecoder().decode(OAuthUsageResponse.self, from: data)
        } catch {
            AppLogger.error("api", "Decode error: \(error)")
            AppLogger.error("api", "Raw body: \(String(body.prefix(500)))")
            throw error
        }
    }
}
