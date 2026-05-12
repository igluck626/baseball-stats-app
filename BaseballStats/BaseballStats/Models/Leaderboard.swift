//
//  Leaderboard.swift
//  BaseballStats
//
//  Codable models for `GET /leaderboards?stat=&year=&player_type=`. The
//  backend nests a full PlayerSearchResult under each row so the leaderboard
//  list can render the same chrome as the search results and tap-to-profile
//  navigation reuses the existing PlayerProfileView entry point.
//

import Foundation

/// `GET /leaderboards` envelope. `year` is null for the all-time and
/// career modes; `mode` echoes whichever mode the request resolved to
/// ("season", "all_time", "career") so callers don't have to track it
/// separately to know how to render the row.
struct LeaderboardResponse: Codable {
    let stat: String
    /// nil for non-season modes — the request didn't carry a year.
    let year: Int?
    /// Optional for back-compat with older backend responses that
    /// pre-date the mode split.
    let mode: String?
    let player_type: String
    let leaders: [LeaderboardEntry]
}

/// One ranked row in a leaderboard. `value` is the raw stat value the
/// list is sorted by (HR count, ERA, WAR, …). `nil` only when the stat
/// column itself is null on the season row, which the backend filters
/// out — but Codable still tolerates a missing/null payload.
///
/// `year` is set for season + all-time rows (which year this single
/// season belongs to). Nil for career rows since they aggregate across
/// every year the player appeared.
struct LeaderboardEntry: Codable, Identifiable, Hashable {
    let rank: Int
    let value: Double?
    let year: Int?
    let player: PlayerSearchResult

    /// Composite id — player_id alone collides in All-Time mode when
    /// the same player appears with multiple seasons in the top 25
    /// (Bonds 2001 + Bonds 2004 for HR, McGwire 1998 + 1999 + 1997,
    /// …). Pairing with `year` keeps every row in the response
    /// distinct; the trailing rank covers the edge case where two
    /// players genuinely tie on year + value (shouldn't happen with
    /// the SQL-level dedupe but cheap to belt-and-brace here).
    var id: String { "\(player.player_id)-\(year ?? -1)-\(rank)" }
}
