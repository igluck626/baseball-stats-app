//
//  PitchingNoteTests.swift
//
//  An empty pitching section says it's too early while the game hasn't finished —
//  in the top of the 1st the batting side hasn't pitched — and keeps "Not recorded"
//  for a finished game with no pitching line.
//

import Testing
@testable import BaseballStats

struct PitchingNoteTests {
    @Test func beforeTheFinalItIsTooEarlyNotMissing() {
        #expect(BoxScoreView.emptyPitchingNote(gameFinished: false) == "No pitching yet.")
    }

    @Test func aFinishedGameWithoutAPitchingLineStillSaysSo() {
        // e.g. 1898-09-25 PIT @ CHN: the home side has no pitching line at all.
        #expect(BoxScoreView.emptyPitchingNote(gameFinished: true) == "Not recorded for this game.")
    }
}
