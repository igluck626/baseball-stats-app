//
//  BDLTeamIds.swift
//  BaseballStats
//
//  Static mappings between BallDontLie team ids, Lahman team codes,
//  and (via `TeamNames.swift`'s `mlbStatsApiTeamId`) the MLB Stats
//  API team ids.
//
//  Mirrors the backend's `_BDL_TEAM_ID_MAP` / `_BDL_TO_LAHMAN_TEAM_MAP`
//  in `data_service.py` — keep the two in sync. BDL ids are stable
//  across seasons (one id per franchise), so this constant is the
//  authoritative source for in-app translation.
//

import Foundation

/// Lahman code (`"NYA"`, `"CHN"`, `"ATH"`, …) → BallDontLie team id.
/// Same 30 entries as the backend's dict.
let lahmanToBDLTeamId: [String: Int] = [
    "ARI":  1, "ATL":  2, "BAL":  3, "BOS":  4, "CHN":  5,
    "CHA":  6, "CIN":  7, "CLE":  8, "COL":  9, "DET": 10,
    "HOU": 11, "KCA": 12, "LAA": 13, "LAN": 14, "MIA": 15,
    "MIL": 16, "MIN": 17, "NYN": 18, "NYA": 19, "ATH": 20,
    "PHI": 21, "PIT": 22, "SDN": 23, "SFN": 24, "SEA": 25,
    "SLN": 26, "TBA": 27, "TEX": 28, "TOR": 29, "WAS": 30,
]

/// Inverse direction — BDL ids ship on every game / stat / play /
/// PA payload, and the iOS layer needs to resolve them to Lahman
/// codes whenever it crosses into the rest of the app's id space
/// (team logos, team_seasons lookups, etc.).
let bdlToLahmanTeamId: [Int: String] = Dictionary(
    uniqueKeysWithValues: lahmanToBDLTeamId.map { ($1, $0) }
)

/// Convenience: BDL id → MLB Stats API numeric team id. Goes
/// through Lahman as the intermediate hop so the existing
/// `mlbStatsApiTeamId` map in `TeamNames.swift` stays the single
/// source of truth for the MLBAM-side mapping.
func mlbTeamId(forBDLId bdlId: Int) -> Int? {
    guard let lahman = bdlToLahmanTeamId[bdlId] else { return nil }
    return lahmanTeamIdToMLBId(lahman)
}

/// Lahman code → MLB Stats API team id.
///
/// Reads `TeamNames.swift`'s id table through `mlbTeamId(for:)`. Until
/// 2026-08-15 it took a stranger route: it called `teamLogoURL(for:)` and
/// parsed the numeric id back out of the CDN URL's path, to avoid
/// duplicating the table. That made a *data* lookup depend on an *image*
/// URL builder — so removing team logos would have silently broken the
/// favourite team, live games, and the whole Scores/Box Score BDL bridge.
/// Repointed on its own, ahead of any imagery removal, for exactly that
/// reason. `mlbTeamId(for:)` takes `String?` and applies the same
/// case-folding, so the behaviour is unchanged.
func lahmanTeamIdToMLBId(_ lahman: String) -> Int? {
    mlbTeamId(for: lahman)
}
