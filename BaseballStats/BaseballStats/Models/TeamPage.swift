//
//  TeamPage.swift
//  BaseballStats
//
//  The team page's two server reads, for any club: `/teams/{bdl_id}/schedule`
//  and `/teams/{bdl_id}/roster`. Both used to be balldontlie calls made from
//  the app — eleven a load for the hero's games, one for the roster, one for
//  the injury list, and a backend request per player to resolve and fill each
//  roster row. The server makes them now, once per team for every user.
//

import Foundation

/// `GET /teams/{bdl_id}/schedule`. Decoded with snake_case conversion, because
/// `games` are balldontlie's own rows and `BDLGame` reads them that way.
struct TeamScheduleResponse: Decodable {
    let teamId: Int
    let season: Int
    let abbreviation: String?
    /// The season's games in start order, spring training included and
    /// placeholder postseason games ("UNK") already dropped.
    let games: [BDLGame]
    let seasonResult: SeasonResult
    /// Next season's first regular-season game, once balldontlie lists it.
    /// Only looked up once this season is over.
    let nextSeasonFirstGame: BDLGame?
}

/// How a team's season stands — `team_page.season_result` on the server.
struct SeasonResult: Decodable, Hashable {
    enum State: String, Decodable {
        case inProgress = "in_progress"
        case alive
        case missedPostseason = "missed_postseason"
        case eliminated
        case champion
        /// The regular season is over and the bracket can't say more.
        case seasonOver = "season_over"
        case unknown

        init(from decoder: Decoder) throws {
            let raw = try decoder.singleValueContainer().decode(String.self)
            self = State(rawValue: raw) ?? .unknown
        }
    }

    let state: State
    let roundName: String?
    let wins: Int?
    let losses: Int?
    /// balldontlie abbreviation of the series opponent.
    let opponent: String?

    /// True once the team has nothing left to play this season.
    var isOver: Bool {
        switch state {
        case .missedPostseason, .eliminated, .champion, .seasonOver: return true
        case .inProgress, .alive, .unknown:                          return false
        }
    }
}

/// `GET /teams/{bdl_id}/roster`. Decoded with the default decoder: the players
/// carry our own `PlayerSearchResult` payload, whose keys are already the
/// wire's.
struct TeamRosterResponse: Decodable {
    let team_id: Int
    let season: Int
    let team_code: String?
    let players: [TeamRosterEntry]
    let injuries: [TeamInjuryEntry]
}

struct TeamRosterEntry: Decodable {
    let bdl_id: Int
    let name: String?
    let position: String?
    let jersey: String?
    let is_pitcher: Bool
    /// Our player record, to push his profile. nil when the balldontlie id
    /// doesn't map to one of ours.
    let player: PlayerSearchResult?
    /// "team" — his line for this team; "all_teams" — his whole season, where
    /// the line for this team can't be trusted (see `RosterPlayer.scope`).
    let scope: String?
    let stats: TeamRosterStats?
}

struct TeamRosterStats: Decodable, Hashable {
    let g: Int?
    let avg: Double?
    let hr: Int?
    let rbi: Int?
    let ops: Double?
    let era: Double?
    let w: Int?
    let so: Int?
    let whip: Double?
    let war: Double?
}

struct TeamInjuryEntry: Decodable {
    let bdl_id: Int?
    let name: String?
    let position: String?
    let status: String?
    let type: String?
    let return_date: String?
    let player: PlayerSearchResult?
}
