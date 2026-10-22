//
//  StandingsMagicNumberTests.swift
//
//  A division leader's magic number only shows while it counts down (1–25).
//  The real response (testdata/standings/standings-2026-10-22.json, captured
//  from production after the 2026-10-22 nightly) has every division leader
//  at 0 or below — the number keeps falling after a clinch — which read on
//  Standings as "M0", "M-3" (Tampa Bay) and "M-13" (Milwaukee).
//

import Foundation
import Testing
@testable import BaseballStats

private func standings() throws -> [TeamStanding] {
    let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().appendingPathComponent("testdata/standings/standings-2026-10-22.json")
    let r = try JSONDecoder().decode(StandingsResponse.self, from: Data(contentsOf: url))
    return try #require(r.standings)
}

struct StandingsMagicNumberTests {

    @Test func tampaBayClinchedSoShowsNoMagicNumber() throws {
        let tb = try #require(try standings().first { $0.team_name == "Tampa Bay Rays" })
        #expect(tb.magic_number == "-3")
        #expect(tb.division_leader == true)
        #expect(tb.shownMagicNumber == nil)
    }

    @Test func noDivisionLeaderShowsOneAfterTheSeason() throws {
        let leaders = try standings().filter { $0.division_leader == true }
        #expect(leaders.count == 6)
        #expect(leaders.allSatisfy { (Int($0.magic_number ?? "") ?? 1) <= 0 }, "every leader has clinched")
        #expect(leaders.allSatisfy { $0.shownMagicNumber == nil })
    }

    @Test func aCountdownStillShows() throws {
        // The response's own countdowns (non-leaders, so never drawn, but the rule is the same).
        let sea = try #require(try standings().first { $0.team_name == "Seattle Mariners" })
        #expect(sea.shownMagicNumber == "6")
        let stl = try #require(try standings().first { $0.team_name == "St. Louis Cardinals" })
        #expect(stl.magic_number == "27")
        #expect(stl.shownMagicNumber == nil, "above 25 says nothing yet")
    }
}
