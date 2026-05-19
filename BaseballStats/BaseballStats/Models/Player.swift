//
//  Player.swift
//  BaseballStats
//
//  Codable models matching the FastAPI backend at
//  https://baseball-stats-app-production-0ef1.up.railway.app
//
//  Property names mirror the JSON keys exactly (BA, HR, OPS_plus, ...) so
//  Codable's default keyed decoding works without a CodingKeys map. This is
//  a deliberate departure from Swift naming conventions — baseball stats
//  have canonical capitalization and matching it keeps the API contract
//  readable.
//

import Foundation

// MARK: - Search

/// One row from `GET /players/search?name=...` — a player matched by name.
/// Carries the same bio block as the player-stats responses so a search
/// result can render a rich row (headshot, position, debut year) without a
/// follow-up call.
struct PlayerSearchResult: Codable, Identifiable, Hashable {
    let player_id: Int
    let name: String
    let bbref_id: String?
    let mlb_debut: Int?
    let mlb_last_season: Int?
    /// Team from the player's most recent season row in the DB. The raw
    /// stored value — may be a Lahman code, a bref code, or a city display
    /// name depending on which loader wrote the row. Use `teamCode` for
    /// reliable client-side lookups.
    let currentTeam: String?
    /// Lahman-style 2–3 char team code, normalized server-side from
    /// `currentTeam` + league. nil when the team can't be resolved (very
    /// old or obscure franchises).
    let teamCode: String?

    // Bio fields (flattened into the search result by the backend)
    let position: String?
    let bats: String?
    /// Renamed from `throws` (a Swift reserved keyword). The JSON key is
    /// still `"throws"` — see CodingKeys below.
    let throwingArm: String?
    let height: Int?            // inches
    let weight: Int?            // pounds
    let birth_year: Int?
    let birth_month: Int?
    let birth_day: Int?
    let birth_city: String?
    let birth_state: String?
    let birth_country: String?
    let debut: String?          // ISO date "YYYY-MM-DD"
    let final_game: String?
    let birthdate: String?      // ISO date, derived
    let headshot_url: String?
    let is_hof: Bool?
    let hof_year: Int?
    /// Set by the leaderboard endpoint per `player_type` so the
    /// profile screen can pick the right default role tab without
    /// waiting for the four parallel current/career fetches. Nil for
    /// search-result rows (where role is still inferred client-side
    /// from the fetched career thresholds).
    let is_pitcher: Bool?

    var id: Int { player_id }

    enum CodingKeys: String, CodingKey {
        case player_id, name, bbref_id, mlb_debut, mlb_last_season
        case currentTeam = "current_team"
        case teamCode = "team_code"
        case position, bats
        case throwingArm = "throws"
        case height, weight
        case birth_year, birth_month, birth_day
        case birth_city, birth_state, birth_country
        case debut, final_game, birthdate, headshot_url, is_hof, hof_year
        case is_pitcher
    }
}

extension PlayerSearchResult {
    /// Higher-resolution headshot URL. The backend emits the URL with
    /// width=213 (the MLB Stats API default for thumbnail use). Swap
    /// in `w_640` for the player profile header — the source image
    /// pipeline supports any width and the larger size renders much
    /// sharper at retina densities. Returns nil for missing/empty
    /// fields and falls back to the raw URL if the pattern isn't
    /// found (defensive for future backend URL format changes).
    var largeHeadshotURL: URL? {
        guard let raw = headshot_url, !raw.isEmpty else { return nil }
        let upgraded = raw.replacingOccurrences(of: "w_213", with: "w_640")
        return URL(string: upgraded)
    }
}

// MARK: - Bio

/// The `bio` block returned inside player current/career stats responses.
/// Same fields as the search-result bio, minus the player_id/name/bbref_id
/// header that lives on the parent response.
struct PlayerBio: Codable, Hashable {
    let position: String?
    let bats: String?
    /// Renamed from `throws` (a Swift reserved keyword). The JSON key is
    /// still `"throws"` — see CodingKeys below.
    let throwingArm: String?
    let height: Int?
    let weight: Int?
    let birth_year: Int?
    let birth_month: Int?
    let birth_day: Int?
    let birth_city: String?
    let birth_state: String?
    let birth_country: String?
    let debut: String?
    let final_game: String?
    let birthdate: String?
    let headshot_url: String?
    let is_hof: Bool?
    let hof_year: Int?

    enum CodingKeys: String, CodingKey {
        case position, bats
        case throwingArm = "throws"
        case height, weight
        case birth_year, birth_month, birth_day
        case birth_city, birth_state, birth_country
        case debut, final_game, birthdate, headshot_url, is_hof, hof_year
    }
}

// MARK: - Current stats (batting)

/// Response from `GET /players/{id}/stats/current`.
struct PlayerCurrentStats: Codable {
    let player_id: Int
    let season: Int
    let bio: PlayerBio?
    let standard: BattingStandardStats?
    let advanced: BattingAdvancedStats?
    /// ISO-8601 UTC stamp of the nightly batch run that wrote the
    /// row. Used by the live-stats overlay to decide whether a
    /// recent game is already counted (game started before this
    /// stamp) or still needs folding in (started after). nil for
    /// historical seasons that pre-date the column.
    let stats_last_updated: String?
}

struct BattingStandardStats: Codable {
    let name: String?
    let team: String?
    let G: Int?
    let PA: Int?
    let AB: Int?
    let R: Int?
    let H: Int?
    let doubles: Int?
    let triples: Int?
    let HR: Int?
    let RBI: Int?
    let BB: Int?
    let IBB: Int?
    let HBP: Int?
    let SO: Int?
    let SB: Int?
    let CS: Int?
    let SH: Int?
    let SF: Int?
    let GIDP: Int?
    let BA: Double?
    let OBP: Double?
    let SLG: Double?
    let OPS: Double?
    let BABIP: Double?
    let ISO: Double?
    let BB_pct: Double?
    let K_pct: Double?
    let wOBA: Double?
}

struct BattingAdvancedStats: Codable {
    let WAR: Double?
    let WAR_off: Double?
    let WAR_def: Double?
    let WAA: Double?
    let OPS_plus: Double?
    let runs_above_avg: Double?
    let runs_above_rep: Double?
}

// MARK: - Career stats (batting)

/// Response from `GET /players/{id}/stats/career`.
struct PlayerCareerStats: Codable {
    let player_id: Int
    let name: String?
    let bio: PlayerBio?
    let seasons: [CareerSeason]?
    let career_totals: CareerTotals?
}

/// One row from the `seasons` array — a single year of a player's career.
/// Mirrors the `player_seasons` table; every stat field is optional because
/// older Lahman seasons are missing many of the modern columns.
struct CareerSeason: Codable, Identifiable, Hashable {
    let year: Int?
    let team: String?
    let league: String?

    let G: Int?
    let PA: Int?
    let AB: Int?
    let R: Int?
    let H: Int?
    let doubles: Int?
    let triples: Int?
    let HR: Int?
    let RBI: Int?
    let BB: Int?
    let IBB: Int?
    let HBP: Int?
    let SO: Int?
    let SB: Int?
    let CS: Int?
    let SH: Int?
    let SF: Int?
    let GIDP: Int?
    /// Total bases — H + 2·2B + 3·3B + 4·HR. Stored on player_seasons
    /// (backfilled by init_db for historical rows); older API
    /// responses that predate the column return nil and the view
    /// falls back to deriving the value on-device.
    let TB: Int?

    let BA: Double?
    let OBP: Double?
    let SLG: Double?
    let OPS: Double?
    let BABIP: Double?
    let ISO: Double?
    let BB_pct: Double?
    let K_pct: Double?
    let wOBA: Double?

    let WAR: Double?
    let WAR_off: Double?
    let WAR_def: Double?
    let WAA: Double?
    let OPS_plus: Double?
    let runs_above_avg: Double?
    let runs_above_rep: Double?

    /// Per-stat league/majors leadership flags computed by the backend.
    /// Keyed by user-facing stat label ("AVG", "HR", "2B", "WAR" …).
    /// Value is "league" if the player led their league that year and
    /// "majors" if they led both leagues combined. Empty dict for
    /// seasons without league info or seasons with no leading stats.
    let leaders: [String: String]?

    /// Stable per-row id for SwiftUI lists. Year is unique within a player's
    /// career in the season-totals view, so it works as the identifier.
    var id: Int { year ?? 0 }
}

/// `career_totals` block. `G/H/HR/RBI` only appear when at least one season
/// has counting stats — the backend omits them otherwise. Slash-line rates
/// and OPS+ are computed off the summed counting stats (PA-weighted for
/// OPS+) so they match bref's career-page numbers.
struct CareerTotals: Codable {
    let seasons: Int?
    let WAR: Double?
    let WAR_off: Double?
    let WAR_def: Double?
    let G: Int?
    let H: Int?
    let HR: Int?
    let RBI: Int?
    let AVG: Double?
    let OBP: Double?
    let SLG: Double?
    let OPS: Double?
    let OPS_plus: Double?
}

// MARK: - Pitching: current

/// Response from `GET /players/{id}/pitching/current`.
struct PitcherCurrentStats: Codable {
    let player_id: Int
    let season: Int
    let bio: PlayerBio?
    let standard: PitcherStandardStats?
    let advanced: PitcherAdvancedStats?
    /// See `PlayerCurrentStats.stats_last_updated` — same semantics
    /// for the pitcher side.
    let stats_last_updated: String?
}

struct PitcherStandardStats: Codable {
    let name: String?
    let team: String?
    let G: Int?
    let GS: Int?
    let CG: Int?
    let SHO: Int?
    let GF: Int?
    let W: Int?
    let L: Int?
    let SV: Int?
    let IP: Double?
    let BFP: Int?
    let H: Int?
    let R: Int?
    let ER: Int?
    let HR: Int?
    let BB: Int?
    let IBB: Int?
    let SO: Int?
    let HBP: Int?
    let WP: Int?
    let BK: Int?
    let SH: Int?
    let SF: Int?
    let GIDP: Int?
    let ERA: Double?
    let WHIP: Double?
    /// FIP lives in `standard` per the backend response shape, even though
    /// it's an advanced metric — keeping the struct aligned with the JSON.
    let FIP: Double?
    let BAOpp: Double?
    let BABIP: Double?
    let K_per9: Double?
    let BB_per9: Double?
    let HR_per9: Double?
}

struct PitcherAdvancedStats: Codable {
    let WAR: Double?
    let WAA: Double?
    let ERA_plus: Double?
    let runs_above_avg: Double?
    let runs_above_rep: Double?
}

// MARK: - Pitching: career

/// Response from `GET /players/{id}/pitching/career`.
struct PitcherCareerStats: Codable {
    let player_id: Int
    let name: String?
    let bio: PlayerBio?
    let seasons: [PitcherCareerSeason]?
    let career_totals: PitcherCareerTotals?
}

/// One row from the pitching `seasons` array — mirrors `pitcher_seasons`.
/// All fields optional because older Lahman seasons are missing many
/// modern columns (FIP, K/9, etc.).
struct PitcherCareerSeason: Codable, Identifiable, Hashable {
    let year: Int?
    let team: String?
    let league: String?

    let G: Int?
    let GS: Int?
    let CG: Int?
    let SHO: Int?
    let GF: Int?
    let W: Int?
    let L: Int?
    let SV: Int?
    let IP: Double?
    let BFP: Int?
    let H: Int?
    let R: Int?
    let ER: Int?
    let HR: Int?
    let BB: Int?
    let IBB: Int?
    let SO: Int?
    let HBP: Int?
    let WP: Int?
    let BK: Int?
    let SH: Int?
    let SF: Int?
    let GIDP: Int?
    let ERA: Double?
    let WHIP: Double?
    let FIP: Double?
    let BAOpp: Double?
    let BABIP: Double?
    let K_per9: Double?
    let BB_per9: Double?
    let HR_per9: Double?

    let WAR: Double?
    let WAR_def: Double?
    let WAA: Double?
    let ERA_plus: Double?
    let runs_above_avg: Double?
    let runs_above_rep: Double?

    /// Per-stat league/majors leadership flags. Same shape and semantics
    /// as `CareerSeason.leaders` — keyed by user-facing label
    /// ("ERA", "SO", "W", "WHIP", "SV", "IP", "WAR").
    let leaders: [String: String]?

    var id: Int { year ?? 0 }
}

/// Pitching `career_totals` block. ERA / WHIP / ERA+ come off the
/// summed counting stats (IP-weighted for ERA+) so they line up with
/// bref's career-page numbers.
struct PitcherCareerTotals: Codable {
    let seasons: Int?
    let WAR: Double?
    let IP: Double?
    let SO: Int?
    let BB: Int?
    let W: Int?
    let L: Int?
    let ERA: Double?
    let WHIP: Double?
    let ERA_plus: Double?
}

// MARK: - Game logs

/// Response from `GET /players/{id}/gamelogs/batting?season=...`.
/// Backend returns games in reverse-chronological order plus a rolling
/// `splits` block (last 5 / 10 / 15 / 30 / season).
struct GameLogResponse: Codable {
    let player_id: Int
    let season: Int
    let games: [GameLog]?
    let splits: GameLogSplits?
}

/// A single game from the gamelog. Fields cover both batting and pitching
/// schemas — only the relevant subset is populated depending on which
/// gamelog endpoint produced this row.
struct GameLog: Codable, Identifiable, Hashable {
    let game_id: String?
    let game_date: String?      // ISO date "YYYY-MM-DD"
    let season: Int?
    let opponent: String?
    let home_away: String?      // "H" | "A"
    let result: String?         // batting: W/L/T  •  pitching: W/L/S/H/BS/ND
    let team_score: Int?
    let opp_score: Int?

    // Batting
    let AB: Int?
    let R: Int?
    let H: Int?
    let doubles: Int?
    let triples: Int?
    let HR: Int?
    let RBI: Int?
    let BB: Int?
    let IBB: Int?
    let SO: Int?
    let SB: Int?
    let CS: Int?
    let HBP: Int?
    let SF: Int?
    let LOB: Int?

    // Pitching
    let IP: Double?
    let ER: Int?
    let WP: Int?
    let pitches: Int?
    let strikes: Int?

    var id: String { game_id ?? UUID().uuidString }
}

/// Rolling-window aggregates returned alongside the game list.
struct GameLogSplits: Codable {
    let last_5: GameLogWindow?
    let last_10: GameLogWindow?
    let last_15: GameLogWindow?
    let last_30: GameLogWindow?
    let season: GameLogWindow?
}

/// One aggregate window. Same combined batting+pitching shape as `GameLog`
/// — only the relevant subset is non-nil.
struct GameLogWindow: Codable {
    let G: Int?

    // Batting
    let AB: Int?
    let R: Int?
    let H: Int?
    let HR: Int?
    let RBI: Int?
    let BB: Int?
    let SO: Int?
    let SB: Int?
    let BA: Double?
    let OBP: Double?
    let SLG: Double?
    let OPS: Double?

    // Pitching
    let IP: Double?
    let ER: Int?
    let ERA: Double?
    let WHIP: Double?
    let K_per9: Double?
    let BB_per9: Double?
}

// Standings models moved to Models/Standings.swift.
