//
//  ScoringPlayLatchTests.swift
//  BaseballStatsTests
//
//  When a live game's Plays section opens by itself (`ScoringPlayLatch`):
//  only for a scoring play that arrives after the box score is opened, never
//  for the ones already in the game when it appears — and the ordering in
//  `BoxScoreViewModel.applyLiveDetail` that guarantees it appears with them.
//

import Combine
import Foundation
import Testing
@testable import BaseballStats

private func play(_ order: Int, scoring: Bool) -> BDLPlay {
    BDLPlay(gameId: 1, order: order, type: "Play Result", text: nil, homeScore: 0, awayScore: 0,
            inning: 1, inningType: "Top", scoringPlay: scoring, scoreValue: scoring ? 1 : nil, outs: nil,
            balls: nil, strikes: nil, batterId: 99, pitcherId: 7,
            pitchType: nil, pitchVelocity: nil, trajectory: nil)
}

/// A game in progress: two scoring plays among four.
private let twoScoring = [play(1, scoring: false), play(2, scoring: true),
                          play(3, scoring: false), play(4, scoring: true)]

struct ScoringPlayLatchTests {

    /// Tonight's CHW @ CLE: opened with a homer already on the board, the
    /// first refresh (a new pitch, no new run) opened Plays.
    @Test func scoringPlaysAlreadyThereDontOpenIt() {
        var latch = ScoringPlayLatch()
        latch.appear(twoScoring)
        let pitch = latch.refresh(twoScoring + [play(5, scoring: false)])
        #expect(!pitch, "same two scoring plays, one more pitch")
        let another = latch.refresh(twoScoring + [play(5, scoring: false), play(6, scoring: false)])
        #expect(!another)
    }

    @Test func aThirdScoringPlayOpensIt() {
        var latch = ScoringPlayLatch()
        latch.appear(twoScoring)
        let pitch = latch.refresh(twoScoring + [play(5, scoring: false)])
        #expect(!pitch)
        let run = latch.refresh(twoScoring + [play(5, scoring: false), play(6, scoring: true)])
        #expect(run)
        let after = latch.refresh(twoScoring + [play(5, scoring: false), play(6, scoring: true), play(7, scoring: false)])
        #expect(!after, "once per arrival, not on every refresh after it")
    }

    @Test func theFirstRunOfAScorelessGameOpensIt() {
        var latch = ScoringPlayLatch()
        let scoreless = [play(1, scoring: false), play(2, scoring: false)]
        latch.appear(scoreless)
        let pitch = latch.refresh(scoreless + [play(3, scoring: false)])
        #expect(!pitch)
        let run = latch.refresh(scoreless + [play(3, scoring: false), play(4, scoring: true)])
        #expect(run)
    }

    /// What `appear` relies on: the live Plays section is built only inside
    /// the situation card, which exists only once `vm.live` is set, and
    /// `applyLiveDetail` sets `plays` BEFORE `live`. So the section can never
    /// appear empty and then receive the game's existing scoring plays as an
    /// "arrival". Publishing `live` first would break that; this catches it.
    @MainActor
    @Test func theLiveSnapshotsPlaysAreSetBeforeTheSectionCanAppear() throws {
        let detail = try JSONDecoder().decode(LiveGameDetail.self, from: liveFixtureData())
        let vm = BoxScoreViewModel(game: undatedGame())
        #expect(vm.live == nil, "no situation card, so no live Plays section, before a snapshot")

        var playsWhenLiveArrived: [BDLPlay]?
        let watch = vm.$live.sink { live in
            if live != nil, playsWhenLiveArrived == nil { playsWhenLiveArrived = vm.plays }
        }
        defer { watch.cancel() }
        vm.applyLiveDetail(detail)

        #expect(vm.live != nil)
        #expect(playsWhenLiveArrived?.filter(\.scoringPlay).count == 2,
                "the fixture's two scoring plays were already set when live arrived")
    }
}

/// testdata/live-detail.json: a captured in-progress snapshot, 2 scoring plays.
private func liveFixtureData() throws -> Data {
    let url = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()   // BaseballStatsTests
        .deletingLastPathComponent()   // BaseballStats
        .deletingLastPathComponent()   // repo root
        .appendingPathComponent("testdata/live-detail.json")
    return try Data(contentsOf: url)
}

/// No parseable date, so the snapshot's pitcher and batter lookups return
/// before any network call.
private func undatedGame() -> Game {
    let team = { (name: String, abbr: String) in
        GameTeam(team: TeamInfo(id: 0, name: name, abbreviation: abbr),
                 score: nil, leagueRecord: nil, isWinner: nil, probablePitcher: nil)
    }
    return Game(
        gamePk: 1, gameDate: "",
        status: GameStatus(abstractGameState: "Live", detailedState: "In Progress",
                           statusCode: nil, codedGameState: nil),
        teams: GameTeams(away: team("Away", "AWY"), home: team("Home", "HOM")),
        venue: nil, linescore: nil, decisions: nil,
        bdlAwayTeamId: nil, bdlHomeTeamId: nil, bdlGameId: nil, seasonType: "regular")
}
