//
//  SeasonStatsCardTests.swift
//
//  The Season Stats card reads GET /teams/season-stats and only formats: the
//  server ranks every stat, Run differential included. Real responses
//  (testdata/season-stats/, captured 2026-10-19):
//    • season-stats-2026-live.json — the deployed server, before Run
//      differential: the card must read it and simply show no RD row;
//    • season-stats-2026-with-rd.json — the server change run locally on the
//      same day's balldontlie totals and our standings: RD for all 30 clubs;
//    • season-stats-2026-with-ra.json — the Runs allowed change, run on
//      2026-10-27's balldontlie totals and production's standings (the
//      server's own functions, the way the endpoint calls them).
//

import Foundation
import Testing
@testable import BaseballStats

private func response(_ name: String) throws -> TeamSeasonStatsResponse {
    let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().appendingPathComponent("testdata/season-stats/\(name).json")
    let d = JSONDecoder()
    d.keyDecodingStrategy = .convertFromSnakeCase
    return try d.decode(TeamSeasonStatsResponse.self, from: Data(contentsOf: url))
}

struct SeasonStatsCardTests {
    @Test func yankeesRunDifferentialReadsAsServed() throws {
        let r = try response("season-stats-2026-with-rd")
        #expect(r.teams.count == 30)
        let nyy = try #require(r.teams.first { $0.bdlId == 19 })
        let rd = try #require(nyy.stats["RD"])
        #expect(rd.label == "Run differential")
        #expect(SeasonStatsFormat.value("RD", rd.value) == "+138")
        #expect(SeasonStatsFormat.rank(rd) == "4th")
        #expect(SeasonStatsFormat.spokenRank(rd) == "4th of 30 in MLB")
    }

    @Test func aTieAndANegativeDifferential() throws {
        let r = try response("season-stats-2026-with-rd")
        let sea = try #require(r.teams.first { $0.team == "SEA" }?.stats["RD"])
        let min = try #require(r.teams.first { $0.team == "MIN" }?.stats["RD"])
        #expect(SeasonStatsFormat.value("RD", sea.value) == "−58")
        #expect(sea.mlbRank == min.mlbRank && sea.mlbTied == true && min.mlbTied == true)
        #expect(SeasonStatsFormat.rank(sea).hasPrefix("T-"))
        #expect(SeasonStatsFormat.spokenRank(sea).hasPrefix("tied "))
    }

    @Test func everyCardStatIsServed() throws {
        let r = try response("season-stats-2026-with-ra")
        let keys = SeasonStatsFormat.overall + SeasonStatsFormat.batting + SeasonStatsFormat.pitching
        #expect(keys.count == 14)
        for t in r.teams {
            for k in keys { #expect(t.stats[k] != nil, "\(t.team) \(k)") }
        }
    }

    /// Runs allowed heads Pitching, as Runs heads Batting; fewest ranks 1st.
    @Test func yankeesRunsAllowedHeadsPitching() throws {
        let r = try response("season-stats-2026-with-ra")
        let nyy = try #require(r.teams.first { $0.bdlId == 19 })
        let ra = try #require(nyy.stats["RA"])
        #expect(SeasonStatsFormat.pitching.first == "RA")
        #expect(SeasonStatsFormat.batting.first == "R")
        #expect(ra.label == "Runs allowed")
        #expect(SeasonStatsFormat.value("RA", ra.value) == "601")
        #expect(SeasonStatsFormat.rank(ra) == "2nd")
        let fewest = try #require(r.teams.min { ($0.stats["RA"]?.value ?? .infinity) < ($1.stats["RA"]?.value ?? .infinity) })
        #expect(fewest.stats["RA"]?.mlbRank == 1)
    }

    @Test func theDeployedServerHasNoRunDifferentialYet() throws {
        let r = try response("season-stats-2026-live")
        let nyy = try #require(r.teams.first { $0.bdlId == 19 })
        #expect(nyy.stats["RD"] == nil)
        #expect(SeasonStatsFormat.value("HR", nyy.stats["HR"]?.value) == "223")
        #expect(SeasonStatsFormat.value("ERA", nyy.stats["ERA"]?.value) == "3.24")
        #expect(SeasonStatsFormat.value("AVG", nyy.stats["AVG"]?.value) == ".235")
        #expect(SeasonStatsFormat.rank(try #require(nyy.stats["R"])) == "T-9th")
    }

    @Test func noValueReadsAsADash() {
        #expect(SeasonStatsFormat.value("RD", nil) == "—")
        let none = TeamSeasonStatsResponse.Stat(label: "Run differential", value: nil, mlbRank: nil, mlbTied: false)
        #expect(SeasonStatsFormat.rank(none) == "—")
        #expect(SeasonStatsFormat.value("RD", 0) == "0")
    }
}
