//
//  GameLeadersTests.swift
//  BaseballStatsTests
//
//  `InningJoin` and `GameLeaders`.
//
//  ⚠️ WHY THESE ARE TESTED AT ALL — the failure mode is silent. A broken
//  join attaches one man's exit velocity to another man's at-bat, and
//  nothing on screen says so: every row still holds a plausible number
//  beside a plausible name. That is not a hypothetical. An index-based
//  join shipped in a harness, rendered 95.5 mph against "De La Cruz
//  stole second", put an exit velocity on an intentional walk, and
//  looked entirely fine until the rows were checked against the feed by
//  hand. No screenshot catches this; only an assertion does.
//
//  ⚠️ FIXTURES ARE REAL PAYLOAD (testdata/game-leaders.json), captured
//  2026-09-08 and regenerable with testdata/make-game-leaders-fixture.py.
//  Constructed data is what missed the interleaved steal and the
//  mid-inning substitution the first time — nobody invents a play stream
//  where a stolen base lands between a batter's second and third pitch,
//  or where a pinch-hit announcement arrives AFTER the next batter's
//  marker. The two games carry, between them, every shape found the hard
//  way:
//    • stealAndSubs (5059936) — a steal inside an at-bat, a pinch hitter
//      and two relief changes mid-inning, one pitcher holding 7 of 10
//    • battedAround (5059818) — four batters batting TWICE in the bottom
//      8th, which is the only reason the join needs a queue at all
//
//  Expectations were computed by the generator's own independent reading
//  of the same payload, so a passing test compares two implementations
//  rather than checking the app against itself.
//

import Foundation
import Testing
@testable import BaseballStats

// MARK: - fixture

private struct LeadersFixture: Decodable {
    let gameId: Int
    let plays: [BDLPlay]
    let pas: [BDLPlateAppearance]
    let identity: [String: Identity]
    let awayTeamId: Int?
    let homeTeamId: Int?
    let expected: Expected

    struct Identity: Decodable { let name: String; let teamId: Int? }

    struct Ranked: Decodable, Equatable { let playerId: Int; let value: Double }

    struct Expected: Decodable {
        let topHits: [Ranked]
        let topPitches: [Ranked]
        let mostRowsOnePlayer: Owner
        let distinctInTopPitches: Int
        let perSideHits: [String: [Ranked]]
        let perSidePitches: [String: [Ranked]]
        let sentences: [Sentence]
        let repeatBatters: [Repeat]

        struct Owner: Decodable { let playerId: Int?; let rows: Int }
        struct Repeat: Decodable { let inning: Int; let half: String; let batterId: Int; let times: Int }
        struct Sentence: Decodable {
            let inning: Int
            let half: String
            let batterId: Int?
            let paNumber: Int
            let occurrence: Int
            let sentence: String?
        }
    }

    /// Resolver in the shape `GameLeaders.build` expects.
    func nameAndTeam(_ pid: Int) -> (name: String, teamId: Int)? {
        guard let i = identity[String(pid)], let t = i.teamId else { return nil }
        return (i.name, t)
    }
}

/// Read from `testdata/` at the repo root, NOT from the test bundle —
/// same reasoning as `BoxScoreOrderTests`: the expectations belong to
/// neither language's tree. Resolved from `#filePath` so no
/// bundle-resource step is needed.
private func fixture(_ name: String) throws -> LeadersFixture {
    let url = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()   // BaseballStatsTests
        .deletingLastPathComponent()   // BaseballStats
        .deletingLastPathComponent()   // repo root
        .appendingPathComponent("testdata/game-leaders.json")
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    let all = try decoder.decode([String: LeadersFixture].self, from: Data(contentsOf: url))
    return try #require(all[name], "no fixture named \(name)")
}

// MARK: - InningJoin

@Suite("InningJoin")
struct InningJoinTests {

    /// The queue's whole reason for existing. Four batters bat twice in
    /// the bottom of the 8th of game 5059818; a join keyed on
    /// inning + half + batter WITHOUT a queue would hand both plate
    /// appearances the same sentence, and the second man's row would
    /// quietly describe the first man's at-bat.
    @Test func aBatterBattingTwiceInAHalfInningGetsBothOfHisOwnRows() throws {
        let fx = try fixture("battedAround")
        #expect(fx.expected.repeatBatters.count == 4, "fixture no longer carries the batted-around inning")

        let sentences = GameLeaders.sentences(plays: fx.plays, plateAppearances: fx.pas)

        for repeated in fx.expected.repeatBatters {
            let mine = fx.expected.sentences
                .filter { $0.inning == repeated.inning
                          && $0.half == repeated.half
                          && $0.batterId == repeated.batterId }
                .sorted { $0.occurrence < $1.occurrence }
            #expect(mine.count == repeated.times)

            let got = mine.compactMap { s -> String? in
                let pa = fx.pas.first { $0.inning == s.inning && $0.paNumber == s.paNumber }
                return pa.flatMap { sentences[GameLeaders.paKey($0)] }
            }
            #expect(got == mine.compactMap(\.sentence),
                    "batter \(repeated.batterId) took the wrong sentence for one of his \(repeated.times) trips")
            // and the two trips must not have collapsed onto one text
            if Set(mine.compactMap(\.sentence)).count > 1 {
                #expect(Set(got).count > 1, "both plate appearances took the SAME sentence")
            }
        }
    }

    /// The case that put 95.5 mph on a stolen base. A `Stolen Base` row
    /// and its `Play Result` twin land between a batter's pitches; both
    /// carry a null batter. If they are allowed into the queue they
    /// consume a slot and every later sentence lands one at-bat late.
    /// ⚠️ A steal reaches the stream in TWO shapes, and only one of them
    /// must be kept out of the join:
    ///
    ///   • as its own event — a `Stolen Base` row and a `Play Result`
    ///     twin carrying the SAME text, both with a null batter. These
    ///     land between a batter's pitches. Letting either into the
    ///     queue consumes an at-bat's slot and shifts every later
    ///     sentence one plate appearance late.
    ///   • as a clause inside a real at-bat's result —
    ///     "Tucker struck out swinging, T. Hernández stole second."
    ///     That row has a real batter id and IS Tucker's plate
    ///     appearance. It must be kept.
    ///
    /// Matching on the word "stole" cannot tell them apart, which is
    /// how this test was first written wrong: it swept up Tucker's row
    /// and asserted a legitimate at-bat had no batter. The batter id is
    /// the discriminator, not the text.
    @Test func anInterleavedStealDoesNotConsumeAnAtBatsSlot() throws {
        let fx = try fixture("stealAndSubs")

        let standalone = fx.plays.filter { $0.type == "Stolen Base" }
        #expect(!standalone.isEmpty, "fixture no longer carries a standalone steal")
        #expect(standalone.allSatisfy { $0.batterId == nil },
                "a standalone steal gained a batter id; the null-batter filter no longer excludes it")

        // its Play Result twin, same text, also null-battered
        for steal in standalone {
            let twin = fx.plays.first {
                $0.type == "Play Result" && $0.text == steal.text && $0.order != steal.order
            }
            #expect(twin != nil, "the Stolen Base row lost its Play Result twin")
            #expect(twin?.batterId == nil, "the steal's Play Result twin gained a batter id")
        }

        // the other shape: a steal named inside a real at-bat's result
        let insideAnAtBat = fx.plays.filter {
            $0.type == "Play Result" && ($0.text ?? "").contains("stole") && $0.batterId != nil
        }
        #expect(!insideAnAtBat.isEmpty,
                "fixture no longer carries a steal mentioned inside an at-bat's result")

        let sentences = GameLeaders.sentences(plays: fx.plays, plateAppearances: fx.pas)
        try assertEveryPlateAppearanceTookItsOwnSentence(fx, sentences)

        // and that at-bat kept its own sentence rather than being skipped
        for row in insideAnAtBat {
            #expect(sentences.values.contains(row.text ?? ""),
                    "an at-bat whose result mentions a steal was dropped from the join")
        }
    }

    /// The other drift case: a pinch-hit announcement and two relief
    /// changes arrive mid-inning, one of them AFTER the following
    /// batter's marker.
    @Test func midInningSubstitutionsDoNotShiftLaterSentences() throws {
        let fx = try fixture("stealAndSubs")
        let announcements = fx.plays.filter {
            $0.type == "Play Result" && $0.batterId == nil
            && (($0.text ?? "").contains(" hit for ") || ($0.text ?? "").contains(" relieved "))
        }
        #expect(announcements.count >= 3, "fixture no longer carries mid-inning substitutions")

        let sentences = GameLeaders.sentences(plays: fx.plays, plateAppearances: fx.pas)
        try assertEveryPlateAppearanceTookItsOwnSentence(fx, sentences)
    }

    /// An unmatched key must yield nothing rather than a neighbour's
    /// value — the failure that reads as fact.
    @Test func anUnmatchedKeyReturnsNothingRatherThanANeighbour() throws {
        let fx = try fixture("stealAndSubs")
        var join = InningJoin(
            fx.pas,
            key: { pa in pa.batterId.map { InningKey(inning: pa.inning, half: pa.halfInning, batterId: $0) } },
            order: { $0.inning * 1_000 + $0.paNumber },
        )
        // a batter who never appeared, in an inning that did
        #expect(join.next(InningKey(inning: 1, half: "top", batterId: -1)) == nil)
        // an inning that never happened, for a batter who did
        let real = try #require(fx.pas.first?.batterId)
        #expect(join.next(InningKey(inning: 99, half: "top", batterId: real)) == nil)
        // exhausting a real key yields nil rather than wrapping around
        let key = InningKey(inning: fx.pas[0].inning, half: fx.pas[0].halfInning, batterId: real)
        var drawn = 0
        while join.next(key) != nil { drawn += 1 }
        #expect(drawn > 0)
        #expect(join.next(key) == nil, "an exhausted key served a value")
    }

    /// The two feeds spell the half-inning differently — "Top"/"Bottom"
    /// from `/plays`, "top"/"bottom" from `/plate_appearances`, plus
    /// mid-inning marker variants. A key that does not normalise builds
    /// two buckets for one half-inning and matches nothing.
    @Test func halfInningSpellingIsNormalised() {
        let a = InningKey(inning: 7, half: "Top", batterId: 1)
        let b = InningKey(inning: 7, half: "top", batterId: 1)
        let c = InningKey(inning: 7, half: "Bottom", batterId: 1)
        let d = InningKey(inning: 7, half: "bottom", batterId: 1)
        #expect(a == b)
        #expect(c == d)
        #expect(a != c)
    }

    /// Every plate appearance that the independent reading matched must
    /// take exactly that sentence — not a neighbour's, not nothing.
    private func assertEveryPlateAppearanceTookItsOwnSentence(
        _ fx: LeadersFixture, _ sentences: [String: String],
    ) throws {
        var checked = 0
        for expected in fx.expected.sentences {
            guard let want = expected.sentence else { continue }
            let pa = try #require(fx.pas.first {
                $0.inning == expected.inning && $0.paNumber == expected.paNumber
            })
            let got = sentences[GameLeaders.paKey(pa)]
            #expect(got == want,
                    "inning \(expected.inning) \(expected.half) batter \(expected.batterId ?? -1): expected \(want.prefix(40)) got \(got?.prefix(40) ?? "nil")")
            checked += 1
        }
        #expect(checked > 50, "fixture thinned out; this assertion stopped covering the game")
    }
}

// MARK: - GameLeaders

@Suite("GameLeaders")
struct GameLeadersTests {

    /// Ranking PITCHES, not plate appearances. Taking each PA's fastest
    /// and ranking those caps a pitcher at one row per batter faced, so
    /// three overpowering pitches to the same hitter would count once —
    /// which is how this was originally written.
    @Test func rankingIsByPitchNotByPlateAppearance() throws {
        let fx = try fixture("stealAndSubs")
        let leaders = try #require(GameLeaders.build(
            plateAppearances: fx.pas, limit: 10, nameAndTeam: fx.nameAndTeam))

        #expect(leaders.fastestPitches.map(\.playerId) == fx.expected.topPitches.map(\.playerId))
        for (got, want) in zip(leaders.fastestPitches, fx.expected.topPitches) {
            #expect(abs(got.value - want.value) < 0.05)
        }

        // The load-bearing consequence: at least one pitcher holds more
        // rows than he faced batters in the top ten, which is only
        // possible if pitches are ranked individually.
        let top = leaders.fastestPitches
        let owner = try #require(fx.expected.mostRowsOnePlayer.playerId)
        let rowsHeld = top.filter { $0.playerId == owner }.count
        let distinctPAs = Set(top.filter { $0.playerId == owner }
                                 .map { "\($0.pa.inning)-\($0.pa.paNumber)" }).count
        #expect(rowsHeld == fx.expected.mostRowsOnePlayer.rows)
        #expect(rowsHeld > distinctPAs,
                "every one of \(owner)'s rows came from a different plate appearance, so per-PA ranking would give the same answer and this proves nothing")
    }

    /// Undeduped: one man may hold most of the board, and that
    /// concentration is the finding rather than something to hide.
    @Test func oneManMayHoldMostOfTheBoard() throws {
        for name in ["stealAndSubs", "battedAround"] {
            let fx = try fixture(name)
            let leaders = try #require(GameLeaders.build(
                plateAppearances: fx.pas, limit: 10, nameAndTeam: fx.nameAndTeam))
            let counts = Dictionary(grouping: leaders.fastestPitches, by: \.playerId)
                .mapValues(\.count)
            let most = counts.values.max() ?? 0
            #expect(most == fx.expected.mostRowsOnePlayer.rows,
                    "\(name): expected one pitcher to hold \(fx.expected.mostRowsOnePlayer.rows) rows")
            #expect(most > 1, "\(name): the board deduped by player again")
            #expect(Set(leaders.fastestPitches.map(\.playerId)).count
                    == fx.expected.distinctInTopPitches)
            // ids must survive the repeats, or SwiftUI collapses the rows
            #expect(Set(leaders.fastestPitches.map(\.id)).count == leaders.fastestPitches.count,
                    "\(name): two rows share an id")
        }
    }

    /// Each side is ranked again from the WHOLE game, not filtered out
    /// of the overall top ten. Filtering leaves a side that placed
    /// nowhere with an almost empty board.
    @Test func perSideBoardsAreRerankedNotFiltered() throws {
        let fx = try fixture("stealAndSubs")
        let away = try #require(fx.awayTeamId)
        let home = try #require(fx.homeTeamId)
        let split = GameLeaders.byTeam(
            plateAppearances: fx.pas, awayTeamId: away, homeTeamId: home,
            limit: 3, nameAndTeam: fx.nameAndTeam)

        let gotAway = try #require(split.away).fastestPitches
        let gotHome = try #require(split.home).fastestPitches
        #expect(gotAway.map(\.playerId) == fx.expected.perSidePitches["away"]?.map(\.playerId))
        #expect(gotHome.map(\.playerId) == fx.expected.perSidePitches["home"]?.map(\.playerId))
        #expect(gotAway.count == 3)
        #expect(gotHome.count == 3)

        // The proof it is not a filter: the overall top three cannot
        // supply three rows for BOTH sides at once.
        let overall = try #require(GameLeaders.build(
            plateAppearances: fx.pas, limit: 3, nameAndTeam: fx.nameAndTeam)).fastestPitches
        let overallIds = Set(overall.map(\.playerId))
        #expect(!Set(gotAway.map(\.playerId)).isSubset(of: overallIds)
                || !Set(gotHome.map(\.playerId)).isSubset(of: overallIds),
                "both per-side boards fit inside the overall top three, so this fixture can no longer tell re-ranking from filtering")
    }

    /// ⚠️ Release speed, not plate speed. BDL ships both, and the same
    /// pitch reads ~8 mph slower at the plate — a board built on the
    /// wrong field looks entirely plausible and compares with nothing.
    @Test func speedIsMeasuredAtReleaseNotAtThePlate() throws {
        let fx = try fixture("stealAndSubs")
        let leaders = try #require(GameLeaders.build(
            plateAppearances: fx.pas, limit: 10, nameAndTeam: fx.nameAndTeam))
        let top = try #require(leaders.fastestPitches.first)

        // find the pitch the top row came from, in the raw payload
        let pa = try #require(fx.pas.first {
            $0.inning == top.pa.inning && $0.paNumber == top.pa.paNumber
        })
        let pitch = try #require((pa.pitches ?? [])[safe: top.pitchIndex])
        let release = try #require(pitch.releaseSpeed)
        #expect(abs(top.value - release) < 0.05, "the board is not using releaseSpeed")

        // and the two fields really are far enough apart for this to matter
        let plate = fx.rawPlateSpeed(inning: pa.inning, paNumber: pa.paNumber, index: top.pitchIndex)
        if let plate {
            #expect(abs(release - plate) > 2.0,
                    "release and plate speed have converged in this fixture, so the assertion above no longer distinguishes them")
            #expect(abs(top.value - plate) > 2.0, "the board is using plateSpeed")
        }
    }

    /// A hardest-hit row must carry what the ball became; the board is
    /// built on the ball in play, not the last pitch of the PA.
    @Test func hardestHitRowsCarryTheOutcome() throws {
        let fx = try fixture("stealAndSubs")
        let leaders = try #require(GameLeaders.build(
            plateAppearances: fx.pas, limit: 10, nameAndTeam: fx.nameAndTeam))
        #expect(leaders.hardestHit.map(\.playerId) == fx.expected.topHits.map(\.playerId))
        #expect(leaders.hardestHit.allSatisfy { $0.kind == .hit })
        #expect(leaders.fastestPitches.allSatisfy { $0.kind == .pitch })
        #expect(leaders.hardestHit.allSatisfy { $0.detail?.isEmpty == false },
                "a hardest-hit row lost its outcome")
    }

    /// A player the resolver cannot name is dropped, not rendered blank.
    @Test func anUnresolvableIdIsDroppedRatherThanShownBlank() throws {
        let fx = try fixture("stealAndSubs")
        let leaders = GameLeaders.build(plateAppearances: fx.pas, limit: 10) { _ in nil }
        #expect(leaders == nil, "with no name for anyone the board should be absent entirely")
    }

    /// Pre-2015 games ship no plate appearances at all — not merely
    /// absent Statcast fields, an empty feed. The card must be absent.
    @Test func aGameWithNoPlateAppearancesHasNoBoard() throws {
        let fx = try fixture("stealAndSubs")
        #expect(GameLeaders.build(plateAppearances: [], nameAndTeam: fx.nameAndTeam) == nil)
    }
}

// MARK: - helpers

private extension Array {
    subscript(safe i: Int) -> Element? { indices.contains(i) ? self[i] : nil }
}

private extension LeadersFixture {
    /// `plateSpeed` is captured in the fixture but deliberately NOT
    /// decoded by `BDLPitchDetail` — the model carries only the field
    /// the app should use. Read it straight from the JSON so the test
    /// can prove the two are far apart.
    func rawPlateSpeed(inning: Int, paNumber: Int, index: Int) -> Double? {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("testdata/game-leaders.json")
        guard let data = try? Data(contentsOf: url),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }
        for (_, game) in root {
            guard let g = game as? [String: Any], g["gameId"] as? Int == gameId,
                  let pas = g["pas"] as? [[String: Any]] else { continue }
            for pa in pas where pa["inning"] as? Int == inning && pa["pa_number"] as? Int == paNumber {
                let pitches = pa["pitches"] as? [[String: Any]] ?? []
                guard index < pitches.count else { return nil }
                return pitches[index]["plate_speed"] as? Double
            }
        }
        return nil
    }
}
