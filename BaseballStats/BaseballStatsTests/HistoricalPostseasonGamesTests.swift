//
//  HistoricalPostseasonGamesTests.swift
//
//  Past postseason games on the Scores tab (Isaac, 2026-10-29). Both fixtures
//  are the backend's OWN output for 2025-11-01, the 2025 World Series Game 7
//  (captured 2026-10-29 from production, read-only; the box score again after
//  the lineup load):
//  `testdata/games-by-date/2025-11-01-world-series-g7.json` (the slate) and
//  `…-boxscore.json` (its box score). Plays: `2013-10-30-world-series-g6-plays.json`,
//  `/games/{pk}/historical-plays` for a game balldontlie has no plays for, built
//  from the load plan's rows exactly as the endpoint shapes them.
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

    /// A past postseason game offers plays even with no provider id: ours,
    /// translated from Retrosheet. A regular-season one does not.
    @Test func aPastPostseasonGameHasOurPlays() throws {
        let g = try game7()
        #expect(g.hasRetrosheetPlays && g.hasPlays)
        let json = #"{"gamePk": -1, "gameDate": "1986-07-04T00:00:00Z", "status": {"abstractGameState": "Final", "detailedState": "Final", "statusCode": "F", "codedGameState": "F"}, "teams": {"away": {"team": {"id": 0, "name": "A", "abbreviation": "A"}, "score": 1, "isWinner": false}, "home": {"team": {"id": 0, "name": "B", "abbreviation": "B"}, "score": 2, "isWinner": true}}}"#
        let regular = try JSONDecoder().decode(Game.self, from: Data(json.utf8))
        #expect(!regular.hasRetrosheetPlays && !regular.hasPlays)
    }

    /// The translated plays decode as `BDLPlay` and drive the same Scoring and
    /// All lists: 2013 World Series Game 6, where balldontlie has none.
    @Test func translatedPlaysRenderLikeTheProvidersOwn() throws {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        let plays = try decoder.decode(BDLDataEnvelope<BDLPlay>.self,
                                       from: fixture("2013-10-30-world-series-g6-plays")).data
        let scoring = PlaysView.scoringRows(from: plays)
        #expect(scoring.map { "\($0.awayScore)-\($0.homeScore)" } == ["0-3", "0-4", "0-5", "0-6", "1-6"])
        #expect(scoring.first?.text?.hasPrefix("Victorino doubled to left") == true)
        #expect(PlaysView.reconcile(plays: plays, finalAway: 1, finalHome: 6).matches)
        // Every at-bat is a batted row carrying its own result and out count —
        // bottom of the 4th, nine of them, though two pitching changes fall
        // between at-bats there (inside an at-bat's group, as balldontlie's do).
        let halves = PlaysView.groupedHalfInnings(plays)
        let b4 = try #require(halves.first { $0.inning == 4 && $0.inningType == "Bottom" })
        #expect(b4.atBats.count == 9 && b4.atBats.allSatisfy { $0.batterId != nil })
        #expect(b4.atBats.compactMap(\.resultText).count == 9)
        #expect(b4.atBats.last?.outs == 3)
        // A change made between innings opens the next half as an announcement.
        let t9 = try #require(halves.first { $0.inning == 9 && $0.inningType == "Top" })
        #expect(t9.atBats.first?.batterId == nil)
        #expect(t9.atBats.first?.plays.first?.text == "Uehara relieved Workman")
    }

    @Test func aRegularSeasonPayloadHasNoLine() throws {
        let json = #"{"gamePk": -1, "gameDate": "2025-09-27T00:00:00Z", "status": {"abstractGameState": "Final", "detailedState": "Final", "statusCode": "F", "codedGameState": "F"}, "teams": {"away": {"team": {"id": 0, "name": "A", "abbreviation": "A"}, "score": 1, "isWinner": false}, "home": {"team": {"id": 0, "name": "B", "abbreviation": "B"}, "score": 2, "isWinner": true}}}"#
        let g = try JSONDecoder().decode(Game.self, from: Data(json.utf8))
        #expect(g.seriesLine == nil && g.isRegularSeason)
    }

    @Test func theBoxScoreHasBothStaffsAndPostseasonLines() throws {
        let box = try JSONDecoder().decode(HistoricalBoxScore.self, from: fixture("2025-11-01-world-series-g7-boxscore"))
        #expect(box.gameId == "retro-TOR202511010")
        // The real lineup and staff order, from the postseason lines' slot/seq
        // (loaded 2026-10-29): Ohtani led off as DH and started on the mound.
        #expect(box.batterOrdering == "lineup")
        let lad = box.teams.away, tor = box.teams.home
        #expect(lad.pitchers.count == 6 && tor.pitchers.count == 7)
        let ladOrder = lad.batters.prefix(5).compactMap { lad.players["ID\($0)"]?.person.fullName }
        #expect(ladOrder == ["Shohei Ohtani", "Will Smith", "Freddie Freeman", "Mookie Betts", "Max Muncy"])
        #expect(lad.players["ID\(lad.batters[0])"]?.position?.abbreviation == "DH")
        let ladStaff = lad.pitchers.compactMap { lad.players["ID\($0)"]?.person.fullName }
        #expect(ladStaff.first == "Shohei Ohtani" && ladStaff.last == "Yoshinobu Yamamoto")
        let winner = lad.pitchers.compactMap { lad.players["ID\($0)"] }
            .first { ($0.stats?.pitching?.wins ?? 0) > 0 }
        #expect(winner?.person.fullName == "Yoshinobu Yamamoto")
        // Pitch counts from Retrosheet's event files (loaded 2026-10-29), on every pitcher.
        let pc = { (side: BoxScoreTeam, name: String) in
            side.players.values.first { $0.person.fullName == name }?.stats?.pitching?.pitchCount }
        #expect(pc(lad, "Shohei Ohtani") == 51 && pc(lad, "Yoshinobu Yamamoto") == 34)
        #expect(pc(tor, "Max Scherzer") == 54)
        #expect((lad.pitchers + tor.pitchers).count == 13)
        #expect(lad.pitchers.allSatisfy { lad.players["ID\($0)"]?.stats?.pitching?.pitchCount != nil })
        #expect(tor.pitchers.allSatisfy { tor.players["ID\($0)"]?.stats?.pitching?.pitchCount != nil })
        // The line beside each man is his 2025 POSTSEASON through Game 7, as
        // balldontlie's is on a 2026 playoff box score: Yamamoto went 5-1, 1.45.
        #expect(winner?.seasonStats?.pitching?.era == "1.45")
        #expect(winner?.seasonStats?.pitching?.wins == 5 && winner?.seasonStats?.pitching?.losses == 1)
        let bichette = tor.players.values.first { $0.person.fullName == "Bo Bichette" }
        #expect(bichette?.seasonStats?.batting?.avg == ".348")
    }
}
