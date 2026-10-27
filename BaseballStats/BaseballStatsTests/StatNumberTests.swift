//
//  StatNumberTests.swift
//
//  A stat of 1,000 or more reads with a thousands comma (Isaac, 2026-10-27).
//  The Season Stats card showed the Yankees' 1450 strikeouts as "1450" — a
//  real value, from testdata/season-stats/season-stats-2026-with-ra.json.
//

import Foundation
import Testing
@testable import BaseballStats

struct StatNumberTests {
    @Test func groupsFromOneThousand() {
        #expect(StatNumber.grouped(999) == "999")
        #expect(StatNumber.grouped(1000) == "1,000")
        #expect(StatNumber.grouped(1140) == "1,140")
        #expect(StatNumber.grouped(4256) == "4,256")
        #expect(StatNumber.grouped(1_234_567) == "1,234,567")
        #expect(StatNumber.grouped(0) == "0")
        #expect(StatNumber.grouped(-1140) == "-1,140")
    }

    @Test func groupsInningsInBaseballNotation() {
        #expect(StatNumber.groupedIP("3000.1") == "3,000.1")
        #expect(StatNumber.groupedIP("7356") == "7,356")
        #expect(StatNumber.groupedIP("85.2") == "85.2")
        #expect(StatNumber.groupedIP("—") == "—")
    }

    @Test func theYankeesStrikeoutsOnTheSeasonStatsCard() throws {
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().appendingPathComponent("testdata/season-stats/season-stats-2026-with-ra.json")
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        let r = try d.decode(TeamSeasonStatsResponse.self, from: Data(contentsOf: url))
        let nyy = try #require(r.teams.first { $0.bdlId == 19 })
        #expect(SeasonStatsFormat.value("SO", nyy.stats["SO"]?.value) == "1,450")
        #expect(SeasonStatsFormat.value("RA", nyy.stats["RA"]?.value) == "601")
    }

    @Test func leaderRowsGroupCountsAndInnings() {
        #expect(formatLeaderValue(1450, stat: "SO") == "1,450")
        #expect(formatLeaderValue(3000.334, stat: "IP") == "3,000.1")
        #expect(formatLeaderValue(0.3, stat: "AVG") == ".300")
    }
}
