//
//  HistoricalPostseasonGamesTests.swift
//
//  Past postseason games on the Scores tab (Isaac, 2026-10-29). Both fixtures
//  are the backend's OWN output for 2025-11-01, the 2025 World Series Game 7
//  (captured 2026-10-29 from production data, read-only):
//  `testdata/games-by-date/2025-11-01-world-series-g7.json` (the slate) and
//  `…-boxscore.json` (its box score).
//

import Foundation
import Testing
@testable import BaseballStats

private func fixture(_ name: String) throws -> Data {
    let url = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()   // BaseballStatsTests
        .deletingLastPathComponent()   // BaseballStats
        .deletingLastPathComponent()   // repo root
        .appendingPathComponent("testdata/games-by-date/\(name).json")
    return try Data(contentsOf: url)
}

@MainActor
@Suite("Past postseason games")
struct HistoricalPostseasonGamesTests {
    private struct Envelope: Codable { let games: [Game] }

    private func game7() throws -> Game {
        let games = try JSONDecoder().decode(Envelope.self, from: fixture("2025-11-01-world-series-g7")).games
        #expect(games.count == 1)
        return try #require(games.first)
    }

    @Test func theCardIsAPostseasonGameWithItsOwnSeriesLine() throws {
        let g = try game7()
        #expect(g.teams.away.team.abbreviation == "LAD" && g.teams.away.score == 5)
        #expect(g.teams.home.team.abbreviation == "TOR" && g.teams.home.score == 4)
        #expect(!g.isRegularSeason)
        #expect(g.seriesLine == "World Series · Game 7 · LAD wins 4-3")
        #expect(g.gamePk < 0 && g.usesRetrosheetBoxScore)
    }

    @Test func matchingToBalldontlieKeepsTheLine() throws {
        // The slate attaches balldontlie's ids for plays; the line must survive it.
        let g = try game7()
        let attached = Game(gamePk: g.gamePk, gameDate: g.gameDate, status: g.status, teams: g.teams,
                            venue: g.venue, linescore: g.linescore, decisions: g.decisions,
                            bdlAwayTeamId: nil, bdlHomeTeamId: nil, bdlGameId: 4506999,
                            seasonType: g.seasonType, seriesLine: g.seriesLine)
        #expect(attached.seriesLine == g.seriesLine && attached.providerHasPlays)
    }

    @Test func aRegularSeasonPayloadHasNoLine() throws {
        let json = #"{"gamePk": -1, "gameDate": "2025-09-27T00:00:00Z", "status": {"abstractGameState": "Final", "detailedState": "Final", "statusCode": "F", "codedGameState": "F"}, "teams": {"away": {"team": {"id": 0, "name": "A", "abbreviation": "A"}, "score": 1, "isWinner": false}, "home": {"team": {"id": 0, "name": "B", "abbreviation": "B"}, "score": 2, "isWinner": true}}}"#
        let g = try JSONDecoder().decode(Game.self, from: Data(json.utf8))
        #expect(g.seriesLine == nil && g.isRegularSeason)
    }

    @Test func theBoxScoreHasBothStaffsAndPostseasonLines() throws {
        let box = try JSONDecoder().decode(HistoricalBoxScore.self, from: fixture("2025-11-01-world-series-g7-boxscore"))
        #expect(box.gameId == "retro-TOR202511010")
        #expect(box.batterOrdering == "alphabetical")      // postseason lines carry no lineup slot
        let lad = box.teams.away, tor = box.teams.home
        #expect(lad.pitchers.count == 6 && tor.pitchers.count == 7)
        let winner = lad.pitchers.compactMap { lad.players["ID\($0)"] }
            .first { ($0.stats?.pitching?.wins ?? 0) > 0 }
        #expect(winner?.person.fullName == "Yoshinobu Yamamoto")
        // The line beside each man is his 2025 POSTSEASON through Game 7, as
        // balldontlie's is on a 2026 playoff box score: Yamamoto went 5-1, 1.45.
        #expect(winner?.seasonStats?.pitching?.era == "1.45")
        #expect(winner?.seasonStats?.pitching?.wins == 5 && winner?.seasonStats?.pitching?.losses == 1)
        let bichette = tor.players.values.first { $0.person.fullName == "Bo Bichette" }
        #expect(bichette?.seasonStats?.batting?.avg == ".348")
    }
}
