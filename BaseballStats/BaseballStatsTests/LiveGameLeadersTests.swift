//
//  LiveGameLeadersTests.swift
//  BaseballStatsTests
//
//  The live Game Leaders board: the server's top 3 decoded off the snapshot,
//  mapped to the card's entries, a live row's pitch found in the play stream —
//  and the client's FINAL ranking agreeing with the server's on ties, so a
//  game's last live top 3 and its final top 3 come out in the same order.
//
//  Fixture: testdata/live-leaders.json, written by the server's own code
//  (testdata/make-live-leaders-fixture.py) and checked by
//  backend/tests/test_game_leaders_live.py, so both languages read one file.
//

import Foundation
import Testing
@testable import BaseballStats

private let repo = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent()   // BaseballStatsTests
    .deletingLastPathComponent()   // BaseballStats
    .deletingLastPathComponent()   // repo root

/// The shared fixture. The plate appearances are balldontlie-shaped, so they
/// decode as the BDL client does (snake case converted).
private struct SharedFixture: Decodable {
    struct Top3: Decodable {
        let hardestHit: [[Ident]]
        let fastestPitches: [[Ident]]
    }
    /// [player_id, inning, half, pa_number, pitch_index, value]
    enum Ident: Decodable, Equatable {
        case int(Int), text(String), number(Double)
        init(from decoder: Decoder) throws {
            let c = try decoder.singleValueContainer()
            if let i = try? c.decode(Int.self) { self = .int(i) }
            else if let s = try? c.decode(String.self) { self = .text(s) }
            else { self = .number(try c.decode(Double.self)) }
        }
    }
    struct Game: Decodable { let top3: Top3 }
    struct Ties: Decodable {
        let identity: [String: Who]
        let pas: [BDLPlateAppearance]
        let top3: Top3
    }
    struct Who: Decodable { let name: String; let teamId: Int }

    let games: [String: Game]
    let ties: Ties
}

private func shared() throws -> SharedFixture {
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    return try decoder.decode(SharedFixture.self,
                              from: Data(contentsOf: repo.appendingPathComponent("testdata/live-leaders.json")))
}

/// The game-leaders fixture's games, for their plate appearances and names.
private struct Games: Decodable {
    struct Game: Decodable {
        let pas: [BDLPlateAppearance]
        let plays: [BDLPlay]
        let identity: [String: Who]
    }
    struct Who: Decodable { let name: String; let teamId: Int? }
}

private func games() throws -> [String: Games.Game] {
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    return try decoder.decode([String: Games.Game].self,
                              from: Data(contentsOf: repo.appendingPathComponent("testdata/game-leaders.json")))
}

private func ident(_ e: GameLeaders.Entry) -> [SharedFixture.Ident] {
    [.int(e.playerId), .int(e.pa.inning), .text(e.pa.halfInning ?? ""), .int(e.pa.paNumber),
     .int(e.pitchIndex), e.value == e.value.rounded() ? .int(Int(e.value)) : .number(e.value)]
}

private func same(_ a: [SharedFixture.Ident], _ b: [SharedFixture.Ident]) -> Bool {
    func n(_ x: SharedFixture.Ident) -> String {
        switch x {
        case .int(let i): return String(format: "%.3f", Double(i))
        case .number(let d): return String(format: "%.3f", d)
        case .text(let s): return s
        }
    }
    return a.map(n) == b.map(n)
}

@Suite("Live Game Leaders")
struct LiveGameLeadersTests {

    /// The ADDITION's test: the client's final ranking, cut to three, is the
    /// server's live top 3 in the same order — for both real games and for a
    /// constructed tie case fed out of game order.
    @Test func theFinalTop3IsTheLastLiveTop3WhenNothingIsMissing() throws {
        let fx = try shared()
        for (name, g) in try games() {
            let b = try #require(GameLeaders.build(plateAppearances: g.pas, limit: 3) { pid in
                g.identity[String(pid)].flatMap { w in w.teamId.map { (w.name, $0) } }
            })
            let want = try #require(fx.games[name]?.top3)
            #expect(b.hardestHit.count == 3 && zip(b.hardestHit, want.hardestHit).allSatisfy { same(ident($0), $1) },
                    "\(name): hardest hit")
            #expect(b.fastestPitches.count == 3 && zip(b.fastestPitches, want.fastestPitches).allSatisfy { same(ident($0), $1) },
                    "\(name): fastest pitches")
        }
    }

    @Test func tiesGoToTheEarlierEventNotToFeedOrder() throws {
        let t = try shared().ties
        let b = try #require(GameLeaders.build(plateAppearances: t.pas, limit: 3) { pid in
            t.identity[String(pid)].map { ($0.name, $0.teamId) }
        })
        // Four 99.5s, fed latest-first: the three earliest, earliest first.
        #expect(zip(b.fastestPitches, t.top3.fastestPitches).allSatisfy { same(ident($0), $1) })
        #expect(b.fastestPitches.map(\.pa.paNumber) == [1, 4, 7])
        // Three 101.0s behind a 103.0: the top of the 1st before the bottom.
        #expect(zip(b.hardestHit, t.top3.hardestHit).allSatisfy { same(ident($0), $1) })
        #expect(b.hardestHit.map(\.pa.paNumber) == [8, 1, 4])
    }

    /// A real recorded snapshot with the server's board added decodes with a
    /// BARE decoder — the one `APIClient` uses — and without it, as from an
    /// older backend, still decodes with no board.
    @Test func theSnapshotDecodesWithAndWithoutTheBoard() throws {
        let raw = try Data(contentsOf: repo.appendingPathComponent("testdata/live-detail.json"))
        let without = try JSONDecoder().decode(LiveGameDetail.self, from: raw)
        #expect(without.gameLeaders == nil)

        let board = try JSONSerialization.jsonObject(
            with: Data(contentsOf: repo.appendingPathComponent("testdata/live-leaders.json"))) as? [String: Any]
        var snap = try #require(try JSONSerialization.jsonObject(with: raw) as? [String: Any])
        snap["game_leaders"] = try #require(board?["board"])
        let with = try JSONDecoder().decode(LiveGameDetail.self,
                                            from: JSONSerialization.data(withJSONObject: snap))
        let gl = try #require(with.gameLeaders)
        #expect(gl.rows == 3 && gl.hardestHit.count == 3 && gl.fastestPitches.count == 3)
        #expect(gl.hardestHit.first?.value == 112.6 && gl.fastestPitches.first?.value == 100.9)
        #expect(with.plays.count == without.plays.count, "the rest of the snapshot is untouched")
    }

    @Test func theServersBoardMapsToEntriesInItsOwnOrder() throws {
        let json = """
        {"rows": 3,
         "hardest_hit": [{"player_id": 7, "name": "Hitter", "team_id": 1, "value": 108.2, "detail": "Double",
                          "result": "Double", "inning": 3, "half": "top", "pa_number": 21, "pitch_index": 2,
                          "pa_pitches": 3, "play_order": 500}],
         "fastest_pitches": [{"player_id": 9, "name": "Thrower", "team_id": 2, "value": 99.9, "detail": "Sinker",
                              "result": "Strikeout", "inning": 2, "half": "bottom", "pa_number": 15,
                              "pitch_index": 0, "pa_pitches": 4, "play_order": null},
                             {"player_id": 9, "name": "Thrower", "team_id": 2, "value": 99.9, "detail": "Sinker",
                              "result": "Walk", "inning": 4, "half": "bottom", "pa_number": 30,
                              "pitch_index": 1, "pa_pitches": 5, "play_order": 812}]}
        """
        let board = try JSONDecoder().decode(LiveGameLeaders.self, from: Data(json.utf8))
        let contact = BDLPlateAppearance(batterId: 7, inning: 3, halfInning: "top", paNumber: 21, pitcherId: nil,
                                         result: "Double", pitches: nil, sequenceComplete: false)
        let g = try #require(GameLeaders.live(board, contactPAs: [contact]))
        #expect(g.hardestHit.map(\.kind) == [.hit] && g.hardestHit[0].pa.batterId == 7)
        #expect(g.fastestPitches.map(\.pa.paNumber) == [15, 30], "kept in the server's order, not re-ranked")
        #expect(g.fastestPitches.allSatisfy { $0.kind == .pitch && $0.pa.pitcherId == 9 })
        #expect(g.fastestPitches.map(\.playOrder) == [nil, 812])
        #expect(g.hardestHit[0].pa.sequenceComplete == false, "a live row's pitch list is never the whole at-bat")
        #expect(g.fastestPitches[0].detail == "Sinker" && g.hardestHit[0].detail == "Double")

        let empty = try JSONDecoder().decode(LiveGameLeaders.self,
                                             from: Data(#"{"rows": 3, "hardest_hit": [], "fastest_pitches": []}"#.utf8))
        #expect(GameLeaders.live(empty) == nil, "nothing to show: no card")
    }

    /// A live pitch row opens its at-bat from the stream with its own pitch
    /// marked: real plays from game 5059936.
    @Test func aLivePitchIsFoundInTheStreamWithItsAtBat() throws {
        let g = try #require(try games()["stealAndSubs"])
        let sorted = g.plays.sorted { $0.order < $1.order }
        // The third pitch of the first at-bat that has three or more.
        var atBat: [BDLPlay] = []
        var pick: [BDLPlay] = []
        for p in sorted {
            if p.type == "Start Batter/Pitcher" {
                if atBat.count >= 3 { pick = atBat; break }
                atBat = []
            } else if (p.text ?? "").hasPrefix("Pitch ") {
                atBat.append(p)
            }
        }
        let target = try #require(pick.count >= 3 ? pick[2] : nil)
        let ab = try #require(GameLeaders.streamAtBat(plays: g.plays, containing: target.order))
        #expect(ab.rows.map(\.order) == pick.map(\.order))
        #expect(ab.index == 2)
        #expect(ab.sentence != nil, "the at-bat's outcome sentence comes with it")
        #expect(GameLeaders.streamAtBat(plays: g.plays, containing: -1)?.index == nil, "no such row: no sheet guess")
    }
}
