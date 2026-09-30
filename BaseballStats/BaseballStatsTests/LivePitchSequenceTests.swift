//
//  LivePitchSequenceTests.swift
//
//  A live ball in play must list its whole pitch sequence. The live
//  snapshot's contact row carries ONLY the in-play pitch; joined as a
//  plate appearance it used to become the at-bat's entire pitch list, so
//  the play sheet read "1 Pitch" for every ball in play (strikeouts, with
//  no contact row, were fine). The contact row now supplies the metrics
//  and the sequence comes from the play stream. Finished games — whose
//  plate appearances carry every pitch — are unchanged.
//

import Foundation
import Testing
@testable import BaseballStats

private func play(_ order: Int, type: String, text: String,
                  pitchType: String? = nil, speed: Double? = nil, outs: Int? = nil) -> BDLPlay {
    BDLPlay(gameId: 1, order: order, type: type, text: text, homeScore: 0, awayScore: 0,
            inning: 1, inningType: "Top", scoringPlay: false, scoreValue: nil, outs: outs,
            balls: nil, strikes: nil, batterId: 99, pitcherId: 7,
            pitchType: pitchType, pitchVelocity: speed, trajectory: nil)
}

/// Top 1: five pitches, the fifth put in play for a flyout.
private let stream: [BDLPlay] = [
    play(1, type: "Start Batter/Pitcher", text: "Aaron Judge pitches to Ben Rice"),
    play(2, type: "Ball", text: "Pitch 1 : Ball 1", pitchType: "Four-seam FB", speed: 97.4),
    play(3, type: "Strike Looking", text: "Pitch 2 : Strike 1 Looking", pitchType: "Slider", speed: 86.6),
    play(4, type: "Foul Ball", text: "Pitch 3 : Strike 2 Foul", pitchType: "Four-seam FB", speed: 98.1),
    play(5, type: "Ball", text: "Pitch 4 : Ball 2", pitchType: "Curve", speed: 81.9),
    play(6, type: "In Play", text: "Pitch 5 : In Play", pitchType: "Sinker", speed: 96.2),
    play(7, type: "Play Result", text: "Ben Rice flied out to center.", outs: 1),
]

private let liveContactJSON = """
{"batter_id": 99, "inning": 1, "half_inning": "top", "pa_number": 1, "result": "Flyout",
 "pitches": [{"exit_velocity": 101.2, "launch_angle": 28, "hit_distance": 380,
              "expected_batting_average": 0.41, "is_barrel": true}]}
"""

private func onlyAtBat(_ pas: [BDLPlateAppearance]) throws -> PlaysView.AtBat {
    let halves = PlaysView.attachContactMetrics(PlaysView.groupedHalfInnings(stream),
                                                plateAppearances: pas)
    return try #require(halves.first?.atBats.first)
}

@Suite("Live pitch sequence")
struct LivePitchSequenceTests {

    @Test func aLiveBallInPlayListsTheWholeSequence() throws {
        let live = try JSONDecoder().decode(LiveContactPA.self, from: Data(liveContactJSON.utf8))
        let pa = live.asPlateAppearance
        #expect(pa.sequenceComplete == false)
        #expect(pa.pitches?.count == 1, "the contact row really does carry one pitch")

        let ab = try onlyAtBat([pa])
        let pitches = PlayDetailSheet.pitches(stream: PlaysView.pitchRows(ab), pa: ab.paPitches)
        #expect(pitches.count == 5, "not '1 Pitch'")
        #expect(pitches.map(\.detail) == ["Four-seam FB 97.4 mph", "Slider 86.6 mph",
                                          "Four-seam FB 98.1 mph", "Curve 81.9 mph",
                                          "Sinker 96.2 mph"])
        // The contact metrics still arrive — for the pitch put in play.
        #expect(ab.contact?.exitVelocity == 101.2)
        #expect(ab.contact?.launchAngle == 28)
        #expect(ab.contact?.hitDistance == 380)
        #expect(ab.contact?.isBarrel == true)
    }

    @Test func aFinishedGameStillUsesItsOwnPitchRecords() throws {
        // A finished game's plate appearance carries every pitch, with the
        // feed's own speeds — the sheet keeps using them, unchanged.
        func detail(_ type: String, _ speed: Double, ev: Double? = nil) -> BDLPitchDetail {
            BDLPitchDetail(exitVelocity: ev, launchAngle: ev == nil ? nil : 28,
                           hitDistance: ev == nil ? nil : 380, expectedBattingAverage: nil,
                           isBarrel: nil, releaseSpeed: speed, pitchType: type,
                           callName: nil, description: nil)
        }
        let full = BDLPlateAppearance(
            batterId: 99, inning: 1, halfInning: "top", paNumber: 1, pitcherId: 7, result: "Flyout",
            pitches: [detail("Four-seam FB", 97.46), detail("Slider", 86.61), detail("Four-seam FB", 98.12),
                      detail("Curve", 81.93), detail("Sinker", 96.24, ev: 101.2)])
        #expect(full.sequenceComplete == nil, "a feed row is complete by default")
        let ab = try onlyAtBat([full])
        let pitches = PlayDetailSheet.pitches(stream: PlaysView.pitchRows(ab), pa: ab.paPitches)
        #expect(pitches.count == 5)
        #expect(pitches.first?.detail == "Four-seam FB 97.5 mph", "the feed's speed, as before")
        #expect(ab.contact?.exitVelocity == 101.2)
    }

    @Test func aPlateAppearanceFromTheFeedDecodesAsComplete() throws {
        let json = #"{"batter_id": 99, "inning": 1, "half_inning": "top", "pa_number": 1, "pitches": []}"#
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        let pa = try d.decode(BDLPlateAppearance.self, from: Data(json.utf8))
        #expect(pa.sequenceComplete == nil)
    }
}
