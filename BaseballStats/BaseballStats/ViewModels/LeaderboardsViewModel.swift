//
//  LeaderboardsViewModel.swift
//  BaseballStats
//
//  Drives the Leaderboards tab. One fetch per (player type, stat, year)
//  selection. Selection state lives here so the view can re-bind the
//  picker without losing scroll/focus context.
//

import Combine
import Foundation

@MainActor
final class LeaderboardsViewModel: ObservableObject {

    // MARK: - Domain types

    enum PlayerKind: String, CaseIterable, Identifiable {
        case batter, pitcher
        var id: String { rawValue }
        var label: String {
            switch self {
            case .batter:  return "Batting"
            case .pitcher: return "Pitching"
            }
        }
    }

    /// League filter — "All" combines both leagues; AL/NL pass through to
    /// the backend's `league` query param. Standalone enum so the view's
    /// segmented control can iterate it directly.
    enum LeagueFilter: String, CaseIterable, Identifiable {
        case all = "All"
        case al  = "AL"
        case nl  = "NL"
        var id: String { rawValue }
        var label: String { rawValue }
        /// API param value — nil for "All", which the backend treats as
        /// "no filter".
        var apiValue: String? {
            switch self {
            case .all: return nil
            case .al:  return "AL"
            case .nl:  return "NL"
            }
        }
    }

    /// One row in the team-picker menu. The 30 modern franchises plus an
    /// "All Teams" sentinel. `apiCode` is the Lahman code the backend
    /// matches against (e.g. "NYA", "LAN", "MIA"); the backend expands
    /// it to historical variants on its side, so iOS only ships the one
    /// canonical code per franchise.
    struct TeamFilter: Identifiable, Hashable {
        let displayName: String
        let apiCode: String?    // nil for "All Teams"
        let league: String?     // nil for "All Teams"; "AL" / "NL" otherwise
        var id: String { apiCode ?? "__all__" }
    }

    /// All-Teams sentinel the picker defaults to.
    static let allTeams = TeamFilter(displayName: "All Teams", apiCode: nil, league: nil)

    /// 30 active MLB franchises in alphabetical order. Each carries the
    /// canonical Lahman code (which the backend treats as a key into
    /// historical-variant lookup) plus the league it currently plays in
    /// (so the picker can be filtered when AL or NL is selected).
    static let teams: [TeamFilter] = [
        .init(displayName: "Arizona Diamondbacks",  apiCode: "ARI", league: "NL"),
        .init(displayName: "Atlanta Braves",        apiCode: "ATL", league: "NL"),
        .init(displayName: "Baltimore Orioles",     apiCode: "BAL", league: "AL"),
        .init(displayName: "Boston Red Sox",        apiCode: "BOS", league: "AL"),
        .init(displayName: "Chicago Cubs",          apiCode: "CHN", league: "NL"),
        .init(displayName: "Chicago White Sox",     apiCode: "CHA", league: "AL"),
        .init(displayName: "Cincinnati Reds",       apiCode: "CIN", league: "NL"),
        .init(displayName: "Cleveland Guardians",   apiCode: "CLE", league: "AL"),
        .init(displayName: "Colorado Rockies",      apiCode: "COL", league: "NL"),
        .init(displayName: "Detroit Tigers",        apiCode: "DET", league: "AL"),
        .init(displayName: "Houston Astros",        apiCode: "HOU", league: "AL"),
        .init(displayName: "Kansas City Royals",    apiCode: "KCA", league: "AL"),
        .init(displayName: "Los Angeles Angels",    apiCode: "LAA", league: "AL"),
        .init(displayName: "Los Angeles Dodgers",   apiCode: "LAN", league: "NL"),
        .init(displayName: "Miami Marlins",         apiCode: "MIA", league: "NL"),
        .init(displayName: "Milwaukee Brewers",     apiCode: "MIL", league: "NL"),
        .init(displayName: "Minnesota Twins",       apiCode: "MIN", league: "AL"),
        .init(displayName: "New York Mets",         apiCode: "NYN", league: "NL"),
        .init(displayName: "New York Yankees",      apiCode: "NYA", league: "AL"),
        .init(displayName: "Athletics",             apiCode: "OAK", league: "AL"),
        .init(displayName: "Philadelphia Phillies", apiCode: "PHI", league: "NL"),
        .init(displayName: "Pittsburgh Pirates",    apiCode: "PIT", league: "NL"),
        .init(displayName: "San Diego Padres",      apiCode: "SDN", league: "NL"),
        .init(displayName: "San Francisco Giants",  apiCode: "SFN", league: "NL"),
        .init(displayName: "Seattle Mariners",      apiCode: "SEA", league: "AL"),
        .init(displayName: "St. Louis Cardinals",   apiCode: "SLN", league: "NL"),
        .init(displayName: "Tampa Bay Rays",        apiCode: "TBA", league: "AL"),
        .init(displayName: "Texas Rangers",         apiCode: "TEX", league: "AL"),
        .init(displayName: "Toronto Blue Jays",     apiCode: "TOR", league: "AL"),
        .init(displayName: "Washington Nationals",  apiCode: "WAS", league: "NL"),
    ]

    /// Stat keys are the user-facing labels and the API query values both —
    /// the backend's leaderboard catalog uses these exact strings. Order
    /// here drives the picker order: most-popular first.
    /// Note: HLD ("holds") isn't currently tracked in pitcher_seasons,
    /// so it's deliberately absent from the pitching list.
    static let battingStats:  [String] = [
        "HR", "AVG", "RBI", "OPS", "H", "R", "SB", "BB",
        "OBP", "SLG", "WAR", "2B", "3B", "SO", "PA", "AB",
    ]
    static let pitchingStats: [String] = [
        "ERA", "SO", "W", "WHIP", "SV", "IP",
        "H", "BB", "HR", "WAR", "CG", "SHO",
    ]

    static let defaultBattingStat  = "HR"
    static let defaultPitchingStat = "ERA"

    static var currentYear: Int {
        Calendar.current.component(.year, from: Date())
    }

    // MARK: - Selection

    @Published var playerKind: PlayerKind       = .batter
    @Published var selectedStat: String         = LeaderboardsViewModel.defaultBattingStat
    @Published var selectedYear: Int            = LeaderboardsViewModel.currentYear
    @Published var selectedLeague: LeagueFilter = .all
    @Published var selectedTeam: TeamFilter     = LeaderboardsViewModel.allTeams

    // MARK: - State

    @Published var entries: [LeaderboardEntry] = []
    @Published var isLoading = false
    @Published var error: String?

    private let api: APIClient

    init(api: APIClient = .shared) {
        self.api = api
    }

    /// Returns the stat list appropriate to the current `playerKind`.
    var availableStats: [String] {
        playerKind == .batter ? Self.battingStats : Self.pitchingStats
    }

    /// Reset the stat to the default for the new player kind. Called when
    /// the user toggles Batting/Pitching — keeping a batting-only stat
    /// selected when switching to Pitching would 400 the next request.
    func resetStatForCurrentKind() {
        selectedStat = (playerKind == .batter)
            ? Self.defaultBattingStat
            : Self.defaultPitchingStat
    }

    /// Whether this stat label is part of the current player kind's catalog.
    /// Used to detect cross-kind selections that need a reset.
    func statBelongsToCurrentKind() -> Bool {
        availableStats.contains(selectedStat)
    }

    /// Teams visible in the picker, narrowed by the league filter so a
    /// user can't pick "Yankees" while the league is set to NL (which
    /// would always return zero rows). The "All Teams" sentinel is
    /// always first.
    var availableTeams: [TeamFilter] {
        var visible = [Self.allTeams]
        let leagueCode = selectedLeague.apiValue
        if let leagueCode {
            visible += Self.teams.filter { $0.league == leagueCode }
        } else {
            visible += Self.teams
        }
        return visible
    }

    /// If the selected team is in a league that no longer matches the
    /// active league filter (e.g. user picked Yankees, then flipped to
    /// NL), drop back to "All Teams" so the next fetch isn't guaranteed
    /// empty.
    func resetTeamIfHidden() {
        let leagueCode = selectedLeague.apiValue
        if let leagueCode,
           let teamLeague = selectedTeam.league,
           teamLeague != leagueCode {
            selectedTeam = Self.allTeams
        }
    }

    // MARK: - Loading

    func load() async {
        isLoading = true
        error = nil
        do {
            let response = try await api.getLeaderboard(
                stat:       selectedStat,
                year:       selectedYear,
                playerType: playerKind.rawValue,
                league:     selectedLeague.apiValue,
                team:       selectedTeam.apiCode
            )
            entries = response?.leaders ?? []
        } catch {
            self.error = error.localizedDescription
            entries = []
        }
        isLoading = false
    }
}
