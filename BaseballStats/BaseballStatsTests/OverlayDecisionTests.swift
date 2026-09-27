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

    private func side(_ seasonG: Int?, _ bdlG: Int?, rows: Int?,
                      today: Bool, rowHasToday: Bool? = nil) -> OverlayDecision.Side {
        .init(seasonG: seasonG, bdlG: bdlG, gamelogRows: rows,
              includesToday: today, seasonRowIncludesToday: rowHasToday)
    }
    private var noSide: OverlayDecision.Side {
        .init(seasonG: nil, bdlG: nil, gamelogRows: nil,
              includesToday: false, seasonRowIncludesToday: nil)
    }
    private func batter(_ g: Int?, _ b: Int?, rows: Int?, today: Bool,
                        rowHasToday: Bool? = nil) -> OverlayDecision {
        .decide(batting: side(g, b, rows: rows, today: today, rowHasToday: rowHasToday),
                pitching: noSide)
    }
    private func pitcher(_ g: Int?, _ b: Int?, rows: Int?, today: Bool,
                         rowHasToday: Bool? = nil) -> OverlayDecision {
        .decide(batting: noSide,
                pitching: side(g, b, rows: rows, today: today, rowHasToday: rowHasToday))
    }

    // ── a. clause 2: the same-day regression ──────────────────────────

    /// Juan Soto, 2026-09-27. The day's final ended after the nightly, so
    /// it is in neither source: our row matches balldontlie's at 110 and
    /// the gamelog has no row for the date. Counting ROWS his 110 equal
    /// his G exactly — `behind` is 0, not negative as the deduped count
    /// made it look — so clause 1 cannot fire and clause 2 must.
    @Test func sotoSameDayFinalInNeitherSourceOverlays() {
        let d = batter(110, 110, rows: 110, today: false)
        #expect(d.shouldOverlayFinals)
    }

    // ── b. rows, not dates ────────────────────────────────────────────

    /// Bo Bichette, three doubleheaders: 159 rows across 156 dates
    /// against a season G of 159. Counting dates read -3 and was called
    /// an ingest hole; counting rows reads 0, which is the truth — his
    /// gamelog is complete. The assertion is that `behind` is NOT
    /// negative, because a negative here would silence the side.
    @Test func doubleheaderRowsAreNotAShortfall() {
        let s = side(159, 159, rows: 159, today: true)
        #expect(s.behind == 0, "rows must be counted, not distinct dates")
        #expect(s.vote == .refuse, "level and already absorbed is a definite no, not an abstention")
    }

    // ── c. clause 1: the provider is late ─────────────────────────────

    /// Bryce Miller, 2026-09-09. Our gamelog had the game and
    /// balldontlie's season row did not: 19 rows against G=18. Note
    /// `today: true` — the gamelog HAS the date, which is what made the
    /// old boolean gate refuse.
    @Test func millerProviderLateOverlaysViaTheCount() {
        let d = pitcher(18, 18, rows: 19, today: true)
        #expect(d.shouldOverlayFinals)
        #expect(d.budget == 1)
    }

    // ── d. a phantom side must not veto ───────────────────────────────

    /// Ezequiel Duran. His batting side is a game behind and due a
    /// correction; his pitching row reads G=4 with zero pitching gamelog
    /// rows — a 2025 line balldontlie carried into the 2026 season row —
    /// giving `behind = -4`. That side has no vote and must not silence
    /// the batting fix.
    @Test func phantomPitchingSideDoesNotVetoBattingCorrection() {
        let d = OverlayDecision.decide(
            batting: side(135, 135, rows: 136, today: true),
            pitching: side(4, 4, rows: 0, today: false))
        #expect(d.shouldOverlayFinals)
        #expect(d.budget == 1, "the budget comes from the side actually behind")
    }

    // ── e. the double-count guard on clause 1 ─────────────────────────

    /// Behind on OLD games while the season row already counts today.
    /// Clause 1 would otherwise hand today's final over a second time.
    /// ⚠️ No caller supplies `seasonRowIncludesToday` yet — see the note
    /// on `Side` — so this pins the parameter's behaviour, not a path
    /// currently exercised in production.
    @Test func aSeasonRowThatAlreadyHasTodayIsNotGivenItTwice() {
        let d = batter(100, 100, rows: 101, today: true, rowHasToday: true)
        #expect(!d.shouldOverlayFinals)
        // and with it unknown, the overlay proceeds
        let unknown = batter(100, 100, rows: 101, today: true, rowHasToday: nil)
        #expect(unknown.shouldOverlayFinals)
    }

    // ── e2. the derivation itself ─────────────────────────────────────

    /// ⚠️ The guard is only as good as the stamp it reads. A season row
    /// written BEFORE first pitch cannot contain the game, so clause 1
    /// stands; one written after it may, so clause 1 stands down.
    @Test func theSeasonRowStampDecidesWhetherTodayIsAlreadyCounted() {
        let firstPitch = Date(timeIntervalSince1970: 1_000_000)
        let before = firstPitch.addingTimeInterval(-3600)
        let after  = firstPitch.addingTimeInterval(+3600)

        #expect(OverlayDecision.seasonRowIncludes(written: before, firstPitch: firstPitch) == false)
        #expect(OverlayDecision.seasonRowIncludes(written: after,  firstPitch: firstPitch) == true)
        #expect(OverlayDecision.seasonRowIncludes(written: nil,    firstPitch: firstPitch) == nil)
        #expect(OverlayDecision.seasonRowIncludes(written: after,  firstPitch: nil) == nil)

        // written before first pitch, a game behind -> overlays
        let stale = batter(100, 100, rows: 101, today: true,
                           rowHasToday: OverlayDecision.seasonRowIncludes(
                               written: before, firstPitch: firstPitch))
        #expect(stale.shouldOverlayFinals)
        // written after the final -> clause 1 stands down, and with the
        // date already in the gamelog clause 2 cannot fire either
        let fresh = batter(100, 100, rows: 101, today: true,
                           rowHasToday: OverlayDecision.seasonRowIncludes(
                               written: after, firstPitch: firstPitch))
        #expect(!fresh.shouldOverlayFinals)
    }

    // ── h. abstention decides nothing ─────────────────────────────────

    /// ⚠️ A side with no gamelog count AND no BDL comparison must
    /// ABSTAIN, never fire on its own — and two abstentions must not add
    /// up to a yes. The previous Bool combination read
    /// `(silent || allows) && (silent || allows)`, so a player with no
    /// season row on either side produced `true && true` and had the
    /// day's finals applied to nothing.
    @Test func twoAbstainingSidesProduceNoOverlay() {
        #expect(noSide.vote == .abstain)
        #expect(!OverlayDecision.decide(batting: noSide, pitching: noSide).shouldOverlayFinals)
        #expect(OverlayDecision.decide(batting: noSide, pitching: noSide).budget == nil)
        // one abstention beside one firing side still overlays
        #expect(OverlayDecision.decide(
            batting: side(110, 110, rows: 110, today: false), pitching: noSide
        ).shouldOverlayFinals)
        // and a refusal outranks a fire
        #expect(!OverlayDecision.decide(
            batting: side(110, 110, rows: 110, today: false),
            pitching: side(23, 23, rows: 23, today: true)
        ).shouldOverlayFinals)
    }

    // ── f. already current ────────────────────────────────────────────

    /// Both sources have the game: the gamelog holds the date and the
    /// row counts it. Nothing to add, and adding would double-count.
    @Test func aFullyAbsorbedPlayerGainsNothing() {
        let d = pitcher(23, 23, rows: 23, today: true)
        #expect(!d.shouldOverlayFinals)
        #expect(d.budget == nil)
    }

    // ── g. the dropped zero-stat row ──────────────────────────────────

    /// Corey Seager, 2026-06-30: he appeared at shortstop with PA=0, MLB
    /// counted the game in G, and our ingest wrote no row. His gamelog
    /// therefore sits a row BELOW his season G all season. Today's final
    /// is in neither source, so clause 1 reads negative and is silent
    /// while clause 2 still fires — which is the whole reason the second
    /// clause exists.
    @Test func aDroppedZeroStatRowStillOverlaysViaClauseTwo() {
        let d = batter(101, 101, rows: 100, today: false)
        #expect(d.shouldOverlayFinals, "a short gamelog must not suppress a final neither source has")
        #expect(d.budget == nil, "a negative shortfall is never a budget")
    }

    // ── the pieces those seven rest on ────────────────────────────────

    @Test func aSilentSideDoesNotVetoTheOther() {
        #expect(pitcher(18, 18, rows: 19, today: true).shouldOverlayFinals)
    }

    @Test func noCountFallsBackToTheBooleanThatShippedBefore() {
        #expect(pitcher(18, 18, rows: nil, today: false).shouldOverlayFinals)
        #expect(!pitcher(18, 18, rows: nil, today: true).shouldOverlayFinals)
    }

    @Test func theBudgetTakesTheLargerShortfall() {
        let d = OverlayDecision.decide(
            batting: side(100, 100, rows: 102, today: true),
            pitching: side(18, 18, rows: 19, today: true))
        #expect(d.budget == 2)
    }

    @Test func boundingKeepsAtMostTheBudgetAndDedupesByGame() {
        let games = [(id: 1, live: false), (id: 1, live: false),
                     (id: 2, live: false), (id: 3, live: false)]
        #expect(OverlayDecision.boundedGames(games, budget: 1,
                isLive: { $0.live }, gameId: { $0.id }).map(\.id) == [1])
    }

    @Test func liveGamesAreNeverBoundedAway() {
        let games = [(id: 1, live: false), (id: 2, live: true), (id: 3, live: false)]
        #expect(OverlayDecision.boundedGames(games, budget: 1,
                isLive: { $0.live }, gameId: { $0.id }).map(\.id) == [1, 2])
    }

    @Test func noBudgetLeavesEveryGame() {
        let games = [(id: 1, live: false), (id: 2, live: false)]
        #expect(OverlayDecision.boundedGames(games, budget: nil,
                isLive: { $0.live }, gameId: { $0.id }).count == 2)
    }

    /// ⚠️ Rates come from COMBINED TOTALS, never from averaging a season
    /// rate with a game rate — where an overlay quietly goes wrong.
    @Test func ratesDeriveFromCombinedTotals() {
        let ip = 98.667 + 3.667, er = Double(44 + 4)
        #expect(String(format: "%.2f", er * 9.0 / ip) == "4.22")
        let wrong = (4.0135 + (4.0 * 9.0 / 3.667)) / 2.0
        #expect(abs(wrong - 4.2215) > 1.0, "averaging rates gives a wildly different answer")
        #expect(String(format: "%.3f", Double(103 + 2) / Double(405 + 5)) == "0.256")
    }
}
