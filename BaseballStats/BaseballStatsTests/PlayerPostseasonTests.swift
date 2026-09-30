//
//  PlayerPostseasonTests.swift
//
//  The profile's postseason record: decoding `/players/{id}/postseason`, the
//  switch appearing only for a side that has a postseason, round labels by
//  era, series results in progress and complete, the mapping into the career
//  tables' row types, and the Overview line's rule.
//
//  `testdata/player-postseason/*.json` is the backend's OWN output (captured
//  2026-09-30): O'Neill (batting only), Sabathia (a pitcher who also batted),
//  Ohtani (two-way), Hendrickson (a 2026 Wild Card series in progress), Ruth
//  (World Series only, both sides) and Ernie Banks (no postseason).
//

import Foundation
import Testing
@testable import BaseballStats

@MainActor
private func record(_ name: String) throws -> PlayerPostseason {
    let url = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()   // BaseballStatsTests
        .deletingLastPathComponent()   // BaseballStats
        .deletingLastPathComponent()   // repo root
        .appendingPathComponent("testdata/player-postseason/\(name).json")
    return try JSONDecoder().decode(PlayerPostseason.self, from: Data(contentsOf: url))
}

@MainActor
@Suite("Player postseason")
struct PlayerPostseasonTests {

    // MARK: sides — the Career switch shows only for a side that has one

    @Test func aBatterHasOnlyABattingSide() throws {
        let r = try record("oneill")
        let bat = try #require(r.batting)
        #expect(r.pitching == nil)
        #expect(bat.career.H == 85 && bat.career.G == 85 && bat.career.AVG == 0.284)
        #expect(bat.seasons.first?.season == 2001)            // newest first
        #expect(bat.seasons.count == 8)
    }

    @Test func noPostseasonMeansNoSides() throws {
        let r = try record("banks")
        #expect(r.batting == nil && r.pitching == nil)
    }

    @Test func aPitcherWhoBattedHasBothSides() throws {
        let r = try record("sabathia")
        let pit = try #require(r.pitching)
        #expect(r.batting != nil)       // pre-2022 pitchers batted; the role toggle picks the side
        #expect(pit.career.SO == 121 && pit.career.W == 10 && pit.career.L == 7)
        #expect(pit.career.IP == "130.1" && pit.career.outs == 391)
    }

    @Test func aTwoWayPlayerHasBoth() throws {
        let r = try record("ohtani")
        #expect(r.batting?.career.HR == 11)
        #expect(r.pitching?.career.SO == 28)
    }

    // MARK: rounds

    @Test func theDeadBallEraIsWorldSeriesOnly() throws {
        let rounds = try #require(record("ruth").batting).seasons.flatMap(\.rounds)
        #expect(!rounds.isEmpty)
        #expect(rounds.allSatisfy { $0.round == "WS" && $0.roundName == "World Series" })
    }

    @Test func aModernSeasonRunsWildCardToWorldSeries() throws {
        let s = try #require(try record("ohtani").batting?.seasons.first)
        #expect(s.season == 2025)
        #expect(s.rounds.map(\.roundName) == ["NL Wild Card", "NLDS", "NLCS", "World Series"])
        #expect(s.rounds.last?.series.summary == "Won 4-3")
    }

    @Test func aSeriesInProgressHasNoResultYet() throws {
        let s = try #require(try record("hendrickson").pitching?.seasons.first)
        #expect(s.season == 2026 && s.source == "bdl")
        let series = try #require(s.rounds.first?.series)
        #expect(series.won == nil)
        #expect(series.summary == "Trails 0-1")
    }

    @Test func seriesSummaries() {
        #expect(PostseasonRoundSeries(wins: 4, losses: 3, won: true).summary == "Won 4-3")
        #expect(PostseasonRoundSeries(wins: 1, losses: 2, won: false).summary == "Lost 1-2")
        #expect(PostseasonRoundSeries(wins: 1, losses: 0, won: nil).summary == "Leads 1-0")
        #expect(PostseasonRoundSeries(wins: 1, losses: 1, won: nil).summary == "Tied 1-1")
    }

    // MARK: into the career tables' row types

    @Test func pitchingInningsBecomeDecimalFromOuts() throws {
        let pit = try #require(try record("sabathia").pitching)
        let row = pit.career.careerSeason(year: nil, team: nil)
        #expect(abs((row.IP ?? 0) - 391.0 / 3) < 1e-9)       // 130.1 IP = 130⅓
        #expect(row.ERA == pit.career.ERA && row.WAR == nil)
    }

    @Test func battingTotalBasesAreDerived() throws {
        let t = try #require(try record("ruth").batting).career
        let row = t.careerSeason(year: nil, team: nil)
        #expect(row.TB == t.H + t.doubles + 2 * t.triples + 3 * t.HR)
        #expect(row.WAR == nil && row.OPS_plus == nil)       // season-only figures stay empty
    }

    // MARK: the Overview line (until the World Series ends)

    @Test func theOverviewLineShowsOnceHeHasAppeared() throws {
        let r = try record("hendrickson")
        let line = showsPostseasonOverviewLine(r, side: r.pitching)
        #expect(line?.season == 2026)
        #expect(showsPostseasonOverviewLine(r, side: r.batting) == nil)   // no batting side
    }

    @Test func notBeforeHeAppears() throws {
        let r = try record("oneill")            // league in progress, he isn't in it
        #expect(showsPostseasonOverviewLine(r, side: r.batting) == nil)
    }

    @Test func itStaysAfterHisTeamIsOutUntilTheWorldSeriesEnds() throws {
        let r = try record("hendrickson")
        let out = PlayerPostseason(playerId: r.playerId, retroLast: r.retroLast, batting: nil,
                                   pitching: r.pitching,
                                   current: PostseasonCurrent(season: 2026, leagueInProgress: true,
                                                              playerAppeared: true, teamEliminated: true))
        #expect(showsPostseasonOverviewLine(out, side: out.pitching) != nil)
        let over = PlayerPostseason(playerId: r.playerId, retroLast: r.retroLast, batting: nil,
                                    pitching: r.pitching,
                                    current: PostseasonCurrent(season: 2026, leagueInProgress: false,
                                                               playerAppeared: true, teamEliminated: true))
        #expect(showsPostseasonOverviewLine(over, side: over.pitching) == nil)
    }

    @Test func anUnknownLeagueStateHidesIt() throws {
        let r = try record("hendrickson")
        let unknown = PlayerPostseason(playerId: r.playerId, retroLast: r.retroLast, batting: nil,
                                       pitching: r.pitching,
                                       current: PostseasonCurrent(season: 2026, leagueInProgress: nil,
                                                                  playerAppeared: true, teamEliminated: nil))
        #expect(showsPostseasonOverviewLine(unknown, side: unknown.pitching) == nil)
    }
}
