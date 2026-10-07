//
//  SceneStateFlipUITests.swift
//  BaseballStatsUITests
//
//  Per-window state must survive a size-class change. Today nothing branches on
//  width, so this passes trivially on the views — it exists to keep passing once
//  a wide layout (a split view on iPad) does branch, which is exactly when view-
//  local state would be thrown away on every resize.
//
//  Launched with `-SizeClassFlipHarness` (DEBUG builds), which adds a button that
//  flips the root's horizontal size class. Reads live BDL data: it steps back
//  to the nearest earlier day with a finished game.
//

import XCTest

final class SceneStateFlipUITests: XCTestCase {
    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    @MainActor
    func testScoresDateAndOpenBoxScoreSurviveSizeClassFlips() throws {
        let app = XCUIApplication()
        app.launchArguments += ["-SizeClassFlipHarness"]
        app.launch()

        app.tabBars.buttons["Scores"].tap()

        // Step back to the nearest earlier day with a finished game (an off day
        // has none), so the day on screen is not today's.
        let previous = app.buttons["scores.previousDay"]
        XCTAssertTrue(previous.waitForExistence(timeout: 10))
        let pill = app.buttons["scores.datePill"]
        let finalCard = app.staticTexts.matching(NSPredicate(format: "label ==[c] 'FINAL'")).firstMatch
        var found = false
        for _ in 0..<7 {
            previous.tap()
            if finalCard.waitForExistence(timeout: 15) { found = true; break }
        }
        XCTAssertTrue(found, "no finished game in the last week")
        let day = pill.label
        XCTAssertNotEqual(day, "Today")

        // Expand the first final card, then push its box score.
        finalCard.tap()
        let boxScoreButton = app.buttons.matching(NSPredicate(format: "label BEGINSWITH 'Box Score'")).firstMatch
        XCTAssertTrue(boxScoreButton.waitForExistence(timeout: 10))
        boxScoreButton.tap()

        let boxScoreBar = app.navigationBars.element(boundBy: 0)
        XCTAssertTrue(boxScoreBar.waitForExistence(timeout: 10))
        let boxScoreTitle = boxScoreBar.identifier
        XCTAssertNotEqual(boxScoreTitle, "Scores", "the box score didn't open")

        // compact -> regular -> compact -> regular -> compact
        let flip = app.buttons["harness.flipSizeClass"]
        XCTAssertTrue(flip.exists)
        attachScreenshot(app, "0 box score \(boxScoreTitle) on \(day), compact")
        for (i, expected) in ["regular", "compact", "regular", "compact"].enumerated() {
            flip.tap()
            XCTAssertTrue(app.buttons[expected].waitForExistence(timeout: 5), "harness didn't flip to \(expected)")
            XCTAssertTrue(app.navigationBars[boxScoreTitle].waitForExistence(timeout: 5),
                          "the open box score didn't survive the flip to \(expected)")
            attachScreenshot(app, "\(i + 1) after flip to \(expected)")
        }

        // Back at the Scores root: still the same day.
        app.navigationBars[boxScoreTitle].buttons.element(boundBy: 0).tap()
        XCTAssertTrue(pill.waitForExistence(timeout: 5))
        XCTAssertEqual(pill.label, day, "the Scores day didn't survive the flips")
        attachScreenshot(app, "5 back at the Scores root on \(pill.label)")
    }

    private func attachScreenshot(_ app: XCUIApplication, _ name: String) {
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = name
        shot.lifetime = .keepAlways
        add(shot)
    }
}
