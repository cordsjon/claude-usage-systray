import XCTest
import UserNotifications
@testable import ClaudeUsageSystray

final class NotifierTests: XCTestCase {

    func testPostBuildsNonCriticalContentWithDefaultSound() {
        let content = Notifier.buildContent(title: "Test Title", body: "Test body", critical: false)
        XCTAssertEqual(content.title, "Test Title")
        XCTAssertEqual(content.body, "Test body")
        XCTAssertEqual(content.sound, .default)
    }

    func testPostBuildsCriticalContentWithDefaultCriticalSound() {
        let content = Notifier.buildContent(title: "Alert", body: "Something broke", critical: true)
        XCTAssertEqual(content.sound, .defaultCritical)
    }
}
