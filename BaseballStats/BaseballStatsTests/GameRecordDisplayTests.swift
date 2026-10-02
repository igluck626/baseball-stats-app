//
//  GameRecordDisplayTests.swift
//
//  The Scores cards' "(W-L)" now comes from `/games/records`: the record as of
//  THAT game. These pin the display rules every card and the box-score header
//  share (`GameRecordDisplay`): a final shows the record after the game, a live
//  or scheduled game the record entering it, a postseason game none; a final
//  whose server record lags is moved by its own result, never left at the
//  pre-game record; a forfeit is flagged; and the date asked for is the date
//  the card is listed under.
//

import Foundation
import Testing
@testable import BaseballStats

private func entry(final: Bool = true, postseason: Bool = false, forfeit: String? = nil,
                   awayBefore: (Int, Int)? = (52, 48), awayAfter: (Int, Int)? = (52, 49),
                   homeBefore: (Int, Int)? = (55, 44), homeAfter: (Int, Int)? = (56, 44)) throws -> GameRecordEntry {
    func wl(_ t: (Int, Int)?) -> String { t.map { #"{"w": \#($0.0), "l": \#($0.1)}"# } ?? "null" }
    let json = """
    {"game_pk": 5059890, "retro_game_id": null, "bdl_game_id": 5059890, "final": \(final),
     "postseason": \(postseason), "played": true, "forfeit": \(forfeit.map { "\"\($0)\"" } ?? "null"),
     "no_decision": false,
     "away": {"team": "PIT", "before": \(wl(awayBefore)), "after": \(wl(awayAfter))},
     "home": {"team": "NYY", "before": \(wl(homeBefore)), "after": \(wl(homeAfter))}}
    """
    return try JSONDecoder().decode(GameRecordEntry.self, from: Data(json.utf8))
}

private func game(pk: Int, iso: String) -> Game {
    let side = GameTeam(team: TeamInfo(id: 0, name: "X", abbreviation: "X"),
                        score: nil, leagueRecord: nil, isWinner: nil, probablePitcher: nil)
    return Game(gamePk: pk, gameDate: iso,
                status: GameStatus(abstractGameState: "Final", detailedState: "Final",
                                   statusCode: nil, codedGameState: nil),
                teams: GameTeams(away: side, home: side), venue: nil, linescore: nil, decisions: nil,
                bdlAwayTeamId: nil, bdlHomeTeamId: nil, bdlGameId: nil, seasonType: nil)
}

private func text(_ e: GameRecordEntry?, home: Bool, final: Bool, won: Bool? = nil) -> String? {
    GameRecordDisplay.text(GameRecordDisplay.record(e, home: home, cardIsFinal: final, wonOnCard: won))
}

@Suite("Game records on the Scores cards")
struct GameRecordDisplayTests {

    // MARK: the payload

    @Test func decodesTheEndpointPayload() throws {
        let e = try entry()
        #expect(e.gamePk == 5059890)
        #expect(e.away.before == .init(w: 52, l: 48) && e.home.after == .init(w: 56, l: 44))
        #expect(!e.isForfeit)
    }

    @Test func aResponseIsKeyedByGamePk() throws {
        let json = #"{"date": "2026-07-20", "source": "balldontlie", "games": [{"game_pk": 7, "retro_game_id": null, "bdl_game_id": 7, "final": false, "postseason": false, "played": true, "forfeit": null, "no_decision": false, "away": {"team": "A", "before": {"w": 1, "l": 0}, "after": null}, "home": {"team": "B", "before": {"w": 0, "l": 1}, "after": null}}]}"#
        let r = try JSONDecoder().decode(GameRecordsResponse.self, from: Data(json.utf8))
        #expect(r.games.map(\.gamePk) == [7])
        #expect(r.games[0].away.after == nil)
    }

    // MARK: final vs live / scheduled

    @Test func aFinalShowsTheRecordAfterTheGame() throws {
        let e = try entry()
        #expect(text(e, home: false, final: true) == "(52-49)")
        #expect(text(e, home: true, final: true) == "(56-44)")
    }

    @Test func aLiveOrScheduledGameShowsTheRecordEnteringIt() throws {
        let e = try entry(final: false, awayAfter: nil, homeAfter: nil)
        #expect(text(e, home: false, final: false) == "(52-48)")
        #expect(text(e, home: true, final: false) == "(55-44)")
    }

    @Test func aFinalTheServerHasNotCaughtUpWithIsMovedByItsOwnResult() throws {
        // The card went final; the server's 3-minute refresh has not run yet.
        let e = try entry(final: false, awayAfter: nil, homeAfter: nil)
        #expect(text(e, home: true, final: true, won: true) == "(56-44)", "the winner gains a W")
        #expect(text(e, home: false, final: true, won: false) == "(52-49)", "the loser gains an L")
        #expect(text(e, home: true, final: true, won: nil) == "(55-44)", "a tie moves neither")
    }

    @Test func theServersAfterOutranksTheCardsResult() throws {
        // A forfeit: the field score said the home side won; the record says otherwise.
        let e = try entry(forfeit: "V", awayAfter: (53, 48), homeAfter: (55, 45))
        #expect(text(e, home: true, final: true, won: true) == "(55-45)")
        #expect(text(e, home: false, final: true, won: false) == "(53-48)")
    }

    // MARK: postseason, forfeit, missing

    @Test func aPostseasonGameShowsNoRecord() throws {
        let e = try entry(postseason: true, awayBefore: nil, awayAfter: nil, homeBefore: nil, homeAfter: nil)
        #expect(text(e, home: true, final: true) == nil)
        #expect(text(e, home: true, final: false) == nil)
        // Even if a record were present, the postseason flag wins.
        let leaked = try entry(postseason: true)
        #expect(text(leaked, home: true, final: true) == nil)
    }

    @Test func aForfeitIsFlagged() throws {
        #expect(try entry(forfeit: "V").isForfeit)
        #expect(try entry(forfeit: "H").isForfeit)
        #expect(!(try entry(forfeit: nil).isForfeit))
    }

    @Test func noEntryMeansNoRecordNotAZero() {
        #expect(text(nil, home: true, final: true) == nil)
        #expect(text(nil, home: false, final: false) == nil)
    }

    // MARK: which date to ask for

    @Test func aHistoricalGameIsAskedForByItsListedDate() {
        // gameDate is midnight UTC — the previous evening in American time.
        let d = GameRecordDisplay.recordsDate(for: game(pk: -12345, iso: "1986-08-02T00:00:00Z"))
        let c = d.map { Calendar.current.dateComponents([.year, .month, .day], from: $0) }
        #expect(c?.year == 1986 && c?.month == 8 && c?.day == 2)
    }

    @Test func aCurrentSeasonGameIsAskedForByItsEasternDate() {
        // 10:10 pm EDT on July 20 is 02:10 UTC on July 21.
        let d = GameRecordDisplay.recordsDate(for: game(pk: 5059890, iso: "2026-07-21T02:10:00Z"))
        let c = d.map { Calendar.current.dateComponents([.year, .month, .day], from: $0) }
        #expect(c?.year == 2026 && c?.month == 7 && c?.day == 20)
    }
}
