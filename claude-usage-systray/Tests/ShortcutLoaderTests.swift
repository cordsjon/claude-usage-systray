import XCTest
@testable import ClaudeUsageSystray

final class ShortcutLoaderTests: XCTestCase {

    private let validYAML = """
    version: 2
    projects:
      - id: svg-paint
        label: SVG-PAINT
        icon: palette
        color: "#4CAF50"
        active: true
        actions:
          - label: status
            prompt: Check SVG-PAINT status
          - label: research
            prompt: Research {query}
            steps:
              - field: query
                prompt: What to research?
                required: true
      - id: keto
        label: KETO
        icon: nutrition
        color: "#F44336"
        active: false
        actions:
          - label: status
            prompt: Check KETO status
    clis:
      - id: qmd
        label: qmd
        prompt: Search for {query}
        steps:
          - field: query
            prompt: Search query
            required: true
    general:
      - label: news
        prompt: Summarise news
    """

    func testDecodesVersion() throws {
        let config = try ShortcutLoader.decode(yaml: validYAML)
        XCTAssertEqual(config.version, 2)
    }

    func testDecodesProjects() throws {
        let config = try ShortcutLoader.decode(yaml: validYAML)
        XCTAssertEqual(config.projects.count, 2)
    }

    func testActiveProjectsExcludesInactive() throws {
        let config = try ShortcutLoader.decode(yaml: validYAML)
        let active = config.projects.filter { $0.isActive }
        XCTAssertEqual(active.count, 1)
        XCTAssertEqual(active.first?.id, "svg-paint")
    }

    func testProjectFields() throws {
        let config = try ShortcutLoader.decode(yaml: validYAML)
        let p = config.projects[0]
        XCTAssertEqual(p.label, "SVG-PAINT")
        XCTAssertEqual(p.icon, "palette")
        XCTAssertEqual(p.color, "#4CAF50")
        XCTAssertTrue(p.isActive)
    }

    func testActionWithSteps() throws {
        let config = try ShortcutLoader.decode(yaml: validYAML)
        let action = config.projects[0].actions[1]
        XCTAssertEqual(action.label, "research")
        XCTAssertEqual(action.steps?.count, 1)
        XCTAssertEqual(action.steps?.first?.field, "query")
        XCTAssertTrue(action.steps?.first?.required ?? false)
    }

    func testUnknownKeysIgnored() throws {
        let yamlWithExtra = """
        version: 2
        projects:
          - id: x
            label: X
            icon: folder
            color: "#000"
            active: true
            unknown_future_key: some_value
            actions:
              - label: go
                prompt: go
        clis: []
        general: []
        """
        let config = try ShortcutLoader.decode(yaml: yamlWithExtra)
        XCTAssertEqual(config.projects.count, 1)
    }

    func testMissingActiveDefaultsToTrue() throws {
        let yaml = """
        version: 2
        projects:
          - id: x
            label: X
            icon: folder
            color: "#000"
            actions:
              - label: go
                prompt: go
        clis: []
        general: []
        """
        let config = try ShortcutLoader.decode(yaml: yaml)
        XCTAssertTrue(config.projects.first?.isActive ?? false)
    }

    func testMissingFileReturnsNil() {
        let result = ShortcutLoader.load(
            from: URL(fileURLWithPath: "/nonexistent/path/workspace_shortcuts.yaml")
        )
        XCTAssertNil(result)
    }

    func testDecodesCliEntries() throws {
        let config = try ShortcutLoader.decode(yaml: validYAML)
        XCTAssertEqual(config.clis.count, 1)
        XCTAssertEqual(config.clis.first?.id, "qmd")
    }

    func testDecodesGeneralEntries() throws {
        let config = try ShortcutLoader.decode(yaml: validYAML)
        XCTAssertEqual(config.general.count, 1)
        XCTAssertEqual(config.general.first?.label, "news")
    }
}
