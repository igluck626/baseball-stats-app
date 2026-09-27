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
//  ⚠️ A REAL FIXTURE CAN STILL BE AN UNREPRESENTATIVE ONE. Every case
//  here was drawn from real payload and the suite passed — and it passed
//  while a regression shipped, because every player it happened to name
//  had a COMPLETE gamelog. Our gamelog carries per-player holes of 0 to 3
//  games (measured across seven players on 2026-09-27, three negative),
//  and none of the original fixtures had one. The count read clean, the
//  arithmetic looked sound, and every same-day final stopped being
//  corrected.
//
//  So the cases below deliberately include shapes no single game would
//  hand you together: a structural hole, a same-day final absent from
//  both sources, and both at once. Real data tells you what happened;
//  it does not tell you what can happen.
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

    /// ⚠️ THIS ASSERTION WAS REVERSED, and it is worth knowing why: it
    /// used to expect NO overlay here, and that expectation is the bug.
    /// Same count with today's date absent from the gamelog does not mean
    /// "nothing to add" — it means nothing has absorbed today's game yet,
    /// which is precisely when it must be added. The test encoded the
    /// regression and passed while every same-day final went uncorrected.
    @Test func aCurrentRowWithTodayInNeitherSourceIsOverlaid() {
        let d = pitcherOnly(seasonG: 23, bdlG: 23, gamelog: 23, includesToday: false)
        #expect(d.shouldOverlayFinals)
        // and once the gamelog HAS the date, both sources agree and it stops
        let absorbed = pitcherOnly(seasonG: 23, bdlG: 23, gamelog: 23, includesToday: true)
        #expect(!absorbed.shouldOverlayFinals)
    }

    // MARK: ⚠️ same-day finals — the case the first version regressed

    /// A game that finished AFTER the nightly ran is in neither source:
    /// our season row still matches balldontlie's, and our gamelog has no
    /// row for the date. The count alone says nothing — it is the second
    /// clause that must fire.
    @Test func aFinalThatEndedAfterTheNightlyIsOverlaid() {
        // clean case: no structural hole, count reads level
        let d = batterOnly(seasonG: 110, bdlG: 110, gamelog: 110, includesToday: false)
        #expect(d.shouldOverlayFinals, "a same-day final in neither source must be added")
    }

    /// ⚠️ THE ACTUAL REGRESSION. Juan Soto, 2026-09-27: our row and
    /// balldontlie's both read 110 while the gamelog held 109 dates — a
    /// structural hole — and the day's final was in neither. The count
    /// reads NEGATIVE, so the first clause cannot fire and the second
    /// must. Bichette and Benge read -3 the same night.
    @Test func aStructuralHoleDoesNotBlockTheSameDayClause() {
        for (name, gamelog, seasonG) in [("Soto", 109, 110), ("Bichette", 156, 159),
                                         ("Benge", 152, 155)] {
            let d = batterOnly(seasonG: seasonG, bdlG: seasonG,
                               gamelog: gamelog, includesToday: false)
            #expect(d.shouldOverlayFinals,
                    "\(name): a short gamelog must not suppress a final neither source has")
        }
    }

    /// The same hole must not veto the OTHER side either — the Duran
    /// shape, now with a negative count rather than a zero.
    @Test func aNegativeSideDoesNotVetoAValidCorrection() {
        // batting is a game behind and due a correction; pitching is a
        // phantom row (G = 4, no gamelog rows at all) reading -4.
        let d = OverlayDecision.decide(
            batting: .init(seasonG: 135, bdlG: 135, gamelogGames: 136, includesToday: true),
            pitching: .init(seasonG: 4, bdlG: 4, gamelogGames: 0, includesToday: false),
        )
        #expect(d.shouldOverlayFinals, "a phantom pitching row vetoed a valid batting fix")
        #expect(d.budget == 1, "the budget comes from the side that is actually behind")
    }

    /// ⚠️ THE KNOWN HOLE, pinned so it is not mistaken for solved. A
    /// Miller-shaped player whose gamelog ALSO has a structural hole:
    /// +1 and -1 cancel to `behind == 0`, and the gamelog HAS the game so
    /// the second clause cannot fire either. Milner, Seager, Foscue and
    /// Montes on the TEX @ SEA night. If this test ever starts failing,
    /// someone has fixed it — update the comment on `allowsOverlay`.
    @Test func aCancellingHoleStillLeavesTheRowStale() {
        let d = batterOnly(seasonG: 85, bdlG: 85, gamelog: 85, includesToday: true)
        #expect(!d.shouldOverlayFinals,
                "this is the documented limit, not a passing case")
    }

    // MARK: behind < 0 — the BDL-direct path, untouched here

    @Test func aNegativeShortfallIsNeverABudget() {
        // ⚠️ This used to assert the overlay DECLINED here, and that
        // assertion was wrong twice over: a negative count means our
        // GAMELOG trails, which says nothing about whether the season row
        // has today's game — and treating it as a refusal is what
        // regressed every same-day final. It contributes no budget, and
        // the second clause decides.
        let d = pitcherOnly(seasonG: 24, bdlG: 24, gamelog: 23, includesToday: false)
        #expect(d.budget == nil, "a negative shortfall is not a budget")
        #expect(d.shouldOverlayFinals, "the second clause applies: the row matches BDL and the gamelog lacks the date")
        // with the date already in the gamelog, neither clause fires
        let absorbed = pitcherOnly(seasonG: 24, bdlG: 24, gamelog: 23, includesToday: true)
        #expect(!absorbed.shouldOverlayFinals)
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
