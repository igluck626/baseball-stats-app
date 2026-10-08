//
//  TeamContactTests.swift
//
//  Team Stats (inside the team-contact block): the block decodes from the
//  backend's real shape (CHW @ CLE, 2026-10-05, as team_contact.py returns it), a
//  payload without it still decodes (an older server), the rows draw in the
//  server's order with each value formatted once, and a row the server holds back
//  or can't fill doesn't draw.
//

import Foundation
import Testing
@testable import BaseballStats

struct TeamContactTests {
    /// CHW @ CLE, 2026-10-05: the backend's own output for /games/15467377/team-contact.
    static let block = #"""
    {"game_id": 15467377, "away": {"ab": 32, "h": 6, "avg": 0.1875, "xba": 0.20406250000000004, "xba_ab": 32, "balls_in_play": 19, "tracked_balls_in_play": 19, "tracked_share": 1.0, "hard_hit": 4, "tracked_batted_balls": 19}, "home": {"ab": 32, "h": 4, "avg": 0.125, "xba": 0.19387096774193543, "xba_ab": 31, "balls_in_play": 23, "tracked_balls_in_play": 22, "tracked_share": 0.9565217391304348, "hard_hit": 10, "tracked_batted_balls": 22}, "final": true, "show": true, "reason": null, "stats": {"rows": ["avg", "xba", "hard_hit", "hr", "risp", "lob", "bb", "so", "sb", "dp", "pitches"], "hidden": {}, "away": {"avg": 0.1875, "xba": 0.20406250000000004, "hard_hit": 4, "hr": 0, "risp_h": 3, "risp_ab": 8, "lob": 5, "bb": 4, "so": 13, "sb": 0, "dp": 0, "pitches": 147, "pa": 36, "feed_pa": 36}, "home": {"avg": 0.125, "xba": 0.19387096774193543, "hard_hit": 10, "hr": 0, "risp_h": 0, "risp_ab": 5, "lob": 5, "bb": 3, "so": 9, "sb": 0, "dp": 1, "pitches": 147, "pa": 35, "feed_pa": 35}, "unrecognised_events": []}}
    """#

    static func liveFixture() throws -> [String: Any] {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("testdata/live-detail.json")
        return try #require(try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
    }

    @Test func decodesTheBackendBlockWithTeamStats() throws {
        let c = try JSONDecoder().decode(TeamContact.self, from: Data(Self.block.utf8))
        #expect(c.show && c.reason == nil)
        let stats = try #require(c.stats)
        #expect(stats.rows == ["avg", "xba", "hard_hit", "hr", "risp", "lob", "bb", "so", "sb", "dp", "pitches"])
        let rows = stats.displayRows
        #expect(rows.map(\.label) == ["AVG", "xBA", "Hit 95+ mph", "HR", "RISP", "LOB", "BB", "SO", "SB",
                                       "Double plays", "Pitches"])
        func row(_ k: String) -> TeamStats.Row? { rows.first { $0.key == k } }
        #expect(row("avg")?.away == ".188" && row("avg")?.home == ".125")
        #expect(row("xba")?.away == ".204" && row("xba")?.home == ".194")
        #expect(row("hard_hit")?.away == "4" && row("hard_hit")?.home == "10")
        // Baseball-Reference: CHW 3 for 8, CLE 0 for 5 with RISP; Team LOB 5 and 5.
        #expect(row("risp")?.away == "3-for-8" && row("risp")?.home == "0-for-5")
        #expect(row("lob")?.away == "5" && row("lob")?.home == "5")
        #expect(row("dp")?.away == "0" && row("dp")?.home == "1")
        #expect(row("pitches")?.away == "147" && row("pitches")?.home == "147")
    }

    @Test func aBlockFromAnOlderServerHasNoStats() throws {
        let old = #"{"away": {"ab": 9, "h": 2, "avg": 0.222, "xba": 0.25, "hard_hit": 3},"# +
                  #" "home": {"ab": 9, "h": 1, "avg": 0.111, "xba": 0.2, "hard_hit": 1}, "show": true, "reason": null}"#
        let c = try JSONDecoder().decode(TeamContact.self, from: Data(old.utf8))
        #expect(c.stats == nil)
    }

    @Test func aLiveSnapshotWithoutTheKeyStillDecodes() throws {
        // The checked-in snapshot predates team contact: an older server.
        let data = try JSONSerialization.data(withJSONObject: try Self.liveFixture())
        let detail = try JSONDecoder().decode(LiveGameDetail.self, from: data)
        #expect(detail.teamContact == nil)
        #expect(!detail.plays.isEmpty)
    }

    @Test func aLiveSnapshotWithTheKeyDecodesTeamStats() throws {
        var json = try Self.liveFixture()
        json["team_contact"] = try JSONSerialization.jsonObject(with: Data(Self.block.utf8))
        let data = try JSONSerialization.data(withJSONObject: json)
        let detail = try JSONDecoder().decode(LiveGameDetail.self, from: data)
        let stats = try #require(detail.teamContact?.stats)
        #expect(stats.displayRows.count == 11)
    }

    @Test func rowsTheServerHoldsBackOrCantFillDontDraw() throws {
        func make(_ rows: String, rispAb: String = "4") throws -> TeamStats {
            let side = #"{"avg": 0.25, "xba": 0.3, "hard_hit": 2, "hr": 1, "risp_h": 1, "risp_ab": \#(rispAb), "lob": 3, "bb": 1, "so": 2, "sb": 0, "dp": 1, "pitches": 40}"#
            let json = #"{"rows": \#(rows), "away": \#(side), "home": \#(side), "hidden": {"xba": "no_data"}}"#
            return try JSONDecoder().decode(TeamStats.self, from: Data(json.utf8))
        }
        // xBA held back before a side has batted: the server leaves it out of `rows`.
        #expect(try make(#"["avg", "hard_hit", "risp", "lob"]"#).displayRows.map(\.key) == ["avg", "hard_hit", "risp", "lob"])
        // A row the server lists but can't fill for a side is skipped, not drawn blank.
        #expect(try make(#"["avg", "risp"]"#, rispAb: "null").displayRows.map(\.key) == ["avg"])
        // A row key this build doesn't know (a newer server) is skipped.
        #expect(try make(#"["avg", "wpa"]"#).displayRows.map(\.key) == ["avg"])
        // Nothing to show before the first plate appearance: no rows, so no card.
        #expect(try make("[]").displayRows.isEmpty)
    }

    @Test func ratesRoundOnceBattingAverageStyle() {
        #expect(TeamContact.rate(0.1954838709677419) == ".195")   // NYY 2026-10-05
        #expect(TeamContact.rate(0.0) == ".000")
        #expect(TeamContact.rate(1.0) == "1.000")
        #expect(TeamContact.rate(nil) == "—")
        #expect(TeamStats.value("risp", .init(avg: nil, xba: nil, hardHit: nil, hr: nil, rispH: 0, rispAb: 4,
                                               lob: nil, bb: nil, so: nil, sb: nil, dp: nil, pitches: nil)) == "0-for-4")
    }
}
