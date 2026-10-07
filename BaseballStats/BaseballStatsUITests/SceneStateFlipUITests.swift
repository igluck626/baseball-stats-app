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
//  flips the window's horizontal size class (at the root and inside Ask). Reads
//  live BDL data: it steps back to the nearest earlier day with a finished game.
//

import XCTest

final class SceneStateFlipUITests: XCTestCase {
    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    @MainActor
    func testScoresAndAskStateSurviveSizeClassFlips() throws {
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

        // Ask: type a draft, flip with Ask open, and the draft is still there.
        app.buttons["Ask a question"].firstMatch.tap()
        XCTAssertTrue(app.navigationBars["Ask"].waitForExistence(timeout: 5))
        let field = app.textFields["ask.draft"]
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        field.tap()
        let draft = "How many home runs did Babe Ruth hit"
        field.typeText(draft)
        XCTAssertEqual(field.value as? String, draft)
        let askFlip = app.buttons["harness.flipSizeClass.ask"]
        for (i, expected) in ["regular", "compact", "regular", "compact"].enumerated() {
            askFlip.tap()
            XCTAssertTrue(app.buttons[expected].firstMatch.waitForExistence(timeout: 5), "harness didn't flip to \(expected)")
            XCTAssertTrue(app.navigationBars["Ask"].exists, "Ask closed on the flip to \(expected)")
            XCTAssertEqual(field.value as? String, draft, "the Ask draft didn't survive the flip to \(expected)")
            attachScreenshot(app, "\(6 + i) Ask after flip to \(expected)")
        }

        // Done and reopen: the window keeps the draft.
        app.navigationBars["Ask"].buttons["Done"].tap()
        XCTAssertTrue(pill.waitForExistence(timeout: 5))
        app.buttons["Ask a question"].firstMatch.tap()
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        XCTAssertEqual(field.value as? String, draft, "the Ask draft didn't survive Done and reopen")
    }

    /// Push something on Leaders (after changing a filter), Search and Home, then
    /// flip the size class four times, visiting every tab after each flip: each
    /// tab still shows what was pushed, the Leaders filter is still set, and the
    /// profile opened from Leaders keeps its tab and its Career scope.
    @MainActor
    func testTabStacksAndLeadersFilterSurviveFlipsAndTabSwitches() throws {
        let app = XCUIApplication()
        // A favourite team, so Home shows its team page (and its news) rather
        // than the team picker.
        app.launchArguments += ["-SizeClassFlipHarness", "-favoriteTeamBDLId", "19"]
        app.launch()

        // Leaders: Career, then the first player.
        app.tabBars.buttons["Leaders"].tap()
        let career = app.buttons["Career"].firstMatch
        XCTAssertTrue(career.waitForExistence(timeout: 10))
        career.tap()
        XCTAssertTrue(career.isSelected)
        let firstRow = app.collectionViews.cells.firstMatch
        XCTAssertTrue(firstRow.waitForExistence(timeout: 20), "no Career leaderboard rows")
        // The profile has no navigation title, so it's recognised by the player's
        // name on screen with the Leaderboards root gone.
        let name = try XCTUnwrap(firstRow.staticTexts.allElementsBoundByIndex.map(\.label)
            .first { $0.contains(" ") && $0.rangeOfCharacter(from: .letters) != nil }, "no player name in the row")
        firstRow.tap()
        let leadersRoot = app.navigationBars["Leaderboards"]
        XCTAssertTrue(app.staticTexts[name].firstMatch.waitForExistence(timeout: 10))
        XCTAssertTrue(leadersRoot.waitForNonExistence(timeout: 10), "the profile didn't open")

        // The profile's own choices: the Career table's Postseason scope, then
        // the Game Logs tab.
        let postseason = app.buttons["Postseason"].firstMatch
        XCTAssertTrue(postseason.waitForExistence(timeout: 15), "no Postseason scope on \(name)'s Career tab")
        postseason.tap()
        XCTAssertTrue(postseason.isSelected)
        let gameLogs = app.buttons["Game Logs"].firstMatch
        gameLogs.tap()
        XCTAssertTrue(gameLogs.isSelected)

        // Search: Award Voting.
        app.tabBars.buttons["Search"].tap()
        let awards = app.buttons.matching(NSPredicate(format: "label CONTAINS 'Award Voting'")).firstMatch
        XCTAssertTrue(awards.waitForExistence(timeout: 10))
        awards.tap()
        let searchTitle = pushedTitle(app, root: "Search")

        // Home: the team's news list.
        app.tabBars.buttons["Home"].tap()
        let seeAll = app.buttons["See all"].firstMatch
        XCTAssertTrue(seeAll.waitForExistence(timeout: 20), "no news section on Home")
        for _ in 0..<8 where !seeAll.isHittable { app.swipeUp() }
        seeAll.tap()
        let homeTitle = pushedTitle(app, root: nil)
        attachScreenshot(app, "d0 Home \(homeTitle); Search \(searchTitle); Leaders profile of \(name)")

        let flip = app.buttons["harness.flipSizeClass"]
        for (i, expected) in ["regular", "compact", "regular", "compact"].enumerated() {
            flip.tap()
            XCTAssertTrue(app.buttons[expected].firstMatch.waitForExistence(timeout: 5),
                          "harness didn't flip to \(expected)")
            app.tabBars.buttons["Leaders"].tap()
            XCTAssertTrue(app.staticTexts[name].firstMatch.waitForExistence(timeout: 5) && !leadersRoot.exists,
                          "Leaders lost \(name)'s profile after the flip to \(expected)")
            XCTAssertTrue(gameLogs.waitForExistence(timeout: 5) && gameLogs.isSelected,
                          "\(name)'s profile left Game Logs after the flip to \(expected)")
            app.tabBars.buttons["Search"].tap()
            XCTAssertTrue(app.navigationBars[searchTitle].waitForExistence(timeout: 5),
                          "Search lost \(searchTitle) after the flip to \(expected)")
            app.tabBars.buttons["Home"].tap()
            XCTAssertTrue(app.navigationBars[homeTitle].waitForExistence(timeout: 5),
                          "Home lost \(homeTitle) after the flip to \(expected)")
            attachScreenshot(app, "d\(i + 1) after flip to \(expected), on Home")
        }

        // The profile's Career tab: still on its Postseason table.
        app.tabBars.buttons["Leaders"].tap()
        app.buttons["Career"].firstMatch.tap()
        XCTAssertTrue(postseason.waitForExistence(timeout: 5) && postseason.isSelected,
                      "\(name)'s Career scope didn't survive the flips")

        // Back to the Leaders root: still on Career.
        app.navigationBars.firstMatch.buttons.element(boundBy: 0).tap()
        XCTAssertTrue(leadersRoot.waitForExistence(timeout: 5))
        XCTAssertTrue(career.waitForExistence(timeout: 5))
        XCTAssertTrue(career.isSelected, "the Leaders filter didn't survive the flips")
    }

    /// The title of the screen just pushed: the visible navigation bar, once it
    /// is no longer the tab's root.
    private func pushedTitle(_ app: XCUIApplication, root: String?) -> String {
        let bar = app.navigationBars.firstMatch
        let deadline = Date().addingTimeInterval(10)
        while Date() < deadline {
            if bar.exists, !bar.identifier.isEmpty, bar.identifier != root,
               bar.buttons.count > 0 { return bar.identifier }
            usleep(200_000)
        }
        XCTFail("nothing was pushed (still \(bar.identifier))")
        return bar.identifier
    }

    private func attachScreenshot(_ app: XCUIApplication, _ name: String) {
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = name
        shot.lifetime = .keepAlways
        add(shot)
    }
}
