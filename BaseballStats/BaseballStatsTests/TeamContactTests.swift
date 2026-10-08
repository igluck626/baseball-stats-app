//
//  TeamContactTests.swift
//
//  Team Stats (inside the team-contact block): the block decodes from the
//  backend's real shape (CHW @ CLE, 2026-10-05, as team_contact.py returns it), a
//  payload without it still decodes (an older server), the rows draw in the
//  server's order with each value formatted once, and a row the server holds back
//  or can't fill doesn't draw. And when the card shows: pre-game, live before
//  and after the first plate appearance, mid-game and final.
//

import Foundation
import Testing
@testable import BaseballStats

struct TeamContactTests {
    /// CHW @ CLE, 2026-10-05: the backend's own output for /games/15467377/team-contact.
    static let block = #"""
    {"game_id": 15467377, "away": {"ab": 32, "h": 6, "avg": 0.1875, "xba": 0.20406250000000004, "xba_ab": 32, "balls_in_play": 19, "tracked_balls_in_play": 19, "tracked_share": 1.0, "hard_hit": 4, "tracked_batted_balls": 19}, "home": {"ab": 32, "h": 4, "avg": 0.125, "xba": 0.19387096774193543, "xba_ab": 31, "balls_in_play": 23, "tracked_balls_in_play": 22, "tracked_share": 0.9565217391304348, "hard_hit": 10, "tracked_batted_balls": 22}, "final": true, "show": true, "reason": null, "stats": {"rows": ["avg", "xba", "xbh", "hr", "risp", "lob", "bb", "so", "sb", "dp"], "hidden": {}, "away": {"avg": 0.1875, "xba": 0.20406250000000004, "xbh": 1, "hr": 0, "risp_h": 3, "risp_ab": 8, "lob": 5, "bb": 4, "so": 13, "sb": 0, "dp": 0, "pa": 36, "feed_pa": 36}, "home": {"avg": 0.125, "xba": 0.19387096774193543, "xbh": 1, "hr": 0, "risp_h": 0, "risp_ab": 5, "lob": 5, "bb": 3, "so": 9, "sb": 0, "dp": 1, "pa": 35, "feed_pa": 35}, "unrecognised_events": []}}
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
        #expect(stats.rows == ["avg", "xba", "xbh", "hr", "risp", "lob", "bb", "so", "sb", "dp"])
        let rows = stats.displayRows
        #expect(rows.map(\.label) == ["AVG", "xBA", "XBH", "HR", "RISP", "LOB", "BB", "SO", "SB",
                                       "Double plays"])
        func row(_ k: String) -> TeamStats.Row? { rows.first { $0.key == k } }
        #expect(row("avg")?.away == ".188" && row("avg")?.home == ".125")
        #expect(row("xba")?.away == ".204" && row("xba")?.home == ".194")
        // CHW 1 double; CLE 1 triple.
        #expect(row("xbh")?.away == "1" && row("xbh")?.home == "1")
        // Baseball-Reference: CHW 3 for 8, CLE 0 for 5 with RISP; Team LOB 5 and 5.
        #expect(row("risp")?.away == "3-for-8" && row("risp")?.home == "0-for-5")
        #expect(row("lob")?.away == "5" && row("lob")?.home == "5")
        #expect(row("dp")?.away == "0" && row("dp")?.home == "1")
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
        #expect(stats.displayRows.count == 10)
    }

    @Test func rowsTheServerHoldsBackOrCantFillDontDraw() throws {
        func make(_ rows: String, rispAb: String = "4") throws -> TeamStats {
            let side = #"{"avg": 0.25, "xba": 0.3, "xbh": 2, "hr": 1, "risp_h": 1, "risp_ab": \#(rispAb), "lob": 3, "bb": 1, "so": 2, "sb": 0, "dp": 1}"#
            let json = #"{"rows": \#(rows), "away": \#(side), "home": \#(side), "hidden": {"xba": "no_data"}}"#
            return try JSONDecoder().decode(TeamStats.self, from: Data(json.utf8))
        }
        // xBA held back before a side has batted: the server leaves it out of `rows`.
        #expect(try make(#"["avg", "xbh", "risp", "lob"]"#).displayRows.map(\.key) == ["avg", "xbh", "risp", "lob"])
        // A row the server lists but can't fill for either side is skipped, not drawn blank.
        #expect(try make(#"["avg", "risp"]"#, rispAb: "null").displayRows.map(\.key) == ["avg"])
        // A row key this build doesn't know (a newer server) is skipped — and so
        // are the two rows this build dropped, should an older server send them.
        #expect(try make(#"["avg", "wpa", "hard_hit", "pitches"]"#).displayRows.map(\.key) == ["avg"])
        // Nothing to show before the first plate appearance: no rows, so no card.
        #expect(try make("[]").displayRows.isEmpty)
    }

    // MARK: - When the card shows (states a-e)

    /// Live blocks in the shape team_contact.py sends them (MIL @ SD 2026-10-06,
    /// replayed): b before the first plate appearance, c after it, d mid-game.
    static let liveB = #"{"show":false,"reason":"no_data","away":{"ab":0,"h":0,"avg":null,"xba":null,"hard_hit":0},"home":{"ab":0,"h":0,"avg":null,"xba":null,"hard_hit":0},"stats":{"rows":[],"hidden":{"xba":"no_data"},"away":{"avg":null,"xba":null,"xbh":0,"hr":0,"risp_h":0,"risp_ab":0,"lob":0,"bb":0,"so":0,"sb":0,"dp":0},"home":{"avg":null,"xba":null,"xbh":0,"hr":0,"risp_h":0,"risp_ab":0,"lob":0,"bb":0,"so":0,"sb":0,"dp":0}}}"#
    static let liveC = #"{"show":true,"reason":null,"away":{"ab":1,"h":0,"avg":0.0,"xba":0.75,"hard_hit":1},"home":{"ab":0,"h":0,"avg":null,"xba":null,"hard_hit":0},"stats":{"rows":["avg","xba","xbh","hr","risp","lob","bb","so","sb","dp"],"hidden":{},"away":{"avg":0.0,"xba":0.75,"xbh":0,"hr":0,"risp_h":0,"risp_ab":0,"lob":0,"bb":0,"so":0,"sb":0,"dp":0},"home":{"avg":null,"xba":null,"xbh":0,"hr":0,"risp_h":0,"risp_ab":0,"lob":0,"bb":0,"so":0,"sb":0,"dp":0}}}"#
    static let liveD = #"{"show":true,"reason":null,"away":{"ab":19,"h":6,"avg":0.3157894736842105,"xba":0.2542105263157895,"hard_hit":11},"home":{"ab":18,"h":4,"avg":0.2222222222222222,"xba":0.23555555555555557,"hard_hit":4},"stats":{"rows":["avg","xba","xbh","hr","risp","lob","bb","so","sb","dp"],"hidden":{},"away":{"avg":0.3157894736842105,"xba":0.2542105263157895,"xbh":3,"hr":0,"risp_h":1,"risp_ab":2,"lob":4,"bb":0,"so":3,"sb":0,"dp":0},"home":{"avg":0.2222222222222222,"xba":0.23555555555555557,"xbh":1,"hr":0,"risp_h":1,"risp_ab":2,"lob":3,"bb":1,"so":4,"sb":1,"dp":0}}}"#

    static func decode(_ json: String) throws -> TeamContact {
        try JSONDecoder().decode(TeamContact.self, from: Data(json.utf8))
    }

    @Test func aPreGameHasNoCard() {
        // No block at all: the finished-game endpoint isn't asked before the
        // final, and a game not yet live has no snapshot.
        #expect(TeamStats.card(for: nil) == nil)
    }

    @Test func liveBeforeTheFirstPlateAppearanceHasNoCard() throws {
        #expect(TeamStats.card(for: try Self.decode(Self.liveB)) == nil)
    }

    @Test func liveAfterTheFirstAtBatShowsTheCard() throws {
        let stats = try #require(TeamStats.card(for: try Self.decode(Self.liveC)))
        #expect(stats.displayRows.count == 10)
    }

    /// Away has batted, home hasn't (the top of the 1st): AVG and xBA show the
    /// away side's value and "—" for the home side, rather than waiting.
    @Test func aSideYetToBatReadsADash() throws {
        let rows = try #require(TeamStats.card(for: try Self.decode(Self.liveC))).displayRows
        let avg = try #require(rows.first { $0.key == "avg" })
        let xba = try #require(rows.first { $0.key == "xba" })
        #expect(avg.away == ".000" && avg.home == "—")
        #expect(xba.away == ".750" && xba.home == "—")
        // And the reverse, should the home side ever have a value the away side lacks.
        let side = #"{"avg": 0.25, "xba": 0.3, "xbh": 2, "hr": 1, "risp_h": 1, "risp_ab": 4, "lob": 3, "bb": 1, "so": 2, "sb": 0, "dp": 1}"#
        let empty = #"{"avg": null, "xba": null, "xbh": 0, "hr": 0, "risp_h": 0, "risp_ab": 0, "lob": 0, "bb": 0, "so": 0, "sb": 0, "dp": 0}"#
        let json = #"{"rows": ["avg", "xba"], "away": \#(empty), "home": \#(side), "hidden": {}}"#
        let rev = try JSONDecoder().decode(TeamStats.self, from: Data(json.utf8)).displayRows
        #expect(rev.map(\.away) == ["—", "—"] && rev.map(\.home) == [".250", ".300"])
    }

    @Test func liveMidGameShowsEveryRow() throws {
        let stats = try #require(TeamStats.card(for: try Self.decode(Self.liveD)))
        #expect(stats.displayRows.count == 10)
    }

    @Test func aFinalShowsEveryRow() throws {
        let stats = try #require(TeamStats.card(for: try Self.decode(Self.block)))
        #expect(stats.displayRows.count == 10)
        // An older server's block (no stats) draws no card.
        let old = #"{"away": {"ab": 9, "h": 2, "avg": 0.222, "xba": 0.25, "hard_hit": 3},"# +
                  #" "home": {"ab": 9, "h": 1, "avg": 0.111, "xba": 0.2, "hard_hit": 1}, "show": true, "reason": null}"#
        #expect(TeamStats.card(for: try Self.decode(old)) == nil)
    }

    @Test func ratesRoundOnceBattingAverageStyle() {
        #expect(TeamContact.rate(0.1954838709677419) == ".195")   // NYY 2026-10-05
        #expect(TeamContact.rate(0.0) == ".000")
        #expect(TeamContact.rate(1.0) == "1.000")
        #expect(TeamContact.rate(nil) == "—")
        #expect(TeamStats.value("risp", .init(avg: nil, xba: nil, xbh: nil, hr: nil, rispH: 0, rispAb: 4,
                                               lob: nil, bb: nil, so: nil, sb: nil, dp: nil)) == "0-for-4")
    }
}
