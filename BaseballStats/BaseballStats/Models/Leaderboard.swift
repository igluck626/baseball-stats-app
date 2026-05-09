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

/// `GET /leaderboards` envelope.
struct LeaderboardResponse: Codable {
    let stat: String
    let year: Int
    let player_type: String
    let leaders: [LeaderboardEntry]
}

/// One ranked row in a leaderboard. `value` is the raw stat value the
/// list is sorted by (HR count, ERA, WAR, …). `nil` only when the stat
/// column itself is null on the season row, which the backend filters
/// out — but Codable still tolerates a missing/null payload.
struct LeaderboardEntry: Codable, Identifiable, Hashable {
    let rank: Int
    let value: Double?
    let player: PlayerSearchResult

    var id: Int { player.player_id }
}
