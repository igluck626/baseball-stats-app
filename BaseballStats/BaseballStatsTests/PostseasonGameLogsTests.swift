//
//  PostseasonGameLogsTests.swift
//
//  The profile's postseason game log: decoding
//  `/players/{id}/postseason/gamelogs?season=`, the "ALDS G3" and "W 8-5"
//  labels, rows newest first with rates to date, and a pitcher's decision
//  reaching the regular log's DEC column.
//
//  `testdata/player-postseason-gamelogs/*.json` is the backend's OWN output
//  (captured 2026-09-30): O'Neill 2001 (batting, sat out ALDS G3 and WS G2 +
//  G6), Sabathia 2009 (both sides), Hendrickson 2026 (a balldontlie season)
//  and Banks (no postseason).
//

import Foundation
import Testing
@testable import BaseballStats

@MainActor
private func logs(_ name: String) throws -> PostseasonGameLogs {
    let url = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()   // BaseballStatsTests
        .deletingLastPathComponent()   // BaseballStats
        .deletingLastPathComponent()   // repo root
        .appendingPathComponent("testdata/player-postseason-gamelogs/\(name).json")
    return try JSONDecoder().decode(PostseasonGameLogs.self, from: Data(contentsOf: url))
}

@MainActor
@Suite("Postseason game logs")
struct PostseasonGameLogsTests {

    @Test func aBatterDecodesOldestFirstWithTeamGameNumbers() throws {
        let l = try logs("oneill-2001")
        let bat = try #require(l.batting)
        #expect(l.pitching == nil && l.source == "retrosheet" && bat.count == 13)
        #expect(bat.first?.date == "2001-10-10")
        // He sat out ALDS Game 3: his third game is G4, his team's fourth.
        #expect(bat.prefix(3).map(\.seriesGameLabel) == ["ALDS G1", "ALDS G2", "ALDS G4"])
        #expect(bat.last?.seriesGameLabel == "WS G7")
        #expect(bat.last?.resultLabel == "L 2-3")
    }

    @Test func aPitcherHasBothSides() throws {
        let l = try logs("sabathia-2009")
        let pit = try #require(l.pitching)
        #expect(l.batting?.count == 5 && pit.count == 5)
        let ws4 = try #require(pit.last)
        // The team won Game 4; he had no decision.
        #expect(ws4.seriesGameLabel == "WS G4" && ws4.resultLabel == "W 7-4" && ws4.decision == "ND")
        #expect(ws4.IP == "6.2" && ws4.outs == 20)
    }

    @Test func theCurrentSeasonComesFromBalldontlie() throws {
        let l = try logs("hendrickson-2026")
        let g = try #require(l.pitching?.first)
        #expect(l.source == "bdl" && l.batting == nil)
        #expect(g.seriesGameLabel == "WC G1" && g.roundName == "AL Wild Card")
    }

    @Test func noPostseasonMeansNoSides() throws {
        let l = try logs("banks-none")
        #expect(l.batting == nil && l.pitching == nil)
    }

    // MARK: labels

    @Test func seriesGameLabels() {
        #expect(postseasonSeriesGameLabel(round: "DS", roundName: "ALDS", gameNumber: 3) == "ALDS G3")
        #expect(postseasonSeriesGameLabel(round: "CS", roundName: "NLCS", gameNumber: 7) == "NLCS G7")
        #expect(postseasonSeriesGameLabel(round: "WS", roundName: "World Series", gameNumber: 1) == "WS G1")
        #expect(postseasonSeriesGameLabel(round: "WC", roundName: "AL Wild Card", gameNumber: 2) == "WC G2")
        #expect(postseasonSeriesGameLabel(round: "DS", roundName: "ALDS", gameNumber: nil) == "ALDS")
    }

    @Test func resultLabels() {
        #expect(postseasonResultLabel(result: "W", teamScore: 8, oppScore: 5) == "W 8-5")
        #expect(postseasonResultLabel(result: "L", teamScore: 1, oppScore: 6) == "L 1-6")
        #expect(postseasonResultLabel(result: nil, teamScore: 1, oppScore: 6) == "—")
    }

    // MARK: rows

    @Test func battingRowsAreNewestFirstWithRatesToDate() throws {
        let bat = try #require(try logs("oneill-2001").batting)
        let rows = PostseasonGameRows.batting(bat)
        #expect(rows.first?.line.date == "2001-11-04" && rows.last?.line.date == "2001-10-10")
        // The newest row's rate is the whole postseason's: H / AB over all 13 games.
        let h = bat.map(\.H).reduce(0, +), ab = bat.map(\.AB).reduce(0, +)
        #expect(abs((rows.first?.avg ?? 0) - Double(h) / Double(ab)) < 1e-9)
        // The oldest row's is that one game's: 0-for-4.
        #expect(rows.last?.avg == 0)
    }

    @Test func pitchingRowsCarryEraToDateAndHisDecision() throws {
        let pit = try #require(try logs("sabathia-2009").pitching)
        let rows = PostseasonGameRows.pitching(pit)
        let outs = pit.map(\.outs).reduce(0, +), er = pit.map(\.ER).reduce(0, +)
        #expect(abs((rows.first?.era ?? 0) - Double(er) * 27 / Double(outs)) < 1e-9)
        // The regular log's DEC column reads `result`: it must be HIS decision.
        let g = try #require(rows.first).line.asGameLog
        #expect(g.result == "ND" && g.team_score == 7)
        #expect(abs((g.IP ?? 0) - 20.0 / 3) < 1e-9)
    }
}
