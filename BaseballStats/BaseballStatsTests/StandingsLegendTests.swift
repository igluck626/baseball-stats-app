//
//  StandingsLegendTests.swift
//
//  The line under a standings table that says what its symbols mean (Isaac,
//  2026-10-27), and the letters behind it. Real response:
//  testdata/standings/standings-2026-10-22.json, the 2026 final standings —
//  "*" Tampa Bay and Milwaukee (division winners with the league's best
//  record), "x" the other four division winners, "y" the six wild cards, "e"
//  the other eighteen.
//

import Foundation
import Testing
@testable import BaseballStats

@MainActor
private func standings() throws -> [TeamStanding] {
    let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().appendingPathComponent("testdata/standings/standings-2026-10-22.json")
    let r = try JSONDecoder().decode(StandingsResponse.self, from: Data(contentsOf: url))
    return try #require(r.standings)
}

@MainActor
struct StandingsLegendTests {

    @Test func theLettersMeanWhatThe2026StandingsShow() throws {
        let all = try standings()
        let by = Dictionary(grouping: all) { ClinchMark($0.clinch_indicator) }
        #expect(Set(by[.bestRecord, default: []].compactMap(\.team_name)) == ["Tampa Bay Rays", "Milwaukee Brewers"])
        #expect(by[.bestRecord, default: []].allSatisfy { $0.division_leader == true })
        #expect(by[.division, default: []].count == 4 && by[.division, default: []].allSatisfy { $0.division_leader == true })
        #expect(by[.wildCard, default: []].count == 6 && by[.wildCard, default: []].allSatisfy { $0.division_leader != true })
        #expect(by[.eliminated, default: []].count == 18)
        #expect(by[nil] == nil, "every 2026 team has a letter the app knows")
    }

    @Test func theLegendListsOnlyTheLettersATableShows() throws {
        let al = try standings().filter { $0.league == "AL" }
        #expect(StandingsLegend.marks(al) == [.bestRecord, .division, .wildCard, .eliminated])
        let wildCards = al.filter { $0.division_leader != true }
        #expect(StandingsLegend.marks(wildCards) == [.wildCard, .eliminated], "no division winner in a wild-card table")
        #expect(StandingsLegend.marks([]) == [])
    }

    @Test func theWordsAreThePlainOnes() {
        #expect(ClinchMark.allCases.map(\.meaning) == ["best record", "division", "wild card", "eliminated"])
    }

    @Test func anUnknownLetterHasNoMeaning() {
        #expect(ClinchMark("z") == nil && ClinchMark("w") == nil && ClinchMark(nil) == nil && ClinchMark("") == nil)
        #expect(ClinchMark("X") == .division, "case doesn't matter")
    }

    @Test func magicNumberJoinsOnlyWhileALeaderShowsOne() throws {
        let all = try standings()
        #expect(!StandingsLegend.showsMagicNumber(all, divisionTables: true), "every 2026 leader has clinched")
        // The same rows mid-season: a leader still counting down.
        let json = try JSONSerialization.jsonObject(with: Data(contentsOf: URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("testdata/standings/standings-2026-10-22.json"))) as! [String: Any]
        var rows = json["standings"] as! [[String: Any]]
        let i = try #require(rows.firstIndex { $0["team_name"] as? String == "Tampa Bay Rays" })
        rows[i]["magic_number"] = "7"
        let midseason = try JSONDecoder().decode([TeamStanding].self, from: JSONSerialization.data(withJSONObject: rows))
        #expect(StandingsLegend.showsMagicNumber(midseason, divisionTables: true))
        #expect(!StandingsLegend.showsMagicNumber(midseason, divisionTables: false), "wild-card tables show none")
    }
}
