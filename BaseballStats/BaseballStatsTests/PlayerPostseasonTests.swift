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
//  (World Series only, both sides), Ernie Banks (no postseason), Betts (on a
//  bye: history, not appeared) and Machado (appeared in the Wild Card).
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

    // MARK: the Overview boxes — one stat set for "<season> Postseason" and "Postseason Career"

    @Test func bothBattingBoxesShowTheSameStatsWithoutWAR() throws {
        let bat = try #require(try record("oneill").batting)
        let career = postseasonBattingStatItems(bat.career).map(\.label)
        let season = postseasonBattingStatItems(try #require(bat.seasons.first).totals).map(\.label)
        // The season box's list with H in WAR's place.
        #expect(career == ["H", "AVG", "OBP", "SLG", "OPS", "HR", "RBI", "SB", "G", "PA"])
        #expect(season == career)
        #expect(!career.contains("WAR"))
    }

    @Test func theBattingCareerBoxReadsTheCareerLine() throws {
        let items = postseasonBattingStatItems(try #require(try record("oneill").batting).career)
        let v = Dictionary(uniqueKeysWithValues: items.map { ($0.label, $0.value) })
        #expect(v["AVG"] == ".284" && v["H"] == "85" && v["G"] == "85")
    }

    @Test func bothPitchingBoxesShowTheSameStatsWithHInWARsPlace() throws {
        let pit = try #require(try record("sabathia").pitching)
        let starter = postseasonPitcherIsStarter(pit.career)
        let career = postseasonPitchingStatItems(pit.career, starter: starter)
        let season = postseasonPitchingStatItems(try #require(pit.seasons.first).totals, starter: starter)
        // The season box's list — WAR, W-L, ERA, WHIP, K/9, G, GS|SV, IP, SO, BB — with H for WAR.
        #expect(career.map(\.label) == ["H", "W-L", "ERA", "WHIP", "K/9", "G", "GS", "IP", "SO", "BB"])
        #expect(season.map(\.label) == career.map(\.label))
        let v = Dictionary(uniqueKeysWithValues: career.map { ($0.label, $0.value) })
        #expect(v["W-L"] == "10-7" && v["IP"] == "130.1" && v["SO"] == "121")
        #expect(v["K/9"] == String(format: "%.1f", 121.0 * 27 / 391))
    }

    @Test func aRelieverGetsSVInTheRoleSlot() throws {
        // Hendrickson: one relief appearance, no start.
        let pit = try #require(try record("hendrickson").pitching)
        #expect(!postseasonPitcherIsStarter(pit.career))
        #expect(postseasonPitchingStatItems(pit.career, starter: false).map(\.label)[6] == "SV")
    }

    // MARK: which Overview boxes show during the postseason

    @Test func aPlayerOnAByeGetsTheCareerBoxOnly() throws {
        // Betts, captured 2026-09-30: the league postseason is on, the Dodgers
        // have a bye, he has nine postseasons of history.
        let r = try record("betts")
        #expect(r.current.leagueInProgress == true && r.current.playerAppeared == false)
        #expect(showsPostseasonCareerBox(r, side: r.batting) != nil)
        #expect(showsPostseasonOverviewLine(r, side: r.batting) == nil)
    }

    @Test func aPlayerWhoHasAppearedGetsBoth() throws {
        let r = try record("machado")                       // captured 2026-09-30, after WC G1
        #expect(showsPostseasonCareerBox(r, side: r.batting) != nil)
        #expect(showsPostseasonOverviewLine(r, side: r.batting)?.season == 2026)
    }

    @Test func aPlayerWithNoPostseasonGetsNeither() throws {
        let r = try record("banks")
        let during = PlayerPostseason(playerId: r.playerId, retroLast: r.retroLast, batting: nil, pitching: nil,
                                      current: PostseasonCurrent(season: 2026, leagueInProgress: true,
                                                                 playerAppeared: false, teamEliminated: nil))
        #expect(showsPostseasonCareerBox(during, side: during.batting) == nil)
        #expect(showsPostseasonOverviewLine(during, side: during.batting) == nil)
    }

    @Test func theCareerBoxEndsWithTheWorldSeries() throws {
        let r = try record("betts")
        let over = PlayerPostseason(playerId: r.playerId, retroLast: r.retroLast, batting: r.batting, pitching: nil,
                                    current: PostseasonCurrent(season: 2026, leagueInProgress: false,
                                                               playerAppeared: false, teamEliminated: nil))
        #expect(showsPostseasonCareerBox(over, side: over.batting) == nil)
    }

    // MARK: the live line

    @Test func theLiveFlagNamesTheSide() throws {
        let json = #"{"player_id": 1, "retro_last": 2025, "batting": null, "pitching": null,"# +
            #""live": [{"game_id": "15467364", "sides": ["bat"]}],"# +
            #""current": {"season": 2026, "league_in_progress": true, "player_appeared": true, "team_eliminated": false}}"#
        let r = try JSONDecoder().decode(PlayerPostseason.self, from: Data(json.utf8))
        #expect(r.isLive(batting: true) && !r.isLive(batting: false))
        #expect(r.live?.first?.gameId == "15467364")
    }

    @Test func productionsPayloadBeforeTheOverlayDecodesWithNoLiveTag() throws {
        // Schwarber, captured from production 2026-09-30 while PHI @ ATL was in
        // progress — before the backend's live overlay: no `live` field at all.
        let r = try record("schwarber")
        #expect(r.live == nil)
        #expect(!r.isLive(batting: true) && !r.isLive(batting: false))
        #expect(r.batting?.seasons.first?.season == 2026)
    }

    @Test func aPayloadWithoutTheLiveFieldStillDecodes() throws {
        let r = try record("machado")                       // captured before the overlay shipped
        #expect(r.live == nil && !r.isLive(batting: true))
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
