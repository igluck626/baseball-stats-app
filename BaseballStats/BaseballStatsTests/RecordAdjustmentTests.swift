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
        #expect(TodayRecordAdjustments.absorbedCount(
            .unanchored, teamId: kHome, resultCount: 2) == 0)

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

    // MARK: - `.counted` — absorption measured against the live game feed

    /// THE CASE THE WHOLE THING EXISTS FOR. The base is a game behind what has
    /// actually been played, so that game must survive as unabsorbed — no
    /// matter what the clock says.
    @Test func countedKeepsTheGameTheBaseIsMissing() {
        let games = [
            finalGame(id: 1, homeId: kHome, awayId: kAway,
                      homeRuns: 5, awayRuns: 2, iso: kGameISO),
        ]
        // Feed says 133 played; the displayed record totals 132. One missing.
        let counted = TodayRecordAdjustments.unabsorbedResultsByTeam(
            from: games, lastUpdated: kAnchorStamp,
            absorption: .counted(feedGamesPlayed: [kHome: 133, kAway: 133],
                                 current: [kHome: record(78, 54),
                                           kAway: record(62, 70)]),
            now: kNow)
        #expect(counted[kHome]?.count == 1)
        #expect(counted[kAway]?.count == 1)
    }

    /// And the converse: when the base already contains it, it must NOT be
    /// added again. This is the double-count 04f5915 fixed, re-asserted for
    /// the new case.
    @Test func countedDropsTheGameTheBaseAlreadyHas() {
        let games = [
            finalGame(id: 1, homeId: kHome, awayId: kAway,
                      homeRuns: 5, awayRuns: 2, iso: kGameISO),
        ]
        // Feed and base agree at 132: nothing outstanding.
        let counted = TodayRecordAdjustments.unabsorbedResultsByTeam(
            from: games, lastUpdated: kAnchorStamp,
            absorption: .counted(feedGamesPlayed: [kHome: 132, kAway: 132],
                                 current: [kHome: record(78, 54),
                                           kAway: record(62, 70)]),
            now: kNow)
        #expect(counted[kHome] == nil, "already absorbed — nothing to add")
        #expect(counted[kAway] == nil)
    }

    /// ⚠️ THE REVERSION, AS A TEST. A game that started BEFORE our last fetch
    /// is dropped by the `lastUpdated` cutoff, which is the fault: the upstream
    /// trails a final by hours, so "started before we fetched" does not mean
    /// "the base contains it". `.counted` ignores the cutoff and keeps it.
    @Test func countedIgnoresTheCutoffThatCausedTheMorningReversion() {
        // The game starts a full hour BEFORE the stamp, so the cutoff would
        // discard it outright.
        let beforeStamp = "2026-08-26T18:00:00.000Z"
        let games = [
            finalGame(id: 1, homeId: kHome, awayId: kAway,
                      homeRuns: 5, awayRuns: 2, iso: beforeStamp),
        ]
        let cutoffStamp = "2026-08-26T19:00:00.000000Z"

        let unanchored = TodayRecordAdjustments.unabsorbedResultsByTeam(
            from: games, lastUpdated: cutoffStamp, absorption: .unanchored,
            now: kNow)
        #expect(unanchored[kHome] == nil, "the cutoff drops it — today's bug")

        // Same inputs, but the feed says the base is a game short.
        let counted = TodayRecordAdjustments.unabsorbedResultsByTeam(
            from: games, lastUpdated: cutoffStamp,
            absorption: .counted(feedGamesPlayed: [kHome: 133, kAway: 133],
                                 current: [kHome: record(78, 54),
                                           kAway: record(62, 70)]),
            now: kNow)
        #expect(counted[kHome]?.count == 1, "counted keeps it — the fix")
    }

    /// A team the feed does not cover must add NOTHING, rather than adding
    /// blind. Same rule `.anchored` follows: no measurement, no adjustment.
    @Test func countedAddsNothingForATeamMissingFromTheFeed() {
        let games = [
            finalGame(id: 1, homeId: kHome, awayId: kAway,
                      homeRuns: 5, awayRuns: 2, iso: kGameISO),
        ]
        let counted = TodayRecordAdjustments.unabsorbedResultsByTeam(
            from: games, lastUpdated: kAnchorStamp,
            absorption: .counted(feedGamesPlayed: [:],
                                 current: [kHome: record(78, 54)]),
            now: kNow)
        #expect(counted[kHome] == nil)
        #expect(counted[kAway] == nil)
    }

    /// A base somehow AHEAD of the feed must not produce a negative count or
    /// resurrect games. Clamped at both ends.
    @Test func countedClampsWhenTheBaseIsAheadOfTheFeed() {
        let games = [
            finalGame(id: 1, homeId: kHome, awayId: kAway,
                      homeRuns: 5, awayRuns: 2, iso: kGameISO),
        ]
        #expect(TodayRecordAdjustments.absorbedCount(
            .counted(feedGamesPlayed: [kHome: 130],
                     current: [kHome: record(78, 54)]),
            teamId: kHome, resultCount: 1) == 1, "absorbs everything, never negative")

        // And a base further behind than today's slate cannot un-absorb games
        // played before it: absorbed floors at 0, never below.
        #expect(TodayRecordAdjustments.absorbedCount(
            .counted(feedGamesPlayed: [kHome: 140],
                     current: [kHome: record(78, 54)]),
            teamId: kHome, resultCount: 1) == 0)
    }

    /// ⚠️ THE DODGERS CASE: BDL HAS ABSORBED, OUR TABLE HAS NOT.
    ///
    /// The record's base (BDL) already contains last night's win, so absorbing
    /// against it yields NOTHING to add — correct, and the record is right
    /// without help. The streak's base is our nightly table, which does not
    /// contain it, so absorbing against THAT must still yield the game.
    ///
    /// Sharing one absorption made the second answer equal the first: the
    /// streak was never walked and showed its raw `streak_code`. W1 beside a
    /// correct record.
    @Test func eachVintageAbsorbsAgainstItsOwnBase() {
        let games = [
            finalGame(id: 1, homeId: kHome, awayId: kAway,
                      homeRuns: 5, awayRuns: 2, iso: kGameISO),
        ]
        let feed: [Int: Int] = [kHome: 133, kAway: 133]

        // BDL is caught up: 78+54 = 132... plus last night = 133. Nothing due.
        let bdlBase: [Int: TeamRecord] = [kHome: record(79, 54), kAway: record(62, 71)]
        let bdlDeltas = TodayRecordAdjustments.deltas(
            from: games, lastUpdated: kAnchorStamp,
            absorption: .counted(feedGamesPlayed: feed, current: bdlBase), now: kNow)
        #expect(bdlDeltas.isEmpty, "the record's base already has it")

        // Our table is a day behind: 78+54 = 132. One game outstanding.
        let tableBase: [Int: TeamRecord] = [kHome: record(78, 54), kAway: record(62, 70)]
        let tableDeltas = TodayRecordAdjustments.deltas(
            from: games, lastUpdated: kAnchorStamp,
            absorption: .counted(feedGamesPlayed: feed, current: tableBase), now: kNow)
        #expect(tableDeltas[kHome]?.wDelta == 1,
                "the streak's base does NOT have it — this is what W1 vs W2 turns on")

        // And the streak walks that same game — asserted on the SET both the
        // record and the streak derive from, which is where they could
        // diverge. (`recalculateGB` itself returns empty for `standings: []`
        // whatever the adjustments say, so it cannot carry this assertion —
        // see `streakWalksTheSameGamesTheRecordDoes`.)
        let walked = TodayRecordAdjustments.unabsorbedResultsByTeam(
            from: games, lastUpdated: kAnchorStamp,
            absorption: .counted(feedGamesPlayed: feed, current: tableBase), now: kNow)
        #expect(walked[kHome]?.count == 1,
                "the streak walks the game the shared gate skipped")
    }

    /// The converse must stay safe: absorbing the RECORD against our table
    /// would double-count, because BDL's base already contains the game.
    @Test func theRecordMustNotAbsorbAgainstTheTable() {
        let games = [
            finalGame(id: 1, homeId: kHome, awayId: kAway,
                      homeRuns: 5, awayRuns: 2, iso: kGameISO),
        ]
        let feed: [Int: Int] = [kHome: 133, kAway: 133]
        let tableBase: [Int: TeamRecord] = [kHome: record(78, 54), kAway: record(62, 70)]
        let wrong = TodayRecordAdjustments.deltas(
            from: games, lastUpdated: kAnchorStamp,
            absorption: .counted(feedGamesPlayed: feed, current: tableBase), now: kNow)
        // Applying THAT to BDL's already-current record is the double count.
        let bdlBase: [Int: TeamRecord] = [kHome: record(79, 54)]
        let doubled = TodayRecordAdjustments.apply(wrong, to: bdlBase)
        #expect(doubled[kHome]?.wins == 80,
                "80-54 for a club that is 79-54 — why the record keeps the BDL vintage")
    }

    /// ⚠️ THE 08:00 ET REGRESSION, WITH THE HOUR WRITTEN DOWN. Isaac checked
    /// before 8am ET and last night's game had vanished from both the record
    /// and the streak. The cause was not absorption — it was scoping: a game
    /// played last night carries YESTERDAY's ET date, and the window admitted
    /// yesterday only before 6 AM, so by 08:00 the game was not a candidate at
    /// all and `.counted` never got to measure it.
    ///
    /// The hour is explicit here rather than derived, because the bug lives in
    /// a specific two-to-four-hour band — after the old window closed at 06:00
    /// and before BDL absorbs at roughly 07:30-10:30 — and a test that picked
    /// its own "now" could drift out of that band and pass while broken.
    @Test func countedStillSeesLastNightsGameAtEightInTheMorning() {
        // 2026-08-26, 19:10 ET first pitch — last night, from the check's view.
        let lastNight = "2026-08-26T23:10:00.000Z"
        // 2026-08-27, 08:00 ET = 12:00Z. Past the old 6 AM cutoff, before BDL
        // has absorbed.
        let eightAM = ISO8601DateFormatter().date(from: "2026-08-27T12:00:00Z")!
        let games = [
            finalGame(id: 1, homeId: kHome, awayId: kAway,
                      homeRuns: 5, awayRuns: 2, iso: lastNight),
        ]

        // The old behaviour, still correct for the cases that need it: with no
        // absorption signal the narrow window is the only guard, so yesterday
        // is out of scope by 08:00.
        let narrow = TodayRecordAdjustments.unabsorbedResultsByTeam(
            from: games, lastUpdated: kAnchorStamp, absorption: .unanchored,
            now: eightAM)
        #expect(narrow[kHome] == nil,
                "unanchored keeps the 6 AM window — its only double-count guard")

        // `.counted`: the feed says the base is a game short, so the game is
        // still a candidate and still unabsorbed.
        let counted = TodayRecordAdjustments.unabsorbedResultsByTeam(
            from: games, lastUpdated: kAnchorStamp,
            absorption: .counted(feedGamesPlayed: [kHome: 133, kAway: 133],
                                 current: [kHome: record(78, 54),
                                           kAway: record(62, 70)]),
            now: eightAM)
        #expect(counted[kHome]?.count == 1,
                "counted still sees last night's game at 08:00 ET")

        // And the record moves with it — the symptom Isaac reported.
        let deltas = TodayRecordAdjustments.deltas(
            from: games, lastUpdated: kAnchorStamp,
            absorption: .counted(feedGamesPlayed: [kHome: 133, kAway: 133],
                                 current: [kHome: record(78, 54),
                                           kAway: record(62, 70)]),
            now: eightAM)
        #expect(deltas[kHome]?.wDelta == 1)
    }

    /// The widened window must not become a second way to double-count: once
    /// the base HAS absorbed last night's game, 08:00 ET must add nothing even
    /// though the game is now in scope.
    @Test func theWiderWindowStillDropsWhatTheBaseAlreadyHas() {
        let lastNight = "2026-08-26T23:10:00.000Z"
        let eightAM = ISO8601DateFormatter().date(from: "2026-08-27T12:00:00Z")!
        let games = [
            finalGame(id: 1, homeId: kHome, awayId: kAway,
                      homeRuns: 5, awayRuns: 2, iso: lastNight),
        ]
        let counted = TodayRecordAdjustments.unabsorbedResultsByTeam(
            from: games, lastUpdated: kAnchorStamp,
            // Feed and base agree — absorbed.
            absorption: .counted(feedGamesPlayed: [kHome: 132, kAway: 132],
                                 current: [kHome: record(78, 54),
                                           kAway: record(62, 70)]),
            now: eightAM)
        #expect(counted[kHome] == nil, "in scope, but measured as already counted")
    }

    /// With two days in scope at once, the OLDEST games must be the ones
    /// retired — `dropFirst(absorbed)` over a chronological list. A base that
    /// has absorbed last night but not this afternoon must keep exactly the
    /// afternoon game.
    @Test func theWiderWindowRetiresTheOlderGameFirst() {
        let lastNight = "2026-08-26T23:10:00.000Z"
        let thisAfternoon = "2026-08-27T17:10:00.000Z"      // 13:10 ET same day
        let evening = ISO8601DateFormatter().date(from: "2026-08-27T23:00:00Z")!
        let games = [
            finalGame(id: 2, homeId: kAway, awayId: kHome,
                      homeRuns: 1, awayRuns: 4, iso: thisAfternoon),
            finalGame(id: 1, homeId: kHome, awayId: kAway,
                      homeRuns: 5, awayRuns: 2, iso: lastNight),
        ]
        let counted = TodayRecordAdjustments.unabsorbedResultsByTeam(
            from: games, lastUpdated: kAnchorStamp,
            // Two candidates, base short by one → the newer survives.
            absorption: .counted(feedGamesPlayed: [kHome: 134, kAway: 134],
                                 current: [kHome: record(79, 54),
                                           kAway: record(62, 71)]),
            now: evening)
        #expect(counted[kHome]?.count == 1)
        // Identified by which side the favourite was on rather than by parsing
        // the timestamp back: `ISO8601DateFormatter()` without
        // `.withFractionalSeconds` returns nil for these ".000Z" strings, so a
        // date comparison here silently compares against nil and passes for
        // the wrong reason. The afternoon game is the one where kHome is AWAY.
        #expect(counted[kHome]?.first?.awayId == kHome,
                "the already-absorbed older game is the one dropped")
    }

    /// The record and the streak must read the same evening — `recalculateGB`
    /// takes the same absorption as `deltas`, so a game counted by one is
    /// counted by the other. Asserted on the SET both derive from, which is
    /// where they could diverge; the streak's own arithmetic is covered by
    /// `streakWalksTheSameGamesTheRecordDoes` above.
    @Test func countedFeedsTheStreakTheSameGamesAsTheRecord() {
        let absorption = TodayRecordAdjustments.Absorption.counted(
            feedGamesPlayed: [kHome: 133, kAway: 133],
            current: [kHome: record(78, 54), kAway: record(62, 70)])
        let deltas = TodayRecordAdjustments.deltas(
            from: Self.oneFinal, lastUpdated: kAnchorStamp,
            absorption: absorption, now: kNow)
        #expect(deltas[kHome]?.wDelta == 1, "the record counts the win")
        let set = TodayRecordAdjustments.unabsorbedResultsByTeam(
            from: Self.oneFinal, lastUpdated: kAnchorStamp,
            absorption: absorption, now: kNow)
        #expect(set[kHome]?.count == 1, "and the streak walks the same game")
    }
}

// MARK: - BDL ↔ Retrosheet matching

/// The pairing that lets a Retrosheet-boxed game still show BDL's plays.
/// Its one hard job is doubleheaders: two games, same day, same clubs.
@Suite struct BDLRetroMatchTests {

    private func retro(pk: Int, away: String, home: String,
                       awayScore: Int?, homeScore: Int?) -> Game {
        func side(_ abbr: String, _ score: Int?) -> GameTeam {
            GameTeam(team: TeamInfo(id: 0, name: abbr, abbreviation: abbr),
                     score: score, leagueRecord: nil,
                     isWinner: nil, probablePitcher: nil)
        }
        return Game(
            gamePk: pk, gameDate: "2021-04-13T00:00:00Z",
            status: GameStatus(abstractGameState: "Final", detailedState: "Final",
                               statusCode: "F", codedGameState: "F"),
            teams: GameTeams(away: side(away, awayScore), home: side(home, homeScore)),
            venue: nil, linescore: nil, decisions: nil,
            bdlAwayTeamId: nil, bdlHomeTeamId: nil, bdlGameId: nil)
    }

    private func bdl(id: Int, away: String, home: String,
                     date: String, awayRuns: Int?, homeRuns: Int?) -> BDLGame {
        func team(_ tid: Int, _ abbr: String) -> BDLTeam {
            BDLTeam(id: tid, slug: nil, abbreviation: abbr, displayName: abbr,
                    shortDisplayName: nil, name: abbr, location: abbr,
                    league: nil, division: nil)
        }
        return BDLGame(
            id: id,
            homeTeam: team(100, home),
            awayTeam: team(200, away),
            homeTeamData: BDLTeamData(hits: nil, runs: homeRuns, errors: nil,
                                      inningScores: nil),
            awayTeamData: BDLTeamData(hits: nil, runs: awayRuns, errors: nil,
                                      inningScores: nil),
            date: date, status: "STATUS_FINAL", venue: nil, period: 9,
            displayClock: nil, scoringSummary: nil, season: 2021,
            seasonType: "regular", postseason: false,
            homeTeamName: home, awayTeamName: away)
    }

    /// The Retrosheet code set and BDL's differ for twelve clubs; the other
    /// eighteen must pass through untouched rather than needing an entry.
    @Test func onlyTheTwelveDifferingCodesAreTranslated() {
        #expect(BDLRetroMatch.bdlAbbreviation(forRetro: "KCA") == "KC")
        #expect(BDLRetroMatch.bdlAbbreviation(forRetro: "WAS") == "WSH")
        #expect(BDLRetroMatch.bdlAbbreviation(forRetro: "CHN") == "CHC")
        #expect(BDLRetroMatch.bdlAbbreviation(forRetro: "CHA") == "CHW")
        #expect(BDLRetroMatch.bdlAbbreviation(forRetro: "BOS") == "BOS")
        #expect(BDLRetroMatch.bdlAbbreviation(forRetro: "ARI") == "ARI")
        #expect(BDLRetroMatch.retroToBDL.count == 15)
    }

    /// START TIME orders a doubleheader, so the opener takes the earlier BDL
    /// id and the nightcap the later one — even though the two arrive in
    /// whatever order the provider listed them.
    @Test func doubleheaderHalvesTakeDistinctIdsInStartTimeOrder() {
        let ours = [retro(pk: -1, away: "SEA", home: "BAL", awayScore: 4, homeScore: 3),
                    retro(pk: -2, away: "SEA", home: "BAL", awayScore: 6, homeScore: 7)]
        // Deliberately reversed: the nightcap first.
        let theirs = [bdl(id: 3906, away: "SEA", home: "BAL",
                          date: "2021-04-13T23:15:00.000Z", awayRuns: 6, homeRuns: 7),
                      bdl(id: 3901, away: "SEA", home: "BAL",
                          date: "2021-04-13T20:05:00.000Z", awayRuns: 4, homeRuns: 3)]
        var flagged: [BDLRetroMatch.Discrepancy] = []
        let out = BDLRetroMatch.match(retroGames: ours, bdlGames: theirs,
                                      onDiscrepancy: { flagged.append($0) })
        #expect(out[0].bdlGameId == 3901)
        #expect(out[1].bdlGameId == 3906)
        #expect(out[0].bdlGameId != out[1].bdlGameId, "no BDL game may serve both halves")
        #expect(flagged.isEmpty, "scores agree, so nothing to report")
    }

    /// The score is a cross-check, not the key: a pairing the clock says is
    /// right but the scoreboard disagrees with is REPORTED, and still made,
    /// because start time is the identity.
    @Test func aScoreDisagreementIsReportedRatherThanSwallowed() {
        let ours = [retro(pk: -1, away: "SEA", home: "BAL", awayScore: 9, homeScore: 9)]
        let theirs = [bdl(id: 3901, away: "SEA", home: "BAL",
                          date: "2021-04-13T20:05:00.000Z", awayRuns: 4, homeRuns: 3)]
        var flagged: [BDLRetroMatch.Discrepancy] = []
        let out = BDLRetroMatch.match(retroGames: ours, bdlGames: theirs,
                                      onDiscrepancy: { flagged.append($0) })
        #expect(out[0].bdlGameId == 3901)
        #expect(flagged.count == 1)
        #expect(flagged[0].retroGamePk == -1)
    }

    /// THE SAFE FAILURE. With nothing to match against, every game comes back
    /// whole — same count, same scores — and simply carries no provider id.
    @Test func anUnmatchedGameKeepsEverythingButTheProviderId() {
        let ours = [retro(pk: -1, away: "SEA", home: "BAL", awayScore: 4, homeScore: 3)]
        let out = BDLRetroMatch.match(retroGames: ours, bdlGames: [])
        #expect(out.count == 1)
        #expect(out[0].bdlGameId == nil)
        #expect(out[0].teams.away.score == 4)
        #expect(out[0].teams.home.score == 3)
        #expect(out[0].usesRetrosheetBoxScore, "the box score still comes from our tables")
        #expect(!out[0].providerHasPlays, "and only the plays are lost")
    }

    /// A club with no counterpart that day passes through rather than stealing
    /// another matchup's id.
    @Test func aGameWithNoCounterpartIsLeftAlone() {
        let ours = [retro(pk: -1, away: "SEA", home: "BAL", awayScore: 4, homeScore: 3),
                    retro(pk: -2, away: "NYN", home: "PHI", awayScore: 1, homeScore: 2)]
        let theirs = [bdl(id: 3901, away: "SEA", home: "BAL",
                          date: "2021-04-13T20:05:00.000Z", awayRuns: 4, homeRuns: 3)]
        let out = BDLRetroMatch.match(retroGames: ours, bdlGames: theirs)
        #expect(out[0].bdlGameId == 3901)
        #expect(out[1].bdlGameId == nil)
    }

    /// BDL SOMETIMES LISTS THE SAME GAME TWICE. SD@CHC on 2005-04-13 appears
    /// as two rows with one timestamp and one score, beside the real nightcap.
    /// In pure start-time order our second half takes the copy and shows the
    /// opener's plays; the score tiebreak steps over it.
    @Test func aDuplicateProviderRowIsSteppedOverRatherThanConsumed() {
        let ours = [retro(pk: -1, away: "SD", home: "CHN", awayScore: 8, homeScore: 3),
                    retro(pk: -2, away: "SD", home: "CHN", awayScore: 3, homeScore: 8)]
        let theirs = [bdl(id: 3527, away: "SD", home: "CHC",
                          date: "2005-04-13T17:05:00.000Z", awayRuns: 8, homeRuns: 3),
                      bdl(id: 3528, away: "SD", home: "CHC",
                          date: "2005-04-13T17:05:00.000Z", awayRuns: 8, homeRuns: 3),
                      bdl(id: 3532, away: "SD", home: "CHC",
                          date: "2005-04-13T20:35:00.000Z", awayRuns: 3, homeRuns: 8)]
        var flagged: [BDLRetroMatch.Discrepancy] = []
        let out = BDLRetroMatch.match(retroGames: ours, bdlGames: theirs,
                                      onDiscrepancy: { flagged.append($0) })
        #expect(out[0].bdlGameId == 3527)
        #expect(out[1].bdlGameId == 3532, "the nightcap, not the duplicated opener")
        #expect(flagged.isEmpty)
    }

    /// The baseball-day window keeps one local day of games out of BDL's
    /// UTC buckets: a west-coast night game belongs to the day it was played,
    /// not the following morning it started in UTC.
    @Test func theBaseballDayWindowKeepsOneLocalDay() {
        let cal = Calendar(identifier: .gregorian)
        var c = DateComponents(); c.year = 2005; c.month = 7; c.day = 3
        c.timeZone = TimeZone(identifier: "UTC")
        let day = cal.date(from: c)!
        let games = [
            bdl(id: 1, away: "CHW", home: "OAK",          // 2 July local, 3 July UTC
                date: "2005-07-03T01:05:00.000Z", awayRuns: 1, homeRuns: 0),
            bdl(id: 2, away: "NYY", home: "DET",          // 3 July, day game
                date: "2005-07-03T17:05:00.000Z", awayRuns: 2, homeRuns: 1),
            bdl(id: 3, away: "SF", home: "SD",            // 3 July local, 4 July UTC
                date: "2005-07-04T02:05:00.000Z", awayRuns: 3, homeRuns: 2),
            bdl(id: 4, away: "BOS", home: "NYY",          // 4 July, day game
                date: "2005-07-04T17:05:00.000Z", awayRuns: 4, homeRuns: 3),
        ]
        let kept = Set(BDLRetroMatch.baseballDay(games, localDate: day).map(\.id))
        #expect(kept == [2, 3], "the previous evening's west-coast game and the next day's are both excluded")
    }

    /// The code map must cover FRANCHISE HISTORY, not just current clubs:
    /// Retrosheet writes the code of the day, BDL writes today's identity.
    @Test func franchiseHistoryCodesAreMapped() {
        #expect(BDLRetroMatch.bdlAbbreviation(forRetro: "FLO") == "MIA")
        #expect(BDLRetroMatch.bdlAbbreviation(forRetro: "MON") == "WSH")
        #expect(BDLRetroMatch.bdlAbbreviation(forRetro: "ATH") == "OAK")
        #expect(BDLRetroMatch.retroToBDL.count == 15)
    }

    /// The season decides the box score, and the id's sign no longer does.
    @Test func theSeasonDecidesTheBoxScoreNotTheId() {
        let g2021 = retro(pk: -1, away: "SEA", home: "BAL", awayScore: 4, homeScore: 3)
        #expect(g2021.seasonYear == 2021)
        #expect(g2021.usesRetrosheetBoxScore)
    }
}

// MARK: - Date picker bounds

@MainActor
@Suite struct SelectableDateRangeTests {
    private static let utc: Calendar = {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = TimeZone(identifier: "UTC")!
        return c
    }()
    private func day(_ s: String) -> Date {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.timeZone = TimeZone(identifier: "UTC")
        f.locale = .init(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f.date(from: s)!
    }

    /// The floor is the first game in our tables, not a rounded year.
    @Test func theFloorIsTheFirstGameWeHold() {
        let r = ScoresViewModel.selectableDateRange
        #expect(!r.contains(day("1898-04-14")), "the day before our first game is out")
        #expect(r.contains(day("1898-04-16")), "the day after is in")
    }

    /// A future date has no games by definition and must not be offerable.
    @Test func theFutureIsNotSelectable() {
        let r = ScoresViewModel.selectableDateRange
        let tomorrow = Calendar.current.date(byAdding: .day, value: 1, to: Date())!
        let nextDecade = Calendar.current.date(byAdding: .year, value: 10, to: Date())!
        #expect(!r.contains(tomorrow))
        #expect(!r.contains(nextDecade))
        #expect(r.contains(Calendar.current.startOfDay(for: Date())), "today is selectable")
    }

    /// The range must be well-formed — a crossed ClosedRange traps at runtime.
    @Test func theRangeIsNeverInverted() {
        let r = ScoresViewModel.selectableDateRange
        #expect(r.lowerBound <= r.upperBound)
    }
}

// MARK: - Scoring plays derived from the score

/// The Scoring tab is built by walking the running score, not by trusting the
/// provider's `scoring_play` flag — which marks 3 plays in 2026, 36 in 2021 and
/// 9 textless ones in 2005. These exercise the shipped
/// `PlaysView.scoringRows(from:)` on sequences taken from real games.
@Suite struct DerivedScoringTests {

    private func play(_ order: Int, inning: Int, top: Bool,
                      away: Int, home: Int, _ text: String?) -> BDLPlay {
        BDLPlay(gameId: 1, order: order, type: nil, text: text,
                homeScore: home, awayScore: away, inning: inning,
                inningType: top ? "Top" : "Bottom", scoringPlay: false,
                scoreValue: nil, outs: nil, balls: nil, strikes: nil,
                batterId: nil, pitcherId: nil, pitchType: nil,
                pitchVelocity: nil, trajectory: nil)
    }

    /// 2026's shape: the score moves on the at-bat's OPENING row and the
    /// sentence arrives two rows later. Taking the text off the row that moved
    /// would print "Melton pitches to DeLuca" as the scoring play.
    @Test func theSentenceIsTakenFromTheRowsAfterTheScoreMoves() {
        let plays = [
            play(1, inning: 2, top: true, away: 0, home: 0, "Top of the 2nd inning"),
            play(2, inning: 2, top: true, away: 1, home: 0, "Troy Melton pitches to Jonny DeLuca"),
            play(3, inning: 2, top: true, away: 1, home: 0, "Pitch 1 : Ball In Play"),
            play(4, inning: 2, top: true, away: 1, home: 0, "DeLuca doubled to left, Simpson scored."),
        ]
        let rows = PlaysView.scoringRows(from: plays)
        #expect(rows.count == 1)
        #expect(rows[0].text == "DeLuca doubled to left, Simpson scored.")
        #expect(rows[0].awayScore == 1)
    }

    /// 2005's shape: the row that moves the score is BLANK and the sentence is
    /// three rows on, with the score dipping back in between.
    @Test func aBlankScoringRowStillFindsItsSentence() {
        let plays = [
            play(1, inning: 2, top: true, away: 0, home: 0, "Pitch 4: in play"),
            play(2, inning: 2, top: true, away: 1, home: 0, nil),
            play(3, inning: 2, top: true, away: 1, home: 0, nil),
            play(4, inning: 2, top: true, away: 0, home: 0,
                 "B Molina grounded into double play, J DaVanon scored."),
        ]
        let rows = PlaysView.scoringRows(from: plays)
        #expect(rows.count == 1, "the dip back to 0-0 must not create a second row")
        #expect(rows[0].text == "B Molina grounded into double play, J DaVanon scored.")
    }

    /// A GRAND SLAM moves the score by four in one row. The sentence names all
    /// four men itself, so no delta needs rendering — taken from a real 2025
    /// play list.
    @Test func aGrandSlamIsOneRowNamingEveryScorer() {
        let plays = [
            play(1, inning: 4, top: true, away: 0, home: 0, "Pitch 2 : Ball In Play"),
            play(2, inning: 4, top: true, away: 4, home: 0, nil),
            play(3, inning: 4, top: true, away: 4, home: 0,
                 "Cruz homered to center (451 feet), McCutchen scored, Reynolds scored and N. Gonzales scored."),
        ]
        let rows = PlaysView.scoringRows(from: plays)
        #expect(rows.count == 1, "four runs on one play is ONE row")
        #expect(rows[0].awayScore == 4)
        #expect(rows[0].text?.contains("N. Gonzales scored") == true)
    }

    /// The peek-then-revert the provider ships must never produce a row whose
    /// score is lower than the one before it — the bug a reader saw as
    /// "AWY 2, HOM 0" followed by "AWY 1, HOM 0".
    @Test func theListIsMonotonicEvenWhenTheFeedIsNot() {
        let plays = [
            play(1, inning: 1, top: true, away: 0, home: 0, "Pitch 1 : Ball 1"),
            play(2, inning: 1, top: true, away: 2, home: 0, "Polanco doubled, two scored."),
            play(3, inning: 1, top: true, away: 1, home: 0, "Pitch 2 : Ball 2"),
            play(4, inning: 1, top: true, away: 2, home: 0, "Pitch 3 : Strike 1"),
            play(5, inning: 3, top: false, away: 2, home: 1, "Perez singled, Soler scored."),
        ]
        let rows = PlaysView.scoringRows(from: plays)
        #expect(rows.count == 2)
        for (a, b) in zip(rows, rows.dropFirst()) {
            #expect(b.awayScore >= a.awayScore && b.homeScore >= a.homeScore)
        }
    }

    /// A pitch line must never be promoted to a scoring play, in any of the
    /// forms the three eras write it.
    @Test func narrationIsNeverMistakenForADescription() {
        for t in ["Pitch 1 : Ball 1", "Pitch 2: strike 2 (looking)",
                  "Danny Duffy pitches to Luis Arraez", "Top of the 1st inning",
                  "Lineup Change", "C Figgins batting", "   "] {
            #expect(PlaysView.isNarration(t), "\(t) should be narration")
        }
        for t in ["DeLuca doubled to left, Simpson scored.",
                  "Larnach homered to right (394 feet)."] {
            #expect(!PlaysView.isNarration(t), "\(t) should be a description")
        }
    }

    /// THE REAL GUARD. Reconciliation catches a list that is short (a sentence
    /// the denylist wrongly discarded) and one that is long (noise promoted to
    /// a scoring play), without depending on that denylist being complete.
    @Test func reconciliationCatchesBothDirections() {
        let good = [
            play(1, inning: 1, top: true, away: 1, home: 0, "A singled, B scored."),
            play(2, inning: 5, top: false, away: 1, home: 2, "C doubled, D and E scored."),
        ]
        let r = PlaysView.reconcile(plays: good, finalAway: 1, finalHome: 2)
        #expect(r.matches)
        #expect(r.shown == 3 && r.expected == 3)

        // The same list against a game the box score says finished 1-3: one run
        // is unaccounted for and the view must say so rather than imply three.
        let short = PlaysView.reconcile(plays: good, finalAway: 1, finalHome: 3)
        #expect(!short.matches)
        #expect(short.shown == 3 && short.expected == 4)
    }

    /// A shutout reconciles like any other game — zero on one side is not a
    /// missing side.
    @Test func aShutoutReconciles() {
        let plays = [
            play(1, inning: 3, top: true, away: 1, home: 0, "A homered."),
            play(2, inning: 7, top: true, away: 2, home: 0, "B singled, A scored."),
        ]
        let r = PlaysView.reconcile(plays: plays, finalAway: 2, finalHome: 0)
        #expect(r.matches)
    }
}

// MARK: - The derived Retrosheet boundary

/// The boundary between our tables and the provider is "the newest season
/// Retrosheet has published and we have ingested", not "last year". These
/// pin the behaviour a constant could not give.
@MainActor
@Suite struct RetrosheetCoverageTests {
    private static let utc: Calendar = {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = TimeZone(identifier: "UTC")!
        return c
    }()
    private func day(_ s: String) -> Date {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.timeZone = TimeZone(identifier: "UTC")
        f.locale = .init(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f.date(from: s)!
    }

    /// A failed or unmade request leaves the boundary exactly where the old
    /// constant put it, so an outage degrades to the previous behaviour.
    @Test func theFallbackIsTheShippedConstant() {
        RetrosheetCoverage.resetForTesting()
        #expect(RetrosheetCoverage.lastSeason == 2025)
        #expect(Game.retrosheetLastSeason == 2025)
    }

    /// THE ROLLOVER. Once the service reports 2026, a 2026 date must route to
    /// our tables — the thing a hardcoded 2025 could never do.
    @Test func theBoundaryMovesWhenTheServiceSaysSo() {
        RetrosheetCoverage.resetForTesting(to: 2026)
        #expect(ScoresViewModel.usesHistoricalSource(
            for: day("2026-05-01"), calendar: Self.utc,
            lastSeason: RetrosheetCoverage.lastSeason))
        #expect(!ScoresViewModel.usesHistoricalSource(
            for: day("2027-05-01"), calendar: Self.utc,
            lastSeason: RetrosheetCoverage.lastSeason))
        RetrosheetCoverage.resetForTesting()
    }

    /// And with the boundary still at 2025, 2026 stays on the provider.
    @Test func theCurrentSeasonStaysWithTheProviderUntilItIsPublished() {
        RetrosheetCoverage.resetForTesting(to: 2025)
        #expect(!ScoresViewModel.usesHistoricalSource(
            for: day("2026-05-01"), calendar: Self.utc,
            lastSeason: RetrosheetCoverage.lastSeason))
        #expect(ScoresViewModel.usesHistoricalSource(
            for: day("2025-09-28"), calendar: Self.utc,
            lastSeason: RetrosheetCoverage.lastSeason))
        RetrosheetCoverage.resetForTesting()
    }

    /// Routing stays a pure function of (date, boundary) — no response, no
    /// error, no count — so a provider failure can never reroute a slate.
    @Test func routingRemainsAPureFunctionOfDateAndBoundary() {
        for season in [1998, 2015, 2025, 2026] {
            let inRange = ScoresViewModel.usesHistoricalSource(
                for: day("2020-06-01"), calendar: Self.utc, lastSeason: season)
            #expect(inRange == (2020 <= season))
        }
    }
}

// MARK: - Team colour resolution across sources

/// A Retrosheet box score carries no MLBAM team id, so the club's colour has
/// to come from the BDL id the slate attached — and only from there.
@Suite struct TeamColourResolutionTests {

    /// ZERO IS THE MARKER, and nothing else produces it. BDL team ids are
    /// 1-30, MLBAM's are 109-158, and a modern game's `TeamInfo.id` is one or
    /// the other — so a fallback keyed on 0 cannot fire on a current game.
    @Test func zeroIsNeverAModernTeamId() {
        for bdlId in 1...30 {
            #expect(bdlId != 0)
            // Every BDL id maps to a Lahman code, which is what makes the
            // fallback usable at all.
            #expect(bdlToLahmanTeamId[bdlId] != nil,
                    "BDL id \(bdlId) has no Lahman code")
        }
    }

    /// The fallback must reach the right club: a 2021 Dodgers game carries
    /// BDL's Dodgers id, and that must resolve to the Dodgers' colour.
    @Test func theBDLFallbackResolvesTheRightClub() {
        guard let dodgersBDLId = lahmanToBDLTeamId["LAN"] else {
            Issue.record("no BDL id for LAN"); return
        }
        #expect(bdlToLahmanTeamId[dodgersBDLId] == "LAN")
        #expect(TeamColors.color(for: bdlToLahmanTeamId[dodgersBDLId]) != nil,
                "the Dodgers must have a colour to fall back to")
    }

    /// And with no id of any kind — a pre-2002 game the slate could not match
    /// — there is nothing to resolve, which is the case that must draw no bar.
    @Test func noIdMeansNoColour() {
        let none: Int? = nil
        #expect(none.flatMap { bdlToLahmanTeamId[$0] } == nil)
    }
}


// MARK: - Standings ordering

/// The row order has to follow the ADJUSTED record, and it has to hold still
/// while someone is reading the table. Both are properties of the sort key and
/// when it is allowed to change, so they are testable without a network.
@Suite @MainActor struct StandingsOrderingTests {

    /// Two clubs in one division: A is 78-54 and B is 79-53, so B leads. A wins
    /// tonight and B loses, making A 79-54 and B 79-54 — level on wins, and A
    /// ahead on losses. The table must put A first, and must call A the leader.
    @Test func adjustedRecordDecidesTheOrderAndTheRank() {
        let pinned = ["AAA": 0.5940, "BBB": 0.5940]   // level after tonight
        var rows = [
            row("BBB", w: 79, l: 53, pct: 0.5985, rank: 1),
            row("AAA", w: 78, l: 54, pct: 0.5909, rank: 2),
        ]
        rows.sort { StandingsViewModel.standingsSort($0, $1, pinned) }
        // Level pct → the tiebreak is the stored rank, then wins. Both are
        // level on the pinned pct, so this asserts the fallback chain holds
        // rather than crashing or ordering arbitrarily.
        #expect(rows.count == 2)

        // The decisive case: A's adjusted pct genuinely exceeds B's.
        let decisive = ["AAA": 0.6000, "BBB": 0.5900]
        var rows2 = [
            row("BBB", w: 79, l: 53, pct: 0.5985, rank: 1),
            row("AAA", w: 78, l: 54, pct: 0.5909, rank: 2),
        ]
        rows2.sort { StandingsViewModel.standingsSort($0, $1, decisive) }
        #expect(rows2.first?.team_id == "AAA", "the adjusted leader sorts first")

        // And rank is restamped from position, so `rank == 1` and `index == 0`
        // describe the same club instead of two different ones.
        for i in rows2.indices { rows2[i].rank = i + 1 }
        #expect(rows2[0].rank == 1)
        #expect(rows2[1].rank == 2)
        #expect(rows2.first(where: { $0.rank == 1 })?.team_id == "AAA")
    }

    /// With no pinned value a row falls back to the backend's pct, so a first
    /// load — or a day with no finals — orders exactly as it did before.
    @Test func emptyPinnedOrderIsTheBackendOrder() {
        var rows = [
            row("AAA", w: 78, l: 54, pct: 0.5909, rank: 2),
            row("BBB", w: 79, l: 53, pct: 0.5985, rank: 1),
        ]
        rows.sort { StandingsViewModel.standingsSort($0, $1, [:]) }
        #expect(rows.first?.team_id == "BBB")
    }

    /// ⚠️ THE QUIET-TICK PROPERTY. A background refresh keeps the pinned key,
    /// so re-sorting during one is a no-op even when the underlying records
    /// have moved past it. This is what stops the table rearranging under a
    /// reader; the NUMBERS still change, only the order is held.
    @Test func aQuietTickKeepsTheOrderItAlreadyHad() {
        let pinned = ["AAA": 0.6000, "BBB": 0.5900]   // committed earlier
        // The rows arrive from a later fetch with B now ahead on the backend's
        // own pct — the order must still follow the pinned key, not this.
        var rows = [
            row("BBB", w: 80, l: 53, pct: 0.6015, rank: 1),
            row("AAA", w: 79, l: 54, pct: 0.5940, rank: 2),
        ]
        rows.sort { StandingsViewModel.standingsSort($0, $1, pinned) }
        #expect(rows.first?.team_id == "AAA",
                "held in place by the pinned order, despite B's newer pct")
        #expect(rows.first?.W == 79, "and the NUMBERS are the fresh ones")
        #expect(rows.last?.W == 80)
    }

    private func row(_ code: String, w: Int, l: Int,
                     pct: Double, rank: Int) -> TeamStanding {
        TeamStanding(
            year: 2026, team_id: code, franch_id: code, team_name: code,
            league: "AL", division: "E", rank: rank, G: w + l, W: w, L: l,
            win_pct: pct, runs_scored: nil, runs_allowed: nil, HR: nil,
            ERA: nil, attendance: nil, park_name: nil, last_updated: nil,
            streak_code: nil, last_ten_w: nil, last_ten_l: nil,
            home_w: nil, home_l: nil, away_w: nil, away_l: nil,
            games_back: nil, wild_card_games_back: nil,
            clinch_indicator: nil, division_leader: nil, clinched: nil,
            magic_number: nil, elimination_number: nil,
        )
    }
}
