//
//  PlaysSubstitutionTests.swift
//
//  Pitching changes and other substitutions in the All list (Isaac,
//  2026-10-29). A change made between at-bats used to join the previous
//  at-bat's group, where it was never shown; where balldontlie gives the
//  row a batter id it replaced that at-bat's headline. Each is now its own
//  announcement row, in the order it happened.
//

import Foundation
import Testing
@testable import BaseballStats

private func play(_ order: Int, _ type: String, _ text: String?, batter: Int? = nil,
                  outs: Int? = nil, scoring: Bool = false, away: Int = 0, home: Int = 0) -> BDLPlay {
    BDLPlay(gameId: 1, order: order, type: type, text: text, homeScore: home, awayScore: away,
            inning: 5, inningType: "Bottom", scoringPlay: scoring, scoreValue: scoring ? 1 : nil,
            outs: outs, balls: nil, strikes: nil, batterId: batter, pitcherId: nil,
            pitchType: nil, pitchVelocity: nil, trajectory: nil)
}

private func rows(_ plays: [BDLPlay]) throws -> [PlaysView.AtBat] {
    try #require(PlaysView.groupedHalfInnings(plays).first).atBats
}

@Suite("Substitutions in the play list")
struct PlaysSubstitutionTests {

    /// Rows copied from balldontlie game 56801 (2016 AL Wild Card): the
    /// relief row carries the previous batter's id, and headlined his RBI
    /// single — with no run line, since the relief row scores nothing.
    @Test func aReliefRowNoLongerTakesThePreviousAtBat() throws {
        let list = try rows([
            play(1, "Start Batter/Pitcher", "Carrera batting", batter: 2538),
            play(2, "Play Result", "Carrera singled to center, Saunders scored, Pillar to third.",
                 batter: 2538, outs: 1, scoring: true, away: 2, home: 2),
            play(3, "End Batter/Pitcher", nil, batter: 2538),
            play(4, "Lineup Change", "Lineup Change", batter: 2538),
            play(5, "Play Result", "Givens relieved Tillman.", batter: 2538, outs: 1),
            play(6, "Start Batter/Pitcher", "Travis batting", batter: 4496),
            play(7, "Play Result", "Travis grounded into force out.", batter: 4496, outs: 2),
        ])
        #expect(list.map { $0.resultText } == [
            "Carrera singled to center, Saunders scored, Pillar to third.",
            "Givens relieved Tillman.",
            "Travis grounded into force out.",
        ])
        #expect(list.map { $0.batterId } == [2538, nil, 4496])
        #expect(list[0].runs == 1 && list[0].outs == 1)
        #expect(list[1].outs == nil && list[1].runs == nil)
    }

    /// LAD-CIN 2026-09-09 (balldontlie 5059967): the change arrives after
    /// the new batter's start marker, before a pitch. It reads before him.
    @Test func aChangeInsideTheAtBatReadsBeforeIt() throws {
        let list = try rows([
            play(1, "Start Batter/Pitcher", "Luis Mey pitches to Will Smith", batter: 10),
            play(2, "Play Result", "Mey relieved Lowder"),
            play(3, "Ball", "Pitch 1 : Ball 1", batter: 10),
            play(4, "Play Result", "Smith walked.", batter: 10, outs: 1),
        ])
        #expect(list.map { $0.resultText } == ["Mey relieved Lowder", "Smith walked."])
        #expect(list[1].plays.map(\.text) == ["Pitch 1 : Ball 1", "Smith walked."])
    }

    /// An overturned call can land after the at-bat's end marker; it is
    /// still that at-bat's pitch, not a note.
    @Test func aPitchAfterTheEndMarkerStaysWithItsAtBat() throws {
        let list = try rows([
            play(1, "Start Batter/Pitcher", "Smith batting", batter: 10),
            play(2, "Play Result", "Smith walked.", batter: 10, outs: 0),
            play(3, "End Batter/Pitcher", nil, batter: 10),
            play(4, "Ball - Overturned", "Pitch 7 : Ball 4", batter: 10),
            play(5, "Play Result", "Monasterio hit for Anthony"),
            play(6, "Start Batter/Pitcher", "Monasterio batting", batter: 11),
        ])
        #expect(list.count == 3)
        #expect(list[0].plays.map(\.text) == ["Smith walked.", "Pitch 7 : Ball 4"])
        #expect(list[1].resultText == "Monasterio hit for Anthony" && list[1].batterId == nil)
    }

    @Test func theWordingIsRecognised() {
        let subs = ["Givens relieved Tillman.", "Upton Jr. hit for Saunders.", "Maybin ran for Gurriel.",
                    "Barnhart catching.", "Ohtani as designated hitter.", "Bradley Jr. in left field.",
                    "Arias at third base, replacing Sandoval", "Belliard at shortstop, replacing Blauser"]
        let not = ["Carrera singled to center, Saunders scored, Pillar to third.",
                   "Betts intentionally walked.", "Bellinger stole second.",
                   "Judge flied out to left field.", "Soto lined out to third base."]
        #expect(subs.allSatisfy { PlaysView.isSubstitution(play(1, "Play Result", $0)) })
        #expect(!not.contains { PlaysView.isSubstitution(play(1, "Play Result", $0)) })
        // A scoring row is never a note, whatever it says.
        #expect(!PlaysView.isSubstitution(play(1, "Play Result", "Arias at third base.", scoring: true)))
    }
}
