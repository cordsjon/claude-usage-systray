import Foundation

/// Single source of truth for how this app reaches the Python engine.
///
/// The engine binds one specific interface address, not 0.0.0.0 — see
/// `engine/api.py:create_server`, which resolves its bind host as
/// `host arg -> $ENGINE_HOST -> "100.92.111.112"` (the tailnet address).
/// A socket bound to one address only receives packets sent to that address,
/// so `localhost` is NOT reachable under the default configuration.
///
/// This type deliberately mirrors that precedence chain so the two sides
/// cannot drift apart again: previously the engine defaulted to the tailnet
/// IP while the app hardcoded `localhost:17420` in ten places, and every
/// engine poll failed silently for weeks behind the direct-API fallback.
///
/// Override order (highest first):
///   1. `ENGINE_HOST` environment variable — matches the engine's own env var,
///      so one launchd `EnvironmentVariables` entry can move both sides.
///   2. `engineHost` in UserDefaults — for a GUI app launched by launchd,
///      where setting an env var means editing a plist:
///        defaults write com.claude.usage-systray engineHost 127.0.0.1
///   3. `defaultHost` — the tailnet address the engine binds by default.
enum EngineConfig {

    /// Must stay equal to the `bind_host` default in `engine/api.py`.
    static let defaultHost = "100.92.111.112"

    static let port = 17420

    /// Resolved engine host. Computed per access rather than cached in a `let`
    /// so a `defaults write` takes effect on the next poll instead of requiring
    /// a relaunch — the polls are a minute apart, so the lookup cost is noise.
    static var host: String {
        if let env = ProcessInfo.processInfo.environment["ENGINE_HOST"],
           !env.isEmpty {
            return env
        }
        if let stored = UserDefaults.standard.string(forKey: "engineHost"),
           !stored.isEmpty {
            return stored
        }
        return defaultHost
    }

    /// Scheme + host + port, with no trailing slash: "http://100.92.111.112:17420".
    /// Append an absolute path ("/api/status") to build an endpoint.
    static var baseURL: String {
        "http://\(host):\(port)"
    }

    /// Build an endpoint URL for `path`, which must begin with "/".
    static func url(_ path: String) -> URL? {
        URL(string: baseURL + path)
    }
}
