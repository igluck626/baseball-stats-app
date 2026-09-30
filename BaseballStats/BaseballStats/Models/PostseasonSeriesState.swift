//
//  PostseasonSeriesState.swift
//  BaseballStats
//
//  Codable models for `GET /postseason/series?season=YYYY` — the LIVE series
//  state for the current postseason (series score, game number, round). Not to
//  be confused with `Postseason.swift`, which is Lahman's completed-series
//  history behind the Playoff History bracket.
//
//  balldontlie has no series field of its own; the backend derives all of this
//  (`backend/api/postseason_series.py`) and hands the client finished display
//  lines, so the client never re-implements the rule.
//

import Foundation

struct PostseasonSeriesResponse: Codable, Hashable {
    let season: Int
    let series: [PostseasonSeriesState]
}

struct PostseasonSeriesState: Codable, Hashable {
    let teams: [String]
    let league: String?
    let round: String?          // "WC" / "DS" / "CS" / "WS"; nil when seeds can't be trusted
    let roundName: String?      // "AL Wild Card" / "ALDS" / "ALCS" / "World Series"
    let bestOf: Int?
    let wins: [String: Int]
    let isOver: Bool
    let winner: String?
    let games: [PostseasonSeriesGame]

    enum CodingKeys: String, CodingKey {
        case teams, league, round, wins, winner, games
        case roundName = "round_name"
        case bestOf = "best_of"
        case isOver = "is_over"
    }
}

struct PostseasonSeriesGame: Codable, Hashable {
    let gameId: Int             // balldontlie game id
    let gameNumber: Int
    let status: String
    let label: String           // "AL Wild Card · Game 2"
    let ifNecessary: Bool
    let seriesStatus: String?   // "BOS leads 1-0" — after this game if final, entering it otherwise
    let line: String?           // what the app shows under the game
    let canClinch: Bool
    let eliminationGame: Bool

    enum CodingKeys: String, CodingKey {
        case status, label, line
        case gameId = "game_id"
        case gameNumber = "game_number"
        case ifNecessary = "if_necessary"
        case seriesStatus = "series_status"
        case canClinch = "can_clinch"
        case eliminationGame = "elimination_game"
    }
}
