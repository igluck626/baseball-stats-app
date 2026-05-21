//
//  BDLScores.swift
//  BaseballStats
//
//  Codable models for the BallDontLie MLB API. Mirrors the JSON
//  shape under `data: [...]` for each endpoint we consume; the
//  envelope itself lives in BallDontLieClient.
//
//  All decoders use `convertFromSnakeCase`, so the Swift field
//  names below are camelCase versions of BDL's snake_case JSON
//  (`home_team_data` → `homeTeamData`, `inning_scores` →
//  `inningScores`, etc.).
//

import Foundation

// MARK: - Game

struct BDLGame: Codable, Identifiable, Hashable {
    let id: Int
    let homeTeam: BDLTeam
    let awayTeam: BDLTeam
    let homeTeamData: BDLTeamData?
    let awayTeamData: BDLTeamData?
    let date: String                 // ISO-8601 UTC, e.g. "2026-05-19T00:40:00.000Z"
    let status: String               // "STATUS_FINAL", "STATUS_IN_PROGRESS", "STATUS_SCHEDULED", …
    let venue: String?
    let period: Int?                 // current inning (live games)
    let displayClock: String?
    let scoringSummary: [BDLScoringPlay]?
    let season: Int
    let seasonType: String           // "regular", "postseason", "spring_training"
    let postseason: Bool?
    let homeTeamName: String?
    let awayTeamName: String?

    /// `date` parsed via ISO-8601. nil if the string ever ships in
    /// an unexpected format — caller falls back to the raw string.
    var startDate: Date? {
        try? Date(date, strategy: .iso8601)
    }
}

struct BDLTeam: Codable, Hashable {
    let id: Int
    let slug: String?
    let abbreviation: String
    let displayName: String
    let shortDisplayName: String?
    let name: String
    let location: String
    let league: String?              // "American" / "National"
    let division: String?            // "East" / "Central" / "West"
}

struct BDLTeamData: Codable, Hashable {
    let hits: Int?
    let runs: Int?
    let errors: Int?
    let inningScores: [Int]?
}

struct BDLScoringPlay: Codable, Hashable {
    let play: String?
    let inning: String?              // "top" / "bottom"
    let period: String?              // "1st" / "2nd" / …
    let awayScore: Int?
    let homeScore: Int?
}

// MARK: - Player + per-game stats

/// Stripped-down player ref used in nested BDL responses
/// (`BDLPlayerStat.player`, `BDLPlay.batter`, etc.).
struct BDLPlayer: Codable, Hashable {
    let id: Int
    let firstName: String?
    let lastName: String?
    let fullName: String
    let position: String?
    let jersey: String?
    let team: BDLTeam?
}

/// One row from `/stats?game_ids[]={id}`. Each player who appeared
/// in a game gets one row; batting fields are populated for
/// position players, pitching for pitchers, both for two-way
/// appearances (Ohtani as DH + starter on the same line).
///
/// BDL prefixes the conflicting field names: hits→pitchingHits,
/// runs→pitchingRuns when the value is for the pitching side.
/// Swift reflects that distinction here.
struct BDLPlayerStat: Codable, Hashable {
    let player: BDLPlayer
    let gameId: Int
    let teamName: String?

    // Batting line
    let atBats: Int?
    let runs: Int?
    let hits: Int?
    let rbi: Int?
    let hr: Int?
    let bb: Int?
    let k: Int?
    let avg: Double?
    let obp: Double?
    let slg: Double?
    let doubles: Int?
    let triples: Int?
    let stolenBases: Int?
    let caughtStealing: Int?
    let hitByPitch: Int?
    let sacFlies: Int?
    let plateAppearances: Int?

    // Pitching line
    let ip: Double?
    let pHits: Int?
    let pRuns: Int?
    let er: Int?
    let pBb: Int?
    let pK: Int?
    let pHr: Int?
    let era: Double?
    let wins: Int?
    let losses: Int?
    let saves: Int?
    let holds: Int?
    let pitchCount: Int?
    let gamesStarted: Int?
}

// MARK: - Lineups

struct BDLGameLineup: Codable, Identifiable, Hashable {
    let id: Int
    let gameId: Int
    let player: BDLPlayer
    let team: BDLTeam
    let battingOrder: Int?
    let position: String?
    let isProbablePitcher: Bool?
}

// MARK: - Plays

struct BDLPlay: Codable, Hashable {
    let gameId: Int
    let order: Int                   // sequential ordering across the game
    let type: String?                // "Start Batter/Pitcher" / "Strike Looking" / …
    let text: String?                // human-readable description
    let homeScore: Int
    let awayScore: Int
    let inning: Int
    let inningType: String?          // "Top" / "Bottom"
    let scoringPlay: Bool
    let scoreValue: Int?
    let outs: Int?
    let balls: Int?
    let strikes: Int?
    let batterId: Int?
    let pitcherId: Int?
    let pitchType: String?
    let pitchVelocity: Double?
    let trajectory: String?
}

// MARK: - Season stats

/// One row from `/season_stats?player_ids[]=X&season=Y`. Different
/// field shape from the per-game `/stats` endpoint — season stats
/// use a `batting_*` / `pitching_*` prefix on every key. The
/// box-score lineup placeholder path consumes these to surface a
/// player's season AVG / OPS / ERA before they've had their first
/// PA / inning of the day.
struct BDLSeasonStat: Codable, Hashable {
    let player: BDLPlayer
    let teamName: String?
    let season: Int?

    let battingAvg: Double?
    let battingObp: Double?
    let battingSlg: Double?
    let battingOps: Double?

    let pitchingEra: Double?
    let pitchingW:   Int?
    let pitchingL:   Int?
    let pitchingSv:  Int?
}

// MARK: - Name utilities

/// Last-name segment of a full name, preserving any trailing
/// baseball suffix ("Jr.", "Sr.", "II", "III", "IV"). Returns
/// "Tatis Jr." for "Fernando Tatis Jr." and "Trout" for "Mike
/// Trout". Used by the box-score's "F. Tatis Jr." abbreviation
/// and the final-game HR summary's "Tatis Jr. (23)" segment.
func lastNameWithSuffix(_ full: String) -> String {
    let parts = full.split(separator: " ").map(String.init)
    guard parts.count >= 2 else { return full }
    let suffixes: Set<String> = ["Jr.", "Sr.", "Jr", "Sr", "II", "III", "IV"]
    var working = parts
    var trailingSuffix: String? = nil
    if let lastToken = working.last, suffixes.contains(lastToken) {
        trailingSuffix = lastToken
        working.removeLast()
    }
    guard let last = working.last else { return full }
    if let trailingSuffix { return "\(last) \(trailingSuffix)" }
    return last
}

// MARK: - Standings

/// One row from `/standings?season=N`. Only the fields the iOS
/// scores cards consume are pulled — the full payload also carries
/// streak / last-ten / home-road / clincher fields that the backend
/// uses for the Standings tab; those are intentionally omitted here
/// because the iOS scores path only needs the W-L pair.
struct BDLStandingsEntry: Codable, Hashable {
    let team: BDLTeam
    let wins: Int
    let losses: Int
}

// MARK: - Plate appearances

struct BDLPlateAppearance: Codable, Hashable {
    let batterId: Int
    let pitcherId: Int
    let inning: Int
    let halfInning: String           // "top" / "bottom"
    let paNumber: Int
    let outs: Int?
    let batterSide: String?
    let pitcherHand: String?
    let result: String?              // "Strikeout" / "Single" / …
    let isBallInPlayOut: Bool?
    let runnerOnFirst: Bool?
    let runnerOnSecond: Bool?
    let runnerOnThird: Bool?
}
