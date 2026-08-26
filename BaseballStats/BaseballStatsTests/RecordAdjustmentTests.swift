//
//  RecordAdjustmentTests.swift
//  BaseballStatsTests
//
//  The today-adjustment folds a finished-but-not-yet-official game into the
//  favourite's record. Its base is BDL's standings; its cutoff used to be our
//  backend's `last_updated`. Two services, so once BDL absorbed a final the
//  base already held the win and the delta added it again — the record climbed
//  by one every time Home was re-entered.
//
//  Dropping the adjustment was the obvious fix and it was wrong. Measured
//  2026-08-26: a game left the live feed at 19:15:42Z and BDL's standings had
//  still not moved 102 minutes later (one observation, a LOWER bound). Without
//  the adjustment the record simply sits stale for hours.
//
//  So the adjustment stays and the CUTOFF changes — from a clock to a count.
//  These are the four cases, as pure functions, because the window in which the
//  bug is visible is hard to catch live and impossible to catch reliably.
//

import Foundation
import Testing
@testable import BaseballStats

// MARK: - Builders

private func team(_ id: Int, _ abbr: String) -> BDLTeam {
    BDLTeam(id: id, slug: abbr.lowercased(), abbreviation: abbr,
            displayName: abbr, shortDisplayName: abbr, name: abbr,
            location: abbr, league: "American", division: "East")
}

/// A finished game at a fixed instant, with a decided score.
private func finalGame(
    id: Int, homeId: Int, awayId: Int,
    homeRuns: Int, awayRuns: Int, iso: String,
) -> Game {
    BDLGame(
        id: id,
        homeTeam: team(homeId, "HOM"), awayTeam: team(awayId, "AWY"),
        homeTeamData: BDLTeamData(hits: nil, runs: homeRuns, errors: nil, inningScores: nil),
        awayTeamData: BDLTeamData(hits: nil, runs: awayRuns, errors: nil, inningScores: nil),
        date: iso, status: "STATUS_FINAL", venue: nil, period: 9, displayClock: nil,
        scoringSummary: nil, season: 2026, seasonType: "regular", postseason: false,
        homeTeamName: "Home", awayTeamName: "Away",
    ).toGame()
}

private let kHome = 10          // BDL team ids under test
private let kAway = 20
/// 2026-08-26, 7:10pm ET = 23:10Z. `now` sits after it, same ET day.
private let kGameISO = "2026-08-26T23:10:00.000Z"
private let kNow = ISO8601DateFormatter().date(from: "2026-08-27T01:30:00Z")!
/// The backend's stamp, BEFORE the game — so the game is post-cutoff.
private let kAnchorStamp = "2026-08-26T15:06:50.517578Z"

private func record(_ w: Int, _ l: Int) -> TeamRecord {
    TeamRecord(wins: w, losses: l, pct: nil)
}

@MainActor
struct RecordAdjustmentTests {

    private static var oneFinal: [Game] {
        [finalGame(id: 1, homeId: kHome, awayId: kAway,
                   homeRuns: 5, awayRuns: 2, iso: kGameISO)]
    }

    /// 1. The base is BEHIND — its games-played count does not yet include
    /// tonight's final — so the delta must add it. This is the case the
    /// adjustment exists for, and the 102-minute measurement says it is the
    /// common one, not the edge.
    @Test func addsTheGameWhenTheBaseHasNotAbsorbedIt() {
        // Anchor 132 games; the base still reads 132, so nothing absorbed.
        let deltas = TodayRecordAdjustments.deltas(
            from: Self.oneFinal, lastUpdated: kAnchorStamp,
            absorption: .anchored(
                gamesPlayed: [kHome: 132, kAway: 132],
                current:     [kHome: record(78, 54), kAway: record(62, 70)]),
            now: kNow,
        )
        #expect(deltas[kHome]?.wDelta == 1, "the winner gains the win")
        #expect(deltas[kAway]?.lDelta == 1, "the loser gains the loss")

        let applied = TodayRecordAdjustments.apply(
            deltas, to: [kHome: record(78, 54), kAway: record(62, 70)])
        #expect(applied[kHome]?.wins == 79)
        #expect(applied[kAway]?.losses == 71)
    }

    /// 2. THE BUG. The base has caught up — its count already includes tonight's
    /// game — so the delta must add NOTHING. Under the old clock-based cutoff
    /// this returned a full delta, because the backend's stamp still predated
    /// the game even though BDL's numbers no longer did.
    @Test func addsNothingOnceTheBaseAlreadyReflectsIt() {
        // Base has moved to 79-54 / 62-71: 133 games, one past the anchor.
        let deltas = TodayRecordAdjustments.deltas(
            from: Self.oneFinal, lastUpdated: kAnchorStamp,
            absorption: .anchored(
                gamesPlayed: [kHome: 132, kAway: 132],
                current:     [kHome: record(79, 54), kAway: record(62, 71)]),
            now: kNow,
        )
        #expect(deltas.isEmpty, "already absorbed — adding again is the bug: got \(deltas)")
    }

    /// 3. Idempotence, stated directly: feed the result of one application back
    /// in and the answer must not move. This is what "re-entering Home" does.
    @Test func applyingTwiceMatchesApplyingOnce() {
        let base: [Int: TeamRecord] = [kHome: record(78, 54), kAway: record(62, 70)]
        let anchor: [Int: Int] = [kHome: 132, kAway: 132]

        let first = TodayRecordAdjustments.apply(
            TodayRecordAdjustments.deltas(
                from: Self.oneFinal, lastUpdated: kAnchorStamp,
                absorption: .anchored(gamesPlayed: anchor, current: base),
                now: kNow),
            to: base)

        // Second pass sees the ALREADY-ADJUSTED dict as `current`, exactly as a
        // re-entry would after the first pass wrote it.
        let second = TodayRecordAdjustments.apply(
            TodayRecordAdjustments.deltas(
                from: Self.oneFinal, lastUpdated: kAnchorStamp,
                absorption: .anchored(gamesPlayed: anchor, current: first),
                now: kNow),
            to: first)

        #expect(first[kHome]?.wins == 79)
        #expect(second[kHome]?.wins == first[kHome]?.wins,
                "second pass moved the record: \(String(describing: second[kHome]?.wins))")
        #expect(second[kAway]?.losses == first[kAway]?.losses)
    }

    /// 4. The standings fetch failed, so there is no anchor. The old code went
    /// on adding to whatever `teamRecords` already held — with the base never
    /// rebuilt, that compounded once per load with no ceiling. With no anchor
    /// there is now no delta at all.
    @Test func addsNothingWhenTheAnchorIsMissing() {
        let base: [Int: TeamRecord] = [kHome: record(78, 54), kAway: record(62, 70)]
        let deltas = TodayRecordAdjustments.deltas(
            from: Self.oneFinal, lastUpdated: nil,
            absorption: .anchored(gamesPlayed: [:], current: base),   // fetch returned nil
            now: kNow,
        )
        #expect(deltas.isEmpty, "no anchor means no measurement, so nothing may be added")

        // And the compounding path specifically: repeated loads cannot climb.
        var carried = base
        for _ in 0..<5 {
            carried = TodayRecordAdjustments.apply(
                TodayRecordAdjustments.deltas(
                    from: Self.oneFinal, lastUpdated: nil,
                    absorption: .anchored(gamesPlayed: [:], current: carried),
                    now: kNow),
                to: carried)
        }
        #expect(carried[kHome]?.wins == 78, "five loads with no anchor must not move the record")
    }

    /// The unanchored path is still the old behaviour, on purpose — Standings
    /// uses it and is correct, because its base and cutoff are one response.
    @Test func unanchoredStillAppliesEveryPostCutoffFinal() {
        let deltas = TodayRecordAdjustments.deltas(
            from: Self.oneFinal, lastUpdated: kAnchorStamp,
            absorption: .unanchored, now: kNow,
        )
        #expect(deltas[kHome]?.wDelta == 1)
    }

    /// STANDINGS IS UNCHANGED. `.unanchored` must drop nothing, so the filtered
    /// set `recalculateGB` now walks is the whole post-cutoff set it walked
    /// before the parameter existed. Asserted on the set itself rather than on
    /// the formatted output, because that is where a regression would enter.
    @Test func unanchoredDropsNothingSoStandingsIsUnaffected() {
        let games = [
            finalGame(id: 1, homeId: kHome, awayId: kAway,
                      homeRuns: 5, awayRuns: 2, iso: kGameISO),
            finalGame(id: 2, homeId: kAway, awayId: kHome,
                      homeRuns: 1, awayRuns: 4, iso: "2026-08-26T23:40:00.000Z"),
        ]
        let unanchored = TodayRecordAdjustments.unabsorbedResultsByTeam(
            from: games, lastUpdated: kAnchorStamp, absorption: .unanchored, now: kNow)
        #expect(unanchored[kHome]?.count == 2, "both of the day's finals survive")
        #expect(unanchored[kAway]?.count == 2)
        #expect(TodayRecordAdjustments.absorbedCount(.unanchored, teamId: kHome) == 0)

        // And it equals the anchored set when the base has absorbed nothing —
        // the two paths only diverge once the base actually moves.
        let anchoredZero = TodayRecordAdjustments.unabsorbedResultsByTeam(
            from: games, lastUpdated: kAnchorStamp,
            absorption: .anchored(gamesPlayed: [kHome: 132, kAway: 132],
                                  current: [kHome: record(78, 54), kAway: record(62, 70)]),
            now: kNow)
        #expect(anchoredZero[kHome]?.count == unanchored[kHome]?.count)
        #expect(anchoredZero[kAway]?.count == unanchored[kAway]?.count)
    }

    /// THE STREAK MUST AGREE WITH THE RECORD. `recalculateGB` rolls the streak
    /// forward through the same filtered set the deltas came from, so a game
    /// the base has already absorbed is dropped from BOTH or neither. Shipping
    /// the record fix while the streak kept rolling an absorbed game forward
    /// would have left the pill one game ahead of the number beside it.
    @Test func streakWalksTheSameGamesTheRecordDoes() {
        let absorbedBase = TodayRecordAdjustments.Absorption.anchored(
            gamesPlayed: [kHome: 132, kAway: 132],
            current:     [kHome: record(79, 54), kAway: record(62, 71)])   // caught up

        let deltas = TodayRecordAdjustments.deltas(
            from: Self.oneFinal, lastUpdated: kAnchorStamp,
            absorption: absorbedBase, now: kNow)
        #expect(deltas.isEmpty, "record adds nothing")

        let set = TodayRecordAdjustments.unabsorbedResultsByTeam(
            from: Self.oneFinal, lastUpdated: kAnchorStamp,
            absorption: absorbedBase, now: kNow)
        #expect(set.isEmpty, "so the streak must walk nothing either — got \(set)")

        // recalculateGB short-circuits on empty adjustments, which is the same
        // answer arrived at from the other side.
        let gb = TodayRecordAdjustments.recalculateGB(
            standings: [], games: Self.oneFinal, adjustments: deltas,
            lastUpdated: kAnchorStamp, absorption: absorbedBase, now: kNow)
        #expect(gb.isEmpty)
    }
}
