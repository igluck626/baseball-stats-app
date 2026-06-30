//
//  Postseason.swift
//  BaseballStats
//
//  Codable models for the playoff-history endpoints:
//    • GET /postseason/available → years that have postseason data, newest first
//    • GET /postseason?year=YYYY → every series that year (both leagues)
//
//  Team codes are RAW Lahman codes (e.g. "LAN", "NYA"); the UI maps them to a
//  conventional abbreviation / color via the shared `teamAbbreviation(for:)`
//  and `TeamColors.color(for:)` helpers. Series arrive sorted alphabetically by
//  `round_code`, NOT in bracket order — the bracket assembler places each
//  series into its position by round code.
//

import Foundation

/// `GET /postseason/available` — `{ "years": [2025, 2024, …, 1884] }`.
struct PostseasonAvailableResponse: Codable, Hashable {
    let years: [Int]
}

/// `GET /postseason?year=YYYY`.
struct PostseasonYearResponse: Codable, Hashable {
    let year: Int
    let series: [PostseasonSeries]
}

/// One postseason series outcome. `wins`/`losses` are from the WINNER's
/// perspective (so `wins` is the winner's game count, `losses` the loser's).
/// `roundCode` is the raw Lahman code (WS, ALDS1, NLWC2, AEDIV, …) used to
/// place the series in a bracket; `round` is a display-ready label.
struct PostseasonSeries: Codable, Hashable, Identifiable {
    let roundCode: String
    let round: String
    let winner: String          // raw Lahman code
    let loser: String           // raw Lahman code
    let winnerLeague: String?   // "AL" / "NL"
    let loserLeague: String?
    let wins: Int?
    let losses: Int?
    let ties: Int?

    enum CodingKeys: String, CodingKey {
        case roundCode = "round_code"
        case round, winner, loser
        case winnerLeague = "winner_league"
        case loserLeague = "loser_league"
        case wins, losses, ties
    }

    /// Unique within a year (a round can repeat across leagues, e.g. ALDS1 /
    /// NLDS1, but the winner/loser pair disambiguates).
    var id: String { "\(roundCode)-\(winner)-\(loser)" }

    /// Winner game count, nil-safe for display.
    var winnerGames: Int { wins ?? 0 }
    /// Loser game count, nil-safe for display.
    var loserGames: Int { losses ?? 0 }
}

/// `GET /postseason/champions` — every World Series result, newest year first.
struct PostseasonChampionsResponse: Codable, Hashable {
    let champions: [WorldSeriesChampion]
}

/// One World Series outcome. `wins`/`losses` are winner-perspective.
struct WorldSeriesChampion: Codable, Hashable, Identifiable {
    let year: Int
    let winner: String          // raw Lahman code (champion)
    let loser: String           // raw Lahman code (runner-up)
    let winnerLeague: String?
    let loserLeague: String?
    let wins: Int?
    let losses: Int?
    let ties: Int?

    enum CodingKeys: String, CodingKey {
        case year, winner, loser
        case winnerLeague = "winner_league"
        case loserLeague = "loser_league"
        case wins, losses, ties
    }

    var id: Int { year }
    var winnerGames: Int { wins ?? 0 }
    var loserGames: Int { losses ?? 0 }
}
