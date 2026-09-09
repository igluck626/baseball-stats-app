//
//  OverlayDecisionTests.swift
//  BaseballStatsTests
//
//  The three branches of the season-row overlay.
//
//  ⚠️ THE MIDDLE BRANCH IS THE ONE THAT MATTERS. `behind > 0` failing
//  shows a stale number, which a reader can at least notice against a
//  box score. `behind == 0` failing adds a game the season row already
//  counts, and a doubled line reads as a plausible stat and is wrong —
//  the failure nobody catches. So the fully-absorbed control is the
//  load-bearing test here, not the two that fix Isaac's report.
//
//  Numbers are real, from 2026-09-09. TEX @ SEA (2026-09-08) had not
//  been absorbed into balldontlie's season aggregate 11 hours after it
//  ended, while our per-game gamelog had it:
//
//    Bryce Miller     season row G=18  ERA 4.0135 | gamelog 19 rows
//                     + 3.667 IP, 4 ER            -> ERA 4.2215 ("4.22")
//    Dominic Canzone  season row G=128 AVG .2543  | gamelog 129 rows
//                     + 5 AB, 2 H                 -> AVG .2561 (".256")
//
//  Tarik Skubal pitched the same night in a game that WAS absorbed —
//  season row and gamelog agree — and must gain nothing.
//

import Foundation
import Testing
@testable import BaseballStats

@Suite("Overlay decision")
struct OverlayDecisionTests {

    /// A pitcher whose side has no batting data — the batting side must
    /// not veto.
    private func pitcherOnly(seasonG: Int?, bdlG: Int?, gamelog: Int?,
                             includesToday: Bool) -> OverlayDecision {
        OverlayDecision.decide(
            batting: .init(seasonG: nil, bdlG: nil, gamelogGames: nil, includesToday: false),
            pitching: .init(seasonG: seasonG, bdlG: bdlG,
                            gamelogGames: gamelog, includesToday: includesToday),
        )
    }

    private func batterOnly(seasonG: Int?, bdlG: Int?, gamelog: Int?,
                            includesToday: Bool) -> OverlayDecision {
        OverlayDecision.decide(
            batting: .init(seasonG: seasonG, bdlG: bdlG,
                           gamelogGames: gamelog, includesToday: includesToday),
            pitching: .init(seasonG: nil, bdlG: nil, gamelogGames: nil, includesToday: false),
        )
    }

    // MARK: behind > 0 — the reported bug

    @Test func millerIsOneGameBehindAndOverlaysExactlyOne() {
        // ⚠️ includesToday is TRUE — our gamelog HAS the game. Under the
        // old boolean gate that alone suppressed the overlay, which is
        // precisely why the stale 4.01 rendered.
        let d = pitcherOnly(seasonG: 18, bdlG: 18, gamelog: 19, includesToday: true)
        #expect(d.shouldOverlayFinals, "the season row is a game behind and must be topped up")
        #expect(d.budget == 1, "exactly one game is missing")
    }

    @Test func canzoneIsOneGameBehindOnTheBattingSide() {
        let d = batterOnly(seasonG: 128, bdlG: 128, gamelog: 129, includesToday: true)
        #expect(d.shouldOverlayFinals)
        #expect(d.budget == 1)
    }

    // MARK: behind == 0 — ⚠️ the double-count guard

    @Test func afullyAbsorbedPlayerGainsNothing() {
        // Skubal: the aggregate already counts the game our gamelog has.
        let d = pitcherOnly(seasonG: 23, bdlG: 23, gamelog: 23, includesToday: true)
        #expect(!d.shouldOverlayFinals, "a current season row must not be topped up")
        #expect(d.budget == nil)
    }

    @Test func aCurrentRowIsNotOverlaidEvenBeforeTheGamelogCatchesUp() {
        // Same count, but the gamelog hasn't absorbed today yet. Still
        // zero behind, so still nothing to add.
        let d = pitcherOnly(seasonG: 23, bdlG: 23, gamelog: 23, includesToday: false)
        #expect(!d.shouldOverlayFinals)
    }

    // MARK: behind < 0 — the BDL-direct path, untouched here

    @Test func anAggregateAheadOfOurGamelogDoesNotOverlay() {
        // balldontlie absorbed a game our gamelog lacks. This decision
        // declines; `shouldUseBDLDirect` in the view model handles it.
        let d = pitcherOnly(seasonG: 24, bdlG: 24, gamelog: 23, includesToday: false)
        #expect(!d.shouldOverlayFinals, "overlay must not fire when the row is AHEAD")
        #expect(d.budget == nil, "a negative shortfall is not a budget")
    }

    // MARK: the fallback, and two-way players

    @Test func noCountFallsBackToTheBooleanThatShippedBefore() {
        // Older backend: no `gamelog_games`. Old semantics exactly.
        #expect(pitcherOnly(seasonG: 18, bdlG: 18, gamelog: nil,
                            includesToday: false).shouldOverlayFinals)
        #expect(!pitcherOnly(seasonG: 18, bdlG: 18, gamelog: nil,
                             includesToday: true).shouldOverlayFinals)
        #expect(!pitcherOnly(seasonG: 18, bdlG: 19, gamelog: nil,
                             includesToday: false).shouldOverlayFinals)
    }

    @Test func aSilentSideDoesNotVetoTheOther() {
        // A pure pitcher's batting side is all nil and must not block.
        #expect(pitcherOnly(seasonG: 18, bdlG: 18, gamelog: 19,
                            includesToday: true).shouldOverlayFinals)
    }

    @Test func aCountedSideReadingZeroDoesBlock() {
        // ⚠️ The distinction that makes the guard work: silent is not the
        // same as "says no". A two-way player current at the plate and
        // behind on the mound must not have his batting line topped up,
        // so the whole overlay declines.
        let d = OverlayDecision.decide(
            batting: .init(seasonG: 100, bdlG: 100, gamelogGames: 100, includesToday: true),
            pitching: .init(seasonG: 18, bdlG: 18, gamelogGames: 19, includesToday: true),
        )
        #expect(!d.shouldOverlayFinals)
    }

    @Test func theBudgetTakesTheLargerShortfall() {
        let d = OverlayDecision.decide(
            batting: .init(seasonG: 100, bdlG: 100, gamelogGames: 102, includesToday: true),
            pitching: .init(seasonG: 18, bdlG: 18, gamelogGames: 19, includesToday: true),
        )
        #expect(d.budget == 2, "taking the smaller would leave the batting row short")
    }

    // MARK: bounding

    @Test func boundingKeepsAtMostTheBudgetAndDedupesByGame() {
        let games = [(id: 1, live: false), (id: 1, live: false),
                     (id: 2, live: false), (id: 3, live: false)]
        let kept = OverlayDecision.boundedGames(
            games, budget: 1, isLive: { $0.live }, gameId: { $0.id })
        #expect(kept.map(\.id) == [1], "a duplicate row and an extra final both dropped")
    }

    @Test func liveGamesAreNeverBoundedAway() {
        // The count describes FINALS the aggregate has not absorbed; a
        // game in progress is in neither, so it always applies.
        let games = [(id: 1, live: false), (id: 2, live: true), (id: 3, live: false)]
        let kept = OverlayDecision.boundedGames(
            games, budget: 1, isLive: { $0.live }, gameId: { $0.id })
        #expect(kept.map(\.id) == [1, 2])
    }

    @Test func noBudgetLeavesEveryGame() {
        let games = [(id: 1, live: false), (id: 2, live: false)]
        let kept = OverlayDecision.boundedGames(
            games, budget: nil, isLive: { $0.live }, gameId: { $0.id })
        #expect(kept.count == 2)
    }

    // MARK: the arithmetic the overlay must produce

    /// ⚠️ Rates must come from COMBINED TOTALS, never from averaging a
    /// season rate with a game rate — that is where an overlay quietly
    /// goes wrong. These are the numbers the profile renders.
    @Test func ratesDeriveFromCombinedTotals() {
        // Miller: ERA = ER * 9 / IP over the summed line.
        let ip = 98.667 + 3.667, er = Double(44 + 4)
        #expect(abs(er * 9.0 / ip - 4.2215) < 0.001)
        #expect(String(format: "%.2f", er * 9.0 / ip) == "4.22")
        // and NOT the average of the two ERAs, which is the wrong shape
        let wrong = (4.0135 + (4.0 * 9.0 / 3.667)) / 2.0
        #expect(abs(wrong - 4.2215) > 1.0, "averaging rates gives a wildly different answer")

        // Canzone: AVG = H / AB over the summed line.
        let avg = Double(103 + 2) / Double(405 + 5)
        #expect(String(format: "%.3f", avg) == "0.256")
    }
}
