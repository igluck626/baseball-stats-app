//
//  TeamNames.swift
//  BaseballStats
//
//  Shared team-code → full-name lookup. Used by the search row, the
//  player profile header, and the season-card subtitle so all surfaces
//  resolve "NYA" / "LAD" / etc. the same way.
//
//  The DB stores three families of team codes depending on which loader
//  wrote the row:
//    • Lahman teamID — "NYA", "BOS", "SLN", "CHA"
//    • Baseball-Reference Tm — "NYY", "STL", "CHW"
//    • _TEAM_DISPLAY city-only — "New York", "Boston" (rare; from the
//      nightly bwar path before team_code was added server-side)
//  This dictionary covers the first two. City-only values fall through
//  to the "doesn't look like a code" branch in `teamFullName(for:)`.
//

import Foundation

/// Maps team abbreviations (Lahman + Baseball-Reference variants) to
/// their full display names. Top-level so any view in the module can
/// reach it without an import path.
let mlbTeamFullName: [String: String] = [
    // Lahman codes (modern era)
    "ARI": "Arizona Diamondbacks",
    "ATL": "Atlanta Braves",
    "BAL": "Baltimore Orioles",
    "BOS": "Boston Red Sox",
    "CHA": "Chicago White Sox",
    "CHN": "Chicago Cubs",
    "CIN": "Cincinnati Reds",
    "CLE": "Cleveland Guardians",
    "COL": "Colorado Rockies",
    "DET": "Detroit Tigers",
    "HOU": "Houston Astros",
    "KCA": "Kansas City Royals",
    "LAA": "Los Angeles Angels",
    "ANA": "Los Angeles Angels",
    "LAN": "Los Angeles Dodgers",
    "MIA": "Miami Marlins",
    "FLO": "Miami Marlins",
    "MIL": "Milwaukee Brewers",
    "MIN": "Minnesota Twins",
    "NYA": "New York Yankees",
    "NYN": "New York Mets",
    // 2025 rebrand — the team dropped the city qualifier and goes by
    // just "Athletics". Same Lahman code for historical continuity.
    "OAK": "Athletics",
    "ATH": "Athletics",
    "PHI": "Philadelphia Phillies",
    "PIT": "Pittsburgh Pirates",
    "SDN": "San Diego Padres",
    "SEA": "Seattle Mariners",
    "SFN": "San Francisco Giants",
    "SLN": "St. Louis Cardinals",
    "TBA": "Tampa Bay Rays",
    "TEX": "Texas Rangers",
    "TOR": "Toronto Blue Jays",
    "WAS": "Washington Nationals",
    "MON": "Montreal Expos",
    "BRO": "Brooklyn Dodgers",
    "WS1": "Washington Senators",

    // Baseball-Reference codes that differ from Lahman
    "NYY": "New York Yankees",
    "NYM": "New York Mets",
    "CHW": "Chicago White Sox",
    "CHC": "Chicago Cubs",
    "KCR": "Kansas City Royals",
    "LAD": "Los Angeles Dodgers",
    "SDP": "San Diego Padres",
    "SFG": "San Francisco Giants",
    "STL": "St. Louis Cardinals",
    "TBR": "Tampa Bay Rays",
    "WSN": "Washington Nationals",
]

/// Resolves a team code to a full display name.
/// - Returns the mapped full name if the code is in the dictionary.
/// - Returns nil for unrecognized short tokens (≤3 chars, all
///   uppercase/numeric) so callers can hide meaningless values like
///   "LS3" rather than rendering them.
/// - Returns the input unchanged when it doesn't look like a code at
///   all (e.g. a city display name like "New York" from a legacy row).
func teamFullName(for code: String) -> String? {
    if let resolved = mlbTeamFullName[code] {
        return resolved
    }
    if code.count <= 3,
       code.allSatisfy({ $0.isUppercase || $0.isNumber }) {
        return nil
    }
    return code
}

/// Maps Lahman codes (the backend's storage form) to the fan-friendly
/// 3-letter abbreviations users recognize on broadcasts and box
/// scores — "SFN" → "SFG", "NYA" → "NYY", "CHA" → "CHW", etc. Codes
/// that are already in the readable form (ATL, BOS, ARI, etc.) and
/// bbref-style codes already in the readable form (NYY, LAD, SFG)
/// fall through unchanged. Used in tight-layout contexts like the
/// leaderboard row where the full name would truncate.
private let mlbTeamAbbreviation: [String: String] = [
    "NYA": "NYY",   // Yankees
    "NYN": "NYM",   // Mets
    "CHA": "CHW",   // White Sox
    "CHN": "CHC",   // Cubs
    "KCA": "KCR",   // Royals
    "LAN": "LAD",   // Dodgers
    "SDN": "SDP",   // Padres
    "SFN": "SFG",   // Giants
    "SLN": "STL",   // Cardinals
    "TBA": "TBR",   // Rays
    "WAS": "WSN",   // Nationals
    // Modern rebrands — collapse historical codes to today's shorthand.
    "OAK": "ATH",   // 2025 rebrand: dropped Oakland, just "Athletics"
    "ANA": "LAA",   // 1997-2004 Anaheim era → current LA Angels
    "FLO": "MIA",   // pre-2012 Florida Marlins → Miami
]

/// Compact 3-letter team code suitable for narrow contexts (leaderboard
/// rows, splits tables). Resolves Lahman storage codes to fan-friendly
/// abbreviations; returns the input unchanged for codes that are
/// already in the recognized short form. Returns the input verbatim
/// for unmapped tokens so callers can still render something rather
/// than a blank.
func teamAbbreviation(for code: String) -> String {
    mlbTeamAbbreviation[code] ?? code
}

/// Lahman team code → MLB Stats API numeric team id. The MLB CDN
/// (`midfield.mlbstatic.com`) keys logos off this numeric id, so we
/// invert here the same table the nightly standings refresh uses
/// on the backend.
///
/// Modern-rebrand and bbref-style aliases (OAK → 133 Athletics,
/// ANA → 108 Angels, FLO → 146 Marlins, MON → 120 Nationals) point
/// at the current franchise's id — the logo CDN doesn't keep
/// pre-rebrand SKUs, but the modern logo is the right call anyway.
private let mlbStatsApiTeamId: [String: Int] = [
    // Lahman canonical codes
    "ARI": 109, "ATL": 144, "BAL": 110, "BOS": 111,
    "CHA": 145, "CHN": 112, "CIN": 113, "CLE": 114,
    "COL": 115, "DET": 116, "HOU": 117, "KCA": 118,
    "LAA": 108, "LAN": 119, "MIA": 146, "MIL": 158,
    "MIN": 142, "NYA": 147, "NYN": 121, "ATH": 133,
    "PHI": 143, "PIT": 134, "SDN": 135, "SFN": 137,
    "SEA": 136, "SLN": 138, "TBA": 139, "TEX": 140,
    "TOR": 141, "WAS": 120,

    // bbref-style codes that may slip through from current-season
    // rows the nightly pulled before being normalized to Lahman.
    "NYY": 147, "NYM": 121, "CHW": 145, "CWS": 145,
    "CHC": 112, "KCR": 118, "LAD": 119, "SDP": 135,
    "SFG": 137, "STL": 138, "TBR": 139, "WSN": 120,

    // Modern-rebrand / historical aliases — collapse to the current
    // franchise so we still surface a logo for older standings rows.
    "OAK": 133,   // 2025 rebrand: Oakland → Athletics
    "ANA": 108,   // 1997–2004 Anaheim Angels → LA Angels
    "FLO": 146,   // pre-2012 Florida Marlins → Miami
    "MON": 120,   // Expos → Nationals lineage
]

/// Build the MLB Stats API CDN URL for a team's PNG logo at 120px.
/// Uses `midfield.mlbstatic.com/v1/team/{id}/spots/120` which serves
/// PNG (1.7 KB), so `AsyncImage` can render it natively — the
/// alternate `mlbstatic.com/team-logos/{id}.svg` endpoint also
/// responds 200 but ships SVG, which iOS' AsyncImage doesn't decode.
///
/// Returns nil for empty / unmapped codes; callers (the Standings
/// row's `teamLogo` AsyncImage placeholder) fall back to a tinted
/// rectangle so the row layout stays solid.
func teamLogoURL(for code: String?) -> URL? {
    guard let code, !code.isEmpty,
          let mlbId = mlbStatsApiTeamId[code.uppercased()] else { return nil }
    return URL(string: "https://midfield.mlbstatic.com/v1/team/\(mlbId)/spots/120")
}

/// Lahman team code → MLB Stats API numeric team id. Surfaced as a
/// function rather than re-exposing the dictionary directly so the
/// case-folding rule stays in one place. Used by the live-stats
/// loader to call `/schedule?teamId={id}` for "does this team play
/// today?" lookups without coupling that path to logo URLs.
func mlbTeamId(for code: String?) -> Int? {
    guard let code, !code.isEmpty else { return nil }
    return mlbStatsApiTeamId[code.uppercased()]
}
