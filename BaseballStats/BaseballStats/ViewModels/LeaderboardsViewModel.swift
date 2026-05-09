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
    /// the backend's leaderboard catalog uses these exact strings. WAR
    /// leads both lists; the rest follow popularity order.
    /// Note: HLD ("holds") isn't currently tracked in pitcher_seasons,
    /// so it's deliberately absent from the pitching list.
    static let battingStats:  [String] = [
        "WAR", "HR", "AVG", "RBI", "OPS", "H", "R", "SB", "BB",
        "OBP", "SLG", "2B", "3B", "SO", "PA", "AB",
    ]
    static let pitchingStats: [String] = [
        "WAR", "ERA", "SO", "W", "WHIP", "SV", "IP",
        "H", "BB", "HR", "CG", "SHO",
    ]

    /// Default stat for each kind. Both kinds open on WAR — the
    /// headline modern stat for hitters and pitchers alike. These
    /// are also what the UI snaps back to whenever the user toggles
    /// between Batting and Pitching — no preserving the previous
    /// selection across kinds.
    static let defaultBattingStat  = "WAR"
    static let defaultPitchingStat = "WAR"

    /// Pagination — start at 25, grow in 25-row batches up to 100.
    static let initialLimit: Int = 25
    static let pageStep:     Int = 25
    static let maxLimit:     Int = 100

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
    @Published var isLoadingMore = false
    @Published var error: String?
    /// How many rows the next request will ask the backend for. Bumps
    /// up by `pageStep` on each "Show more" tap, capped at `maxLimit`.
    @Published var displayedLimit: Int = LeaderboardsViewModel.initialLimit

    private let api: APIClient

    init(api: APIClient = .shared) {
        self.api = api
    }

    /// Returns the stat list appropriate to the current `playerKind`.
    var availableStats: [String] {
        playerKind == .batter ? Self.battingStats : Self.pitchingStats
    }

    /// Reset the stat to the default for the new player kind. Called
    /// unconditionally when the user toggles Batting/Pitching — even
    /// when the previous stat exists in both catalogs (e.g. WAR), the
    /// product wants the toggle to behave as a "fresh start" landing
    /// on each kind's headline default rather than preserving the
    /// prior selection across the boundary.
    func resetStatForCurrentKind() {
        selectedStat = (playerKind == .batter)
            ? Self.defaultBattingStat
            : Self.defaultPitchingStat
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

    /// Whether the "Show more" button should be visible at the bottom
    /// of the list. Two conditions, both required:
    ///   • We haven't hit the 100-row cap yet.
    ///   • The last request returned a full batch — if the API gave us
    ///     fewer rows than we asked for (e.g. team-filtered board with
    ///     only 12 qualifying players), the dataset is exhausted.
    var canLoadMore: Bool {
        displayedLimit < Self.maxLimit && entries.count >= displayedLimit
    }

    /// Fetch with the current selection state. The view drives this via
    /// `.task(id: fetchKey)` so that any change to a selection field
    /// (kind / stat / year / league / team / displayedLimit) coalesces
    /// into exactly one fetch — no chained-onChange races, no double-
    /// fetches even when a user gesture mutates two fields at once
    /// (e.g. the kind toggle resetting the stat and the page limit).
    ///
    /// Distinguishes "first page" from "load more" by comparing the
    /// limit to `initialLimit`, so the view can show a row spinner for
    /// pagination instead of the big center spinner.
    func load() async {
        let loadingMore = displayedLimit > Self.initialLimit
        if loadingMore {
            isLoadingMore = true
        } else {
            isLoading = true
        }
        error = nil
        do {
            let response = try await api.getLeaderboard(
                stat:       selectedStat,
                year:       selectedYear,
                playerType: playerKind.rawValue,
                league:     selectedLeague.apiValue,
                team:       selectedTeam.apiCode,
                limit:      displayedLimit
            )
            entries = response?.leaders ?? []
        } catch {
            self.error = error.localizedDescription
            entries = []
        }
        isLoading = false
        isLoadingMore = false
    }

    /// Reset the request limit back to the first page. Call before any
    /// filter mutation — otherwise a previously-expanded board (e.g.
    /// limit=100) would pre-fetch a full 100 rows on the next change.
    func resetPagination() {
        displayedLimit = Self.initialLimit
    }

    /// Bump the limit by one page. The actual fetch is triggered by
    /// the view's `.task(id: fetchKey)` reacting to the new limit, so
    /// this method just mutates state — no async work, no possibility
    /// of racing the kind/stat reset path.
    func loadMore() {
        guard canLoadMore, !isLoadingMore else { return }
        let nextLimit = min(displayedLimit + Self.pageStep, Self.maxLimit)
        guard nextLimit > displayedLimit else { return }
        displayedLimit = nextLimit
    }
}
