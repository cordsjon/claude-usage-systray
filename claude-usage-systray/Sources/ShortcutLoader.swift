import Foundation
import Yams

enum ShortcutLoader {

    static let defaultConfigURL: URL = {
        let home = FileManager.default.homeDirectoryForCurrentUser
        return home
            .appendingPathComponent("projects/00_Governance/shortcuts/workspace_shortcuts.yaml")
    }()

    /// Loads config from the default filesystem path. Returns nil if missing or invalid.
    static func load() -> WorkspaceConfig? {
        load(from: defaultConfigURL)
    }

    /// Loads config from an arbitrary path. Exposed for testing.
    static func load(from url: URL) -> WorkspaceConfig? {
        guard FileManager.default.fileExists(atPath: url.path),
              let content = try? String(contentsOf: url, encoding: .utf8)
        else { return nil }
        return try? decode(yaml: content)
    }

    /// Decodes YAML string into WorkspaceConfig. Throws on parse failure.
    static func decode(yaml content: String) throws -> WorkspaceConfig {
        let decoder = YAMLDecoder()
        return try decoder.decode(WorkspaceConfig.self, from: content)
    }
}
