//
//  Scores.swift
//  BaseballStats
//
//  Codable models for MLB Stats API responses consumed by the
//  Scores tab. Two payload shapes are decoded:
//
//    1. `ScheduleResponse` — list of games for one date, with team
//       info, current score, linescore, and decisions hydrated in.
//    2. `BoxScoreResponse` — per-team batting + pitching lineups
//       for one game.
//
//  Field names mirror the API exactly so default Codable decoding
//  works without a CodingKeys map. Anything we don't need is
//  omitted; the decoder ignores unknown keys by default.
//

import Foundation

// MARK: - Schedule

struct ScheduleResponse: Codable {
    let dates: [ScheduleDate]
}

struct ScheduleDate: Codable {
    let date: String
    let games: [Game]
}

struct Game: Codable, Identifiable, Hashable {
    let gamePk: Int
    let gameDate: String              // ISO-8601 UTC, e.g. "2026-05-15T19:05:00Z"
    let status: GameStatus
    let teams: GameTeams
    let venue: Venue?
    let linescore: Linescore?
    let decisions: Decisions?

    var id: Int { gamePk }

    /// Bucketed game phase so the UI can branch on intent rather
    /// than the raw API state strings.
    enum Phase {
        case preview        // scheduled / not yet started
        case live           // in progress
        case final          // ended
        case other          // postponed / canceled / suspended
    }

    var phase: Phase {
        switch status.abstractGameState {
        case "Preview": return .preview
        case "Live":    return .live
        case "Final":   return .final
        default:        return .other
        }
    }

    /// `gameDate` parsed via `Date.ISO8601FormatStyle`. nil if the
    /// API ever ships an unexpected shape — caller falls back to
    /// the raw string.
    var startDate: Date? {
        try? Date(gameDate, strategy: .iso8601)
    }
}

struct GameStatus: Codable, Hashable {
    let abstractGameState: String     // "Preview", "Live", "Final"
    let detailedState: String         // "Scheduled", "In Progress", "Final", "Postponed", …
    let statusCode: String?
    let codedGameState: String?
}

struct GameTeams: Codable, Hashable {
    let away: GameTeam
    let home: GameTeam
}

struct GameTeam: Codable, Hashable {
    let team: TeamInfo
    let score: Int?
    let leagueRecord: TeamRecord?
    let isWinner: Bool?
    let probablePitcher: PlayerInfo?
}

struct TeamInfo: Codable, Hashable {
    let id: Int
    let name: String
    let abbreviation: String?

    /// Direct MLB logo URL — `midfield.mlbstatic.com` keys off the
    /// Stats API team id, so no Lahman code mapping is needed here.
    var logoURL: URL? {
        URL(string: "https://midfield.mlbstatic.com/v1/team/\(id)/spots/120")
    }
}

struct TeamRecord: Codable, Hashable {
    let wins: Int?
    let losses: Int?
    let pct: String?
}

struct Venue: Codable, Hashable {
    let id: Int?
    let name: String?
}

// MARK: - Linescore

struct Linescore: Codable, Hashable {
    let currentInning: Int?
    let currentInningOrdinal: String?  // "9th", "Top 7th", …
    let inningState: String?           // "Top", "Bottom", "Middle", "End"
    let innings: [Inning]?
    let teams: LinescoreTeamsTotals?
    let scheduledInnings: Int?
    let isTopInning: Bool?
    let balls: Int?
    let strikes: Int?
    let outs: Int?
}

struct Inning: Codable, Hashable {
    let num: Int
    let home: InningTotals?
    let away: InningTotals?
}

struct InningTotals: Codable, Hashable {
    let runs: Int?
    let hits: Int?
    let errors: Int?
    let leftOnBase: Int?
}

struct LinescoreTeamsTotals: Codable, Hashable {
    let home: InningTotals?
    let away: InningTotals?
}

// MARK: - Decisions

struct Decisions: Codable, Hashable {
    let winner: PlayerInfo?
    let loser: PlayerInfo?
    let save: PlayerInfo?
}

struct PlayerInfo: Codable, Hashable {
    let id: Int
    let fullName: String
}

// MARK: - Box score

struct BoxScoreResponse: Codable {
    let teams: BoxScoreTeams
}

struct BoxScoreTeams: Codable {
    let away: BoxScoreTeam
    let home: BoxScoreTeam
}

struct BoxScoreTeam: Codable {
    let team: TeamInfo
    /// Keyed by "ID{playerId}" — the box-score endpoint serializes
    /// players as a dict keyed by id-prefixed string, not an array.
    let players: [String: BoxPlayer]
    /// Batting order — list of player ids in lineup order. Pinch
    /// hitters / DH appear as additional ids past the starting nine.
    let batters: [Int]
    /// Pitching order — first id is the starter; later ids are the
    /// reliever appearances in order.
    let pitchers: [Int]
}

struct BoxPlayer: Codable {
    let person: PlayerInfo
    let position: BoxPosition?
    /// This-game stats. Both sides may be nil for a player who
    /// didn't appear (e.g. position player listed as a pitcher).
    let stats: BoxStats?
    /// Season-to-date stats — used to surface AVG / ERA next to
    /// the box-score line.
    let seasonStats: BoxStats?
    /// "Williams, P" style — same string the MLB.com box score
    /// surfaces, nil if the API didn't ship it.
    let stats_battingOrder: String?

    enum CodingKeys: String, CodingKey {
        case person, position, stats, seasonStats
        case stats_battingOrder = "battingOrder"
    }
}

struct BoxPosition: Codable {
    let abbreviation: String?
}

struct BoxStats: Codable {
    let batting: BoxBatting?
    let pitching: BoxPitching?
}

struct BoxBatting: Codable, Hashable {
    let atBats: Int?
    let runs: Int?
    let hits: Int?
    let doubles: Int?
    let triples: Int?
    let homeRuns: Int?
    let rbi: Int?
    let baseOnBalls: Int?
    let strikeOuts: Int?
    /// "AVG" — comes through as a String like ".301" / ".000" /
    /// "---" (the latter for players with 0 PA), so we keep it raw.
    let avg: String?
}

struct BoxPitching: Codable, Hashable {
    let inningsPitched: String?   // "5.2" — string per MLB convention
    let hits: Int?
    let runs: Int?
    let earnedRuns: Int?
    let baseOnBalls: Int?
    let strikeOuts: Int?
    let homeRuns: Int?
    let era: String?              // "2.41" — string from the API
}

// MARK: - Live feed (/api/v1.1/game/{pk}/feed/live)

/// Full-game live snapshot — `LiveGameCard` polls this every 30s to
/// drive the in-progress card and the live BoxScoreView. Decodes
/// only the `liveData` subtree; the response also contains
/// `gameData` (static metadata) which we skip.
struct LiveFeedResponse: Codable {
    let liveData: LiveData
}

struct LiveData: Codable {
    let linescore: LiveLinescore?
    let plays: LivePlays?
    /// Same shape as the standalone /boxscore endpoint nested one
    /// level deeper. Live mode uses this so the batting + pitching
    /// tables on BoxScoreView refresh in sync with the rest of the
    /// live state without a second round trip.
    let boxscore: LiveBoxscoreEnvelope?
}

struct LiveBoxscoreEnvelope: Codable {
    let teams: BoxScoreTeams
}

struct LiveLinescore: Codable {
    let currentInning: Int?
    let currentInningOrdinal: String?       // "9th", etc.
    let inningHalf: String?                  // "Top" / "Bottom"
    let inningState: String?                 // "Top" / "Bottom" / "Middle" / "End"
    let isTopInning: Bool?
    let balls: Int?
    let strikes: Int?
    let outs: Int?
    /// Offense block — current batter + base runners. The bases
    /// themselves are `PlayerInfo?` per base; non-nil means a
    /// runner is on that base.
    let offense: LiveOffense?
    /// Defense block — current pitcher.
    let defense: LiveDefense?
    let innings: [Inning]?
    let teams: LinescoreTeamsTotals?
    let scheduledInnings: Int?
}

struct LiveOffense: Codable {
    let batter: PlayerInfo?
    let onDeck: PlayerInfo?
    let inHole: PlayerInfo?
    let first: PlayerInfo?
    let second: PlayerInfo?
    let third: PlayerInfo?
}

struct LiveDefense: Codable {
    let pitcher: PlayerInfo?
    let catcher: PlayerInfo?
}

struct LivePlays: Codable {
    let currentPlay: LivePlay?
}

struct LivePlay: Codable {
    let result: LivePlayResult?
    let about: LivePlayAbout?
    let matchup: LivePlayMatchup?
    let count: LivePlayCount?
    /// Per-pitch / per-event log for this PA. We surface the last
    /// item's description as a one-line "last play" string when
    /// `result.description` is empty (mid-PA states like a single
    /// pitch before the plate appearance ends).
    let playEvents: [LivePlayEvent]?
}

struct LivePlayResult: Codable {
    let description: String?
    let event: String?
}

struct LivePlayAbout: Codable {
    let halfInning: String?
    let inning: Int?
}

struct LivePlayMatchup: Codable {
    let batter: PlayerInfo?
    let pitcher: PlayerInfo?
}

struct LivePlayCount: Codable {
    let balls: Int?
    let strikes: Int?
    let outs: Int?
}

struct LivePlayEvent: Codable {
    let details: LivePlayEventDetails?
    let isPitch: Bool?
}

struct LivePlayEventDetails: Codable {
    let description: String?
    let event: String?
}
