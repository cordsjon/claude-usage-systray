import XCTest
@testable import ClaudeUsageSystray

/// Covers the regression this type exists to prevent: the app hardcoded
/// `localhost:17420` while `engine/api.py` bound the tailnet address by
/// default, so every engine poll failed silently behind the direct-API
/// fallback (1413 "Engine unreachable" entries, 0 successes).
final class EngineConfigTests: XCTestCase {

    private let defaultsKey = "engineHost"

    override func setUp() {
        super.setUp()
        UserDefaults.standard.removeObject(forKey: defaultsKey)
    }

    override func tearDown() {
        UserDefaults.standard.removeObject(forKey: defaultsKey)
        super.tearDown()
    }

    /// The contract with engine/api.py. If someone changes the engine's
    /// `bind_host` default without changing this constant, this test fails —
    /// which is the whole point of pinning it.
    func testDefaultHostMatchesEngineBindDefault() {
        XCTAssertEqual(EngineConfig.defaultHost, "100.92.111.112")
        XCTAssertEqual(EngineConfig.port, 17420)
    }

    /// Guards the original bug directly: the default must NOT be loopback,
    /// because the engine does not listen there.
    func testDefaultHostIsNotLoopback() throws {
        UserDefaults.standard.removeObject(forKey: defaultsKey)
        guard ProcessInfo.processInfo.environment["ENGINE_HOST"] == nil else {
            throw XCTSkip("ENGINE_HOST is set in this environment; default path not exercised")
        }
        XCTAssertNotEqual(EngineConfig.host, "localhost")
        XCTAssertNotEqual(EngineConfig.host, "127.0.0.1")
    }

    func testUserDefaultsOverridesDefault() throws {
        guard ProcessInfo.processInfo.environment["ENGINE_HOST"] == nil else {
            throw XCTSkip("ENGINE_HOST is set and outranks UserDefaults")
        }
        UserDefaults.standard.set("127.0.0.1", forKey: defaultsKey)
        XCTAssertEqual(EngineConfig.host, "127.0.0.1")
        XCTAssertEqual(EngineConfig.baseURL, "http://127.0.0.1:17420")
    }

    /// An empty string must not count as "configured" — otherwise a cleared
    /// preference silently yields "http://:17420".
    func testEmptyUserDefaultsFallsBackToDefault() throws {
        guard ProcessInfo.processInfo.environment["ENGINE_HOST"] == nil else {
            throw XCTSkip("ENGINE_HOST is set and outranks UserDefaults")
        }
        UserDefaults.standard.set("", forKey: defaultsKey)
        XCTAssertEqual(EngineConfig.host, EngineConfig.defaultHost)
    }

    func testBaseURLHasNoTrailingSlash() {
        XCTAssertFalse(EngineConfig.baseURL.hasSuffix("/"))
    }

    func testURLBuildsEndpointPaths() throws {
        let url = try XCTUnwrap(EngineConfig.url("/api/status"))
        XCTAssertEqual(url.path, "/api/status")
        XCTAssertEqual(url.port, 17420)
        XCTAssertEqual(url.scheme, "http")
    }

    /// `host` is a computed property, not a cached `static let`, so a changed
    /// preference takes effect on the next poll rather than the next launch.
    func testHostReflectsLaterChangesWithoutRelaunch() throws {
        guard ProcessInfo.processInfo.environment["ENGINE_HOST"] == nil else {
            throw XCTSkip("ENGINE_HOST is set and outranks UserDefaults")
        }
        UserDefaults.standard.set("10.0.0.1", forKey: defaultsKey)
        XCTAssertEqual(EngineConfig.host, "10.0.0.1")
        UserDefaults.standard.set("10.0.0.2", forKey: defaultsKey)
        XCTAssertEqual(EngineConfig.host, "10.0.0.2")
    }
}
