//
//  TeamContactTests.swift
//
//  Team contact (AVG / xBA / 95+ mph): the block decodes from the backend's
//  shape, a payload without it still decodes (an older server), the block is
//  drawn only when the server says to and the numbers are there, and the
//  numbers are rounded once, here, batting-average style.
//

import Foundation
import Testing
@testable import BaseballStats

struct TeamContactTests {
    /// CHW @ CLE, 2026-10-05, as `team_contact.py` returns it (unrounded).
    static let block = #"""
    {"away": {"ab": 32, "h": 6, "avg": 0.1875, "xba": 0.20375, "xba_ab": 32,
              "balls_in_play": 19, "tracked_balls_in_play": 19, "tracked_share": 1.0,
              "hard_hit": 4, "tracked_batted_balls": 19},
     "home": {"ab": 32, "h": 4, "avg": 0.125, "xba": 0.19387096774193549, "xba_ab": 31,
              "balls_in_play": 23, "tracked_balls_in_play": 22, "tracked_share": 0.9565217391304348,
              "hard_hit": 10, "tracked_batted_balls": 22},
     "final": true, "show": true, "reason": null, "game_id": 15467377}
    """#

    static func liveFixture() throws -> [String: Any] {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("testdata/live-detail.json")
        return try #require(try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
    }

    @Test func decodesTheBackendBlock() throws {
        let c = try JSONDecoder().decode(TeamContact.self, from: Data(Self.block.utf8))
        #expect(c.show && c.reason == nil)
        #expect(c.away.ab == 32 && c.away.h == 6 && c.away.hardHit == 4)
        #expect(c.home.hardHit == 10 && c.home.trackedShare == 0.9565217391304348)
        #expect(TeamContact.rate(c.home.xba) == ".194")
        #expect(TeamContact.rate(c.away.avg) == ".188")
        #expect(c.isDisplayable)
    }

    @Test func aLiveSnapshotWithoutTheKeyStillDecodes() throws {
        // The checked-in snapshot predates team contact: an older server.
        let data = try JSONSerialization.data(withJSONObject: try Self.liveFixture())
        let detail = try JSONDecoder().decode(LiveGameDetail.self, from: data)
        #expect(detail.teamContact == nil)
        #expect(!detail.plays.isEmpty)
    }

    @Test func aLiveSnapshotWithTheKeyDecodesIt() throws {
        var json = try Self.liveFixture()
        json["team_contact"] = try JSONSerialization.jsonObject(with: Data(Self.block.utf8))
        let data = try JSONSerialization.data(withJSONObject: json)
        let detail = try JSONDecoder().decode(LiveGameDetail.self, from: data)
        let c = try #require(detail.teamContact)
        #expect(c.isDisplayable && c.home.xba == 0.19387096774193549)
    }

    @Test func renderedOnlyWhenTheServerSaysSoAndTheNumbersAreThere() throws {
        func make(show: Bool, reason: String?, homeXba: String = "0.2") throws -> TeamContact {
            let json = """
            {"away": {"ab": 9, "h": 2, "avg": 0.222, "xba": 0.25, "hard_hit": 3},
             "home": {"ab": 9, "h": 1, "avg": 0.111, "xba": \(homeXba), "hard_hit": 1},
             "show": \(show), "reason": \(reason.map { "\"\($0)\"" } ?? "null")}
            """
            return try JSONDecoder().decode(TeamContact.self, from: Data(json.utf8))
        }
        #expect(try make(show: true, reason: nil).isDisplayable)
        #expect(!(try make(show: false, reason: "too_early").isDisplayable))
        #expect(!(try make(show: false, reason: "untracked").isDisplayable))
        #expect(!(try make(show: false, reason: "no_data").isDisplayable))
        // The server said show but a number is missing: nothing to draw.
        #expect(!(try make(show: true, reason: nil, homeXba: "null").isDisplayable))
    }

    @Test func ratesRoundOnceBattingAverageStyle() {
        #expect(TeamContact.rate(0.1954838709677419) == ".195")   // NYY 2026-10-05
        #expect(TeamContact.rate(0.0) == ".000")
        #expect(TeamContact.rate(1.0) == "1.000")
        #expect(TeamContact.rate(nil) == "—")
    }
}
