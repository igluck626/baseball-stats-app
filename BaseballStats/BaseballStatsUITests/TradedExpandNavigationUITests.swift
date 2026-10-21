//
//  TradedExpandNavigationUITests.swift
//  BaseballStatsUITests
//
//  A traded season's club rows stay expanded while you go deeper from the
//  profile and come back, and reset only when the profile itself is popped.
//  Navigates for real: Search → Luis Arraez → expand 2026 → Game Logs → push a
//  box score → back (still expanded) → back to Search → Arraez again
//  (collapsed).
//
//  Reads live data: our server's /players/{id}/season-teams (Arraez's 2026
//  SF → PHI clubs) and balldontlie for the box score. FAILS until the server
//  serves /season-teams.
//

import XCTest

final class TradedExpandNavigationUITests: XCTestCase {
    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    @MainActor
    func testExpandedSurvivesAPushAndResetsWhenTheProfileIsPopped() throws {
        let app = XCUIApplication()
        app.launchArguments += ["-appearanceMode", "light"]
        app.launch()

        openArraez(app)
        let disclosure = topDisclosure(app)
        XCTAssertEqual(disclosure.value as? String, "Collapsed", "a traded season opens collapsed")
        disclosure.tap()
        XCTAssertEqual(topDisclosure(app).value as? String, "Expanded")

        // Deeper: a box score from the Game Logs tab, then back.
        app.buttons["Game Logs"].firstMatch.tap()
        let row = app.buttons["gameLogs.row"].firstMatch
        XCTAssertTrue(row.waitForExistence(timeout: 30), "no tappable game-log row")
        row.tap()
        // The box score's title is "AWAY @ HOME" in every layout.
        let boxScore = app.navigationBars.matching(NSPredicate(format: "identifier CONTAINS ' @ '")).firstMatch
        XCTAssertTrue(boxScore.waitForExistence(timeout: 30), "the box score did not open")
        back(app)
        XCTAssertTrue(app.buttons["Career"].firstMatch.waitForExistence(timeout: 10))
        app.buttons["Career"].firstMatch.tap()
        XCTAssertEqual(topDisclosure(app).value as? String, "Expanded",
                       "expanded rows survive a push from the profile")

        // Leave the profile itself, then come back to it.
        back(app)
        XCTAssertTrue(app.searchFields.firstMatch.waitForExistence(timeout: 10), "not back on Search")
        openResult(app)
        app.buttons["Career"].firstMatch.tap()
        XCTAssertEqual(topDisclosure(app).value as? String, "Collapsed",
                       "a popped profile comes back collapsed")
    }

    private func openArraez(_ app: XCUIApplication) {
        searchTab(app).tap()
        let field = app.searchFields.firstMatch
        XCTAssertTrue(field.waitForExistence(timeout: 10)); field.tap()
        field.typeText("Luis Arraez")
        openResult(app)
        let career = app.buttons["Career"].firstMatch
        XCTAssertTrue(career.waitForExistence(timeout: 30)); career.tap()
    }

    /// The Search tab: in the bottom tab bar on a phone, in the floating bar at
    /// the top on iPad (see SceneStateFlipUITests.tab).
    private func searchTab(_ app: XCUIApplication) -> XCUIElement {
        let deadline = Date().addingTimeInterval(30)
        repeat {
            let inTabBar = app.tabBars.buttons["Search"]
            if inTabBar.exists { return inTabBar }
            if let top = app.buttons.matching(NSPredicate(format: "label == 'Search'")).allElementsBoundByIndex
                .first(where: { $0.exists && $0.frame.minY < 120 }) { return top }
            usleep(250_000)
        } while Date() < deadline
        XCTFail("no Search tab")
        return app.tabBars.buttons["Search"]
    }

    private func openResult(_ app: XCUIApplication) {
        let result = app.staticTexts["Luis Arraez"].firstMatch
        XCTAssertTrue(result.waitForExistence(timeout: 30), "no search result")
        result.tap()
    }

    /// The topmost traded season's disclosure (Arraez's 2026 under the default
    /// year-descending sort).
    private func topDisclosure(_ app: XCUIApplication) -> XCUIElement {
        let first = app.buttons["tradedDisclosure"].firstMatch
        XCTAssertTrue(first.waitForExistence(timeout: 30), "no traded season on screen")
        let all = app.buttons.matching(identifier: "tradedDisclosure").allElementsBoundByIndex
        return all.min { $0.frame.minY < $1.frame.minY } ?? first
    }

    private func back(_ app: XCUIApplication) {
        let button = app.navigationBars.buttons.element(boundBy: 0)
        XCTAssertTrue(button.waitForExistence(timeout: 10), "no back button")
        button.tap()
    }
}
