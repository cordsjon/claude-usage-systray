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
