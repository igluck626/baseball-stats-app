//
//  Awards.swift
//  BaseballStats
//
//  Codable models for the player-awards + award-voting endpoints.
//
//    GET /players/{id}/awards   →  PlayerAwardsResponse
//    GET /awards/voting?award=&year=&league=  →  AwardVotingResponse
//
//  Field names mirror the JSON keys exactly so Codable's default
//  keyed decoding works without a CodingKeys map.
//

import Foundation

// MARK: - /players/{id}/awards

/// Enriched awards payload. The two derived blocks (`headline_awards`,
/// `career_by_year`) are what the iOS career table + header awards row
/// actually consume. The raw arrays are kept for debugging / future
/// surfaces that want unaggregated data.
struct PlayerAwardsResponse: Codable {
    let player_id: Int
    /// Counts of canonical headline trophies the player has won, plus
    /// All-Star appearances. Zero-count entries are omitted server-
    /// side, so the dict can be iterated directly. Keys are: "MVP",
    /// "CY Young", "ROY", "Gold Glove", "Silver Slugger",
    /// "World Series MVP", "All-Star".
    let headline_awards: [String: Int]?
    /// One entry per season the player appeared in any award source.
    let career_by_year: [PlayerAwardYear]?
    /// Raw rows — same shape as the original endpoint returned.
    let awards: [PlayerAwardRow]?
    let allstar: [PlayerAllstarRow]?
    let award_shares: [PlayerAwardShareRow]?
}

/// One year's award activity for a player. `votes` carries MVP / CY /
/// ROY voting rank + points totals when the player received votes
/// that year; empty when the player wasn't on any ballot.
struct PlayerAwardYear: Codable, Hashable {
    let year: Int
    let awards: [PlayerAwardRow]
    let allstar: Bool
    let votes: [PlayerAwardShareRow]
}

struct PlayerAwardRow: Codable, Hashable {
    let award_name: String?
    let league: String?
    let notes: String?
    let tie: String?
    /// Year is optional because the raw arrays don't always carry it
    /// — the per-year block already groups by year so the row's own
    /// year is redundant inside that context.
    let year: Int?
}

struct PlayerAllstarRow: Codable, Hashable {
    let year: Int?
    let game_num: Int?
    let team: String?
    let league: String?
    let GP: Int?
    let starting_pos: Int?
}

/// One vote-share row — backs both the per-year `votes` array on the
/// awards response and the entries on the voting leaderboard.
struct PlayerAwardShareRow: Codable, Hashable {
    let award_id: String?      // "MVP" / "CY Young" / "ROY"
    let league: String?
    let rank: Int?
    let points_won: Double?
    let points_max: Double?
    let votes_first: Int?
    let year: Int?             // present on the player-shares array; nil inside per-year `votes`
}

// MARK: - /awards/voting

struct AwardVotingResponse: Codable {
    let award_id: String
    let year: Int
    let league: String
    let entries: [AwardVotingEntry]
}

struct AwardVotingEntry: Codable, Identifiable, Hashable {
    let rank: Int
    let points_won: Double?
    let points_max: Double?
    let votes_first: Int?
    let player: PlayerSearchResult
    /// That player's batting + pitching stat block for the season the
    /// vote concerns. Either side can be nil; two-way players get
    /// both populated.
    let season_stats: SeasonStatsBlock?

    /// Composite id so SwiftUI's ForEach can disambiguate rows in
    /// the rare two-player tie case (same rank, different player).
    var id: String { "\(rank)-\(player.player_id)" }
}

struct SeasonStatsBlock: Codable, Hashable {
    let batting: SeasonBatting?
    let pitching: SeasonPitching?

    struct SeasonBatting: Codable, Hashable {
        let AVG: Double?
        let HR:  Int?
        let RBI: Int?
        let WAR: Double?
        let PA:  Int?
        /// OPS+ — stored as Float server-side, decoded as Double here
        /// and rounded for display. BR convention is integer ("132").
        let OPSplus: Double?
    }

    struct SeasonPitching: Codable, Hashable {
        let ERA: Double?
        let W:   Int?
        let L:   Int?
        let SO:  Int?
        let WAR: Double?
        let IP:  Double?
        /// ERA+ — same Float→Double→rounded-int treatment as OPSplus.
        let ERAplus: Double?
    }
}
