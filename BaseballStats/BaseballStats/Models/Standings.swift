//
//  Standings.swift
//  BaseballStats
//
//  Codable models for the team-standings and team-history endpoints.
//  Lifted out of Player.swift to keep that file focused on player-level
//  shapes.
//

import Foundation

// MARK: - Standings (single-year snapshot)

/// Response from `GET /teams/standings?year=...`.
struct StandingsResponse: Codable {
    let year: Int
    /// ISO-8601 + "Z" — when the latest row in this year's standings was
    /// last touched by the nightly update. nil for older Lahman-only
    /// years where rows were never re-saved.
    let last_updated: String?
    let standings: [TeamStanding]?
}

/// One team's record for a single season. Maps to the `team_seasons`
/// table. Used for both standings rows and team-history rows (same
/// underlying TeamSeason shape on the backend).
struct TeamStanding: Codable, Identifiable, Hashable {
    let year: Int?
    let team_id: String?
    let franch_id: String?
    let team_name: String?
    let league: String?
    /// Single-letter division code from the Lahman archive: "E", "C",
    /// "W". Nil for pre-divisional years.
    let division: String?
    let rank: Int?
    let G: Int?
    let W: Int?
    let L: Int?
    let win_pct: Double?
    let runs_scored: Int?
    let runs_allowed: Int?
    let HR: Int?
    let ERA: Double?
    let attendance: Int?
    let park_name: String?
    let last_updated: String?

    /// Composite id (year + team_id) — a franchise appears across many
    /// years and many franchises share a year, so neither alone is unique.
    var id: String { "\(year ?? 0)-\(team_id ?? "?")" }
}

// MARK: - Team history (year-by-year)

/// Response from `GET /teams/{team_id}/history`. The backend resolves
/// team_id → franch_id so the history follows relocations (MON → WSN
/// shows up as a single continuous history).
struct TeamHistoryResponse: Codable {
    let team_id: String
    /// One TeamStanding row per season in franchise history. Same shape
    /// as the standings rows, sorted chronologically by the backend.
    let history: [TeamStanding]?
}
