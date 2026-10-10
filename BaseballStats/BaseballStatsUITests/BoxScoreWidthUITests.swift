//
//  BoxScoreWidthUITests.swift
//  BaseballStatsUITests
//
//  The box score's layout follows the window's width and the text size, and the
//  reader's state survives a change of either: the team they picked, and the
//  card they were reading. Run on an iPad (a 13-inch held upright is 1,032pt,
//  wide enough for both teams at the default size); skipped on a phone, which is
//  never wide upright.
//
//  Launched with `-SizeClassFlipHarness` (DEBUG builds), whose width button
//  draws the window narrower (`-SizeClassFlipHarnessNarrowWidth`, else 430pt).
//  Reads live BDL data: it opens the nearest earlier day's first finished game.
//

import XCTest

final class BoxScoreWidthUITests: XCTestCase {
    override func setUpWithError() throws {
        continueAfterFailure = false
        try XCTSkipUnless(UIDevice.current.userInterfaceIdiom == .pad, "needs a window wide enough for two teams")
        XCUIDevice.shared.orientation = .portrait
    }

    override func tearDown() {
        XCUIDevice.shared.orientation = .portrait
        super.tearDown()
    }

    /// Wide (both teams, no picker) → compact (picker, home by default) → pick
    /// away, read down into the linescore → wide → compact: still away, and
    /// still at the linescore, which is at a different offset in each layout.
    @MainActor
    func testPickedTeamAndReadingPlaceSurviveWideAndCompact() throws {
        let app = launch()
        openFinishedBoxScore(app)
        XCTAssertTrue(isWide(app), "a 13-inch iPad upright should show both teams")
        attach(app, "0 wide")

        let width = app.buttons["harness.flipWidth"]
        width.tap()                                                      // narrow: 430pt
        XCTAssertTrue(app.buttons["narrow"].waitForExistence(timeout: 5))
        let picker = app.segmentedControls.firstMatch
        XCTAssertTrue(picker.waitForExistence(timeout: 5), "no team picker when narrow")
        XCTAssertEqual(batting(app), 1)
        let away = picker.buttons.element(boundBy: 0), home = picker.buttons.element(boundBy: 1)
        XCTAssertTrue(home.isSelected, "a finished game opens on the home team")
        away.tap()
        XCTAssertTrue(away.isSelected)

        // Read down until the linescore card's top has gone under the bar: the
        // reader is in the linescore now, with the picker still below.
        let bar = app.navigationBars.firstMatch
        let linescore = app.staticTexts["Linescore"]
        for _ in 0..<5 where linescore.frame.minY >= bar.frame.maxY {
            drag(app, by: linescore.frame.minY - bar.frame.maxY + 30)
        }
        XCTAssertLessThan(linescore.frame.minY, bar.frame.maxY, "didn't scroll into the linescore")
        XCTAssertGreaterThan(picker.frame.minY, bar.frame.maxY, "scrolled past the linescore")
        sleep(1)                                                        // let the scroll come to rest
        attach(app, "1 narrow, away, reading the linescore")

        width.tap()                                                      // wide again
        XCTAssertTrue(app.buttons["full"].waitForExistence(timeout: 5))
        XCTAssertTrue(waitFor { self.isWide(app) }, "not wide again")
        sleep(1)
        // The linescore card at the top of the screen in the new layout, not
        // wherever the old pixel offset lands (its title sits 14pt into the
        // card) — or as near as the content allows: a shorter layout stops
        // with its last card's foot on screen.
        let atTop = abs(linescore.frame.minY - (bar.frame.maxY + 14)) <= 20
        let lastTotals = app.staticTexts.matching(NSPredicate(format: "label == 'Totals'")).allElementsBoundByIndex
            .map(\.frame.maxY).max() ?? .infinity
        XCTAssertTrue(linescore.isHittable && (atTop || lastTotals <= app.windows.firstMatch.frame.maxY),
                      "wide: linescore title at \(linescore.frame.minY), bar ends at \(bar.frame.maxY), content ends at \(lastTotals)")
        attach(app, "2 wide again, at the linescore")

        width.tap()                                                      // narrow again
        XCTAssertTrue(app.buttons["narrow"].waitForExistence(timeout: 5))
        XCTAssertTrue(picker.waitForExistence(timeout: 5))
        XCTAssertTrue(picker.buttons.element(boundBy: 0).isSelected, "the picked team didn't survive wide")
        sleep(1)
        XCTAssertEqual(linescore.frame.minY, bar.frame.maxY + 14, accuracy: 20,
                       "narrow: linescore title at \(linescore.frame.minY), bar ends at \(bar.frame.maxY)")
        attach(app, "3 narrow again, away, at the linescore")
    }

    /// The same window at a narrower width: 800pt is under the default size's
    /// two-team width (816pt), so one team.
    @MainActor
    func testTheCutoffFollowsWidthNotDevice() throws {
        let app = launch(narrowWidth: 800)
        openFinishedBoxScore(app)
        XCTAssertTrue(isWide(app))
        app.buttons["harness.flipWidth"].tap()
        XCTAssertTrue(app.buttons["narrow"].waitForExistence(timeout: 5))
        XCTAssertTrue(waitFor { !self.isWide(app) && app.segmentedControls.firstMatch.exists },
                      "800pt should show one team with the picker")
        attach(app, "800pt: one team")
    }

    /// Largest standard text: one team upright (two need 1,172pt), both on the
    /// 13-inch iPad's side (1,376pt, the column capped at 1,200).
    @MainActor
    func testLargestStandardTextNeedsTheWiderWindow() throws {
        let app = launch(textSize: "UICTContentSizeCategoryXXXL")
        openFinishedBoxScore(app)
        XCTAssertTrue(waitFor { !self.isWide(app) && app.segmentedControls.firstMatch.exists },
                      "xxxL upright should fall back to one team")
        attach(app, "xxxL upright")
        XCUIDevice.shared.orientation = .landscapeLeft
        XCTAssertTrue(waitFor { self.isWide(app) }, "xxxL on its side should fit both teams")
        attach(app, "xxxL landscape")
    }

    /// Accessibility sizes: always one team, even on the widest window.
    @MainActor
    func testAccessibilitySizesAlwaysShowOneTeam() throws {
        let app = launch(textSize: "UICTContentSizeCategoryAccessibilityM")
        openFinishedBoxScore(app)
        XCUIDevice.shared.orientation = .landscapeLeft
        sleep(2)
        XCTAssertFalse(isWide(app), "AX1 must never show two teams")
        XCTAssertTrue(app.segmentedControls.firstMatch.exists, "AX1 keeps the picker")
        attach(app, "AX1 landscape")
    }

    /// Wide, away's tables come first in the accessibility tree and are drawn
    /// on the left. ⚠️ This is the tree XCUITest walks, in declaration order;
    /// it doesn't apply `accessibilitySortPriority` (swapping the priorities
    /// leaves it passing), so it pins the order VoiceOver starts from, not the
    /// order VoiceOver itself reads.
    @MainActor
    func testAwayComesBeforeHomeInTheAccessibilityTree() throws {
        let app = launch()
        openFinishedBoxScore(app)
        XCTAssertTrue(isWide(app))
        assertAwayFirst(app)
        attach(app, "left-to-right")
    }

    /// Game Leaders, expanded and filtered to the away side, through wide →
    /// medium → wide: still expanded, still filtered.
    @MainActor
    func testGameLeadersStayExpandedAcrossLayouts() throws {
        let app = launch(narrowWidth: 700)                              // medium: one team, 700pt
        openFinishedBoxScore(app)
        XCTAssertTrue(isWide(app))
        let header = app.buttons.matching(NSPredicate(format: "label BEGINSWITH 'GAME LEADERS'")).firstMatch
        XCTAssertTrue(header.waitForExistence(timeout: 10), "no Game Leaders card")
        for _ in 0..<8 where !header.isHittable { drag(app, by: 400) }      // below the tables
        let hardest = app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH 'HARDEST HIT'")).firstMatch
        XCTAssertFalse(hardest.exists, "Game Leaders starts collapsed")
        header.tap()
        XCTAssertTrue(hardest.waitForExistence(timeout: 5), "didn't expand")
        let scope = app.segmentedControls.containing(.button, identifier: "Overall").firstMatch
        XCTAssertTrue(scope.waitForExistence(timeout: 5))
        scope.buttons.element(boundBy: 1).tap()
        XCTAssertTrue(scope.buttons.element(boundBy: 1).isSelected)
        attach(app, "0 wide, expanded, away")

        let width = app.buttons["harness.flipWidth"]
        width.tap()
        XCTAssertTrue(app.buttons["narrow"].waitForExistence(timeout: 5))
        XCTAssertTrue(waitFor { self.batting(app) == 1 }, "not one team at 700pt")
        XCTAssertTrue(hardest.exists, "collapsed by the change to medium")
        XCTAssertTrue(scope.buttons.element(boundBy: 1).isSelected, "scope lost in medium")
        attach(app, "1 medium, still expanded, away")

        width.tap()
        XCTAssertTrue(app.buttons["full"].waitForExistence(timeout: 5))
        XCTAssertTrue(waitFor { self.batting(app) == 2 }, "not wide again")
        XCTAssertTrue(hardest.exists, "collapsed by the change back to wide")
        XCTAssertTrue(scope.buttons.element(boundBy: 1).isSelected, "scope lost in wide")
        attach(app, "2 wide again, still expanded, away")
    }

    // MARK: - Helpers

    /// Away's section comes first in the accessibility tree, and its first
    /// BATTING header too; and it's drawn on the left.
    private func assertAwayFirst(_ app: XCUIApplication, file: StaticString = #filePath, line: UInt = #line) {
        let sections = app.otherElements.matching(NSPredicate(format: "identifier BEGINSWITH 'boxscore.team.'"))
            .allElementsBoundByIndex
        XCTAssertEqual(sections.map(\.identifier), ["boxscore.team.away", "boxscore.team.home"],
                       "accessibility tree order", file: file, line: line)
        let away = app.otherElements["boxscore.team.away"].frame, home = app.otherElements["boxscore.team.home"].frame
        XCTAssertLessThan(away.minX, home.minX, "away should be drawn on the left", file: file, line: line)
        let firstBatting = app.staticTexts.matching(NSPredicate(format: "label == 'BATTING'")).firstMatch.frame
        XCTAssertTrue(away.contains(CGPoint(x: firstBatting.midX, y: firstBatting.midY)),
                      "the first BATTING in the tree isn't away's", file: file, line: line)
    }

    private func launch(narrowWidth: Int? = nil, textSize: String? = nil) -> XCUIApplication {
        let app = XCUIApplication()
        app.launchArguments += ["-SizeClassFlipHarness"]
        if let narrowWidth { app.launchArguments += ["-SizeClassFlipHarnessNarrowWidth", String(narrowWidth)] }
        if let textSize { app.launchArguments += ["-UIPreferredContentSizeCategoryName", textSize] }
        app.launch()
        return app
    }

    /// Scores → the nearest earlier day with a finished game → its box score.
    private func openFinishedBoxScore(_ app: XCUIApplication) {
        app.tabBars.buttons["Scores"].firstMatch.tap()
        let previous = app.buttons["scores.previousDay"]
        XCTAssertTrue(previous.waitForExistence(timeout: 10))
        let finalCard = app.staticTexts.matching(NSPredicate(format: "label ==[c] 'FINAL'")).firstMatch
        var found = false
        for _ in 0..<7 {
            previous.tap()
            if finalCard.waitForExistence(timeout: 15) { found = true; break }
        }
        XCTAssertTrue(found, "no finished game in the last week")
        finalCard.tap()
        let boxScoreButton = app.buttons.matching(NSPredicate(format: "label BEGINSWITH 'Box Score'")).firstMatch
        XCTAssertTrue(boxScoreButton.waitForExistence(timeout: 10))
        boxScoreButton.tap()
        XCTAssertTrue(app.staticTexts["BATTING"].firstMatch.waitForExistence(timeout: 20), "the box score didn't load")
    }

    /// Both teams' tables on screen and no team picker.
    private func isWide(_ app: XCUIApplication) -> Bool {
        batting(app) == 2 && !app.segmentedControls.firstMatch.exists
    }

    private func batting(_ app: XCUIApplication) -> Int {
        app.staticTexts.matching(NSPredicate(format: "label == 'BATTING'")).count
    }

    private func waitFor(_ timeout: TimeInterval = 5, _ condition: @escaping () -> Bool) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return true }
            usleep(200_000)
        }
        return condition()
    }

    /// Scroll the content up by `points`, slowly and held at the end, so it stops
    /// where it was dragged rather than coasting on.
    private func drag(_ app: XCUIApplication, by points: CGFloat) {
        let window = app.windows.firstMatch
        let start = window.coordinate(withNormalizedOffset: CGVector(dx: 0.6, dy: 0.8))
        let end = start.withOffset(CGVector(dx: 0, dy: -points))
        start.press(forDuration: 0.1, thenDragTo: end, withVelocity: .slow, thenHoldForDuration: 0.5)
    }

    private func attach(_ app: XCUIApplication, _ name: String) {
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = name
        shot.lifetime = .keepAlways
        add(shot)
    }
}
