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

// MARK: - Staleness detection (pure function)

final class PEStalenessTests: XCTestCase {
    func testNotStaleWithinThreeIntervals() {
        let lastPoll = Date().addingTimeInterval(-90)  // 90s ago, interval=60s -> 1.5 intervals
        XCTAssertFalse(isSupervisorStale(lastPoll: lastPoll, pollInterval: 60, now: Date()))
    }

    func testStaleAfterThreeIntervals() {
        let lastPoll = Date().addingTimeInterval(-200)  // 200s ago, interval=60s -> >3 intervals
        XCTAssertTrue(isSupervisorStale(lastPoll: lastPoll, pollInterval: 60, now: Date()))
    }

    func testNilLastPollIsStale() {
        XCTAssertTrue(isSupervisorStale(lastPoll: nil, pollInterval: 60, now: Date()))
    }
}

// MARK: - Alert seen-id dedupe (pure function)

final class PEAlertDedupeTests: XCTestCase {
    func testUnseenActiveAlertIsNewlyUnseen() {
        let seen: Set<String> = []
        let alerts = [PEAlert(alertId: "stalled:dev:active", firstSeen: "t1", lastSeen: "t2", active: true)]
        let unseen = unseenActiveAlertIds(alerts: alerts, seenIds: seen)
        XCTAssertEqual(unseen, ["stalled:dev:active"])
    }

    func testAlreadySeenAlertIsNotReturnedAgain() {
        let seen: Set<String> = ["stalled:dev:active"]
        let alerts = [PEAlert(alertId: "stalled:dev:active", firstSeen: "t1", lastSeen: "t2", active: true)]
        let unseen = unseenActiveAlertIds(alerts: alerts, seenIds: seen)
        XCTAssertTrue(unseen.isEmpty)
    }

    func testInactiveAlertNeverConsideredUnseen() {
        let seen: Set<String> = []
        let alerts = [PEAlert(alertId: "stalled:dev:active", firstSeen: "t1", lastSeen: "t2", active: false)]
        let unseen = unseenActiveAlertIds(alerts: alerts, seenIds: seen)
        XCTAssertTrue(unseen.isEmpty)
    }
}

// MARK: - Retry / Kick control dispatch

private final class MockURLProtocol: URLProtocol {
    static var requestHandler: ((URLRequest) throws -> (HTTPURLResponse, Data))?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let handler = Self.requestHandler else {
            XCTFail("No request handler set")
            return
        }
        do {
            let (response, data) = try handler(request)
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}

final class PEControlDispatchTests: XCTestCase {
    var session: URLSession!
    var service: PosterEngineService!

    override func setUp() {
        super.setUp()
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [MockURLProtocol.self]
        session = URLSession(configuration: config)
        service = PosterEngineService()
        service.urlSession = session
    }

    override func tearDown() {
        MockURLProtocol.requestHandler = nil
        super.tearDown()
    }

    func testRetryPostsToCorrectPathAndParsesOpId() async throws {
        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.url?.path, "/pe/dev/jobs/job-1/retry")
            XCTAssertEqual(request.httpMethod, "POST")
            let body = try JSONSerialization.data(withJSONObject: ["accepted": true, "op_id": "op-99"])
            let resp = HTTPURLResponse(url: request.url!, statusCode: 202, httpVersion: nil, headerFields: nil)!
            return (resp, body)
        }
        let opId = try await service.retry(instance: "dev", jobId: "job-1")
        XCTAssertEqual(opId, "op-99")
    }

    func testKickPostsToCorrectPath() async throws {
        MockURLProtocol.requestHandler = { request in
            XCTAssertEqual(request.url?.path, "/pe/dev/worker/kick")
            let body = try JSONSerialization.data(withJSONObject: ["accepted": true, "op_id": "op-100"])
            let resp = HTTPURLResponse(url: request.url!, statusCode: 202, httpVersion: nil, headerFields: nil)!
            return (resp, body)
        }
        let opId = try await service.kick(instance: "dev")
        XCTAssertEqual(opId, "op-100")
    }

    func testKickRateLimited429ThrowsDescriptiveError() async throws {
        MockURLProtocol.requestHandler = { request in
            let resp = HTTPURLResponse(url: request.url!, statusCode: 429, httpVersion: nil, headerFields: nil)!
            return (resp, Data("{}".utf8))
        }
        do {
            _ = try await service.kick(instance: "dev")
            XCTFail("expected throw")
        } catch let error as PEControlError {
            XCTAssertEqual(error, .rateLimited)
        }
    }
}
