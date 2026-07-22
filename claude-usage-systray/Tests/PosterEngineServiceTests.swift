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
