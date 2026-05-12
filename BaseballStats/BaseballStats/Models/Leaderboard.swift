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

    var id: Int { player.player_id }
}
