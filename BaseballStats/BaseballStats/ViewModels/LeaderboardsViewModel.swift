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

    /// Leaderboard mode — picks which slice of history the rankings
    /// come from. Drives the segmented control at the top of the view
    /// and the `mode` query param on the backend.
    enum Mode: String, CaseIterable, Identifiable {
        case season   = "season"     // single-year (default)
        case allTime  = "all_time"   // top single seasons across all years
        case career   = "career"     // aggregated career totals
        var id: String { rawValue }
        var label: String {
            switch self {
            case .season:  return "Season"
            case .allTime: return "All-Time"
            case .career:  return "Career"
            }
        }
        /// True when the year picker should be visible — only the
        /// single-year mode needs a year. All-time / career take their
        /// scope from "everything on record."
        var usesYear: Bool { self == .season }
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

    /// Stat keys mostly double as their own user-facing labels — the
    /// backend's leaderboard catalog uses these exact strings, so the
    /// API wire value and the picker text match by default. Stats
    /// where the API key isn't a comfortable display label get a
    /// `displayName(_:)` override below; "SO/9" → "K/9" is the only
    /// one today (the backend key matches the BR career-table column
    /// header, but readers think of the stat as K/9).
    ///
    /// WAR leads both lists; the rest follow popularity order.
    /// Note: HLD ("holds") isn't currently tracked in pitcher_seasons,
    /// so it's deliberately absent from the pitching list.
    static let battingStats:  [String] = [
        "WAR", "HR", "AVG", "RBI", "OPS", "H", "R", "SB", "BB",
        "OBP", "SLG", "2B", "3B", "SO", "PA", "AB",
    ]
    static let pitchingStats: [String] = [
        "WAR", "ERA", "SO", "W", "WHIP", "SV", "IP",
        "SO/9", "H", "BB", "HR", "CG", "SHO",
    ]

    /// User-facing label for a stat. Passes through for stats where
    /// the API key reads fine on its own; falls into the override map
    /// only for the cases where it doesn't. Used by the stat picker,
    /// closed-menu label, and empty-state copy.
    static func displayName(_ stat: String) -> String {
        statDisplayOverrides[stat] ?? stat
    }
    private static let statDisplayOverrides: [String: String] = [
        "SO/9": "K/9",
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

    /// Inclusive bounds for the year-range slider. Floor is 1871 —
    /// the first season carried by the Lahman archive, so a range
    /// like 1871–1900 will return real (if sparse) deadball-era
    /// leaders. The single-year toolbar picker still floors at 1900
    /// for season-mode usability; the slider's wider floor only
    /// matters for All-Time / Career mode where deeper history is
    /// the whole point.
    static var yearRangeBounds: ClosedRange<Int> {
        1871...currentYear
    }

    // MARK: - Selection

    @Published var playerKind: PlayerKind       = .batter
    @Published var selectedStat: String         = LeaderboardsViewModel.defaultBattingStat
    @Published var selectedYear: Int            = LeaderboardsViewModel.currentYear
    @Published var selectedLeague: LeagueFilter = .all
    @Published var selectedTeam: TeamFilter     = LeaderboardsViewModel.allTeams
    @Published var selectedMode: Mode           = .season
    /// Year-range floor for the All-Time / Career range slider. Hidden
    /// (and ignored) in Season mode, where the single-year picker is
    /// already maximally specific. Persists across mode switches so a
    /// "1990–1999" window the user set up in All-Time mode carries
    /// straight into Career when they flip the segment.
    ///
    /// `selected*` is the *live* slider-bound value — updates on every
    /// drag tick and drives the chip's "YYYY – YYYY" readout.
    /// `committed*` lags 500ms behind and is what the fetch actually
    /// uses. Splitting the two lets the slider feel responsive while
    /// the API isn't hit on every intermediate value.
    @Published var selectedYearFrom: Int = LeaderboardsViewModel.yearRangeBounds.lowerBound
    @Published var selectedYearTo:   Int = LeaderboardsViewModel.yearRangeBounds.upperBound
    @Published var committedYearFrom: Int = LeaderboardsViewModel.yearRangeBounds.lowerBound
    @Published var committedYearTo:   Int = LeaderboardsViewModel.yearRangeBounds.upperBound

    // MARK: - State

    @Published var entries: [LeaderboardEntry] = []
    @Published var isLoading = false
    @Published var isLoadingMore = false
    @Published var error: String?
    /// True while a year-range debounce is in flight (the user is
    /// actively dragging the slider, or just released within the
    /// last 500ms). The view hides the error UI while true so a
    /// failed intermediate fetch doesn't flash "Couldn't Load
    /// Leaderboard" mid-drag.
    @Published var isRangeAdjusting: Bool = false
    /// How many rows the next request will ask the backend for. Bumps
    /// up by `pageStep` on each "Show more" tap, capped at `maxLimit`.
    @Published var displayedLimit: Int = LeaderboardsViewModel.initialLimit
    /// Increments once per debounced commit. The view's FetchKey
    /// watches this single token instead of every filter field, so
    /// rapid sequential changes (e.g. user tapping through five
    /// stats in 300ms) coalesce into a single fetch after the
    /// debounce window settles. Without this, every onChange would
    /// flip a different FetchKey field and re-fire `.task(id:)`
    /// — letting a slow earlier response overwrite a fast later one.
    @Published private(set) var commitToken: Int = 0

    private let api: APIClient
    /// Shared in-flight debounce timer. One slot used by both filter
    /// changes (200ms) and year-range drags (500ms) — whichever
    /// happened most recently dictates the wait. A follow-up change
    /// cancels and replaces it, so the API stays quiet until the
    /// user genuinely pauses.
    private var debounceTask: Task<Void, Never>?
    /// Debounce intervals — chosen so menu-tap changes feel snappy
    /// (200ms) while slider drags get a generous settle window
    /// (500ms) before the network call fires.
    private static let filterDebounceNanos: UInt64 = 200_000_000
    private static let rangeDebounceNanos:  UInt64 = 500_000_000

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

    /// Called from the view's `.onChange` on stat / kind / league /
    /// team / mode / year picker changes. Clears any stale error
    /// synchronously so "Couldn't Load Leaderboard" doesn't linger
    /// past the user's tap, resets pagination, and schedules a
    /// 200ms commit so a rapid tap sequence collapses into a
    /// single fetch.
    func filterDidChange() {
        error = nil
        resetPagination()
        scheduleCommit(after: Self.filterDebounceNanos)
    }

    /// Called from the view's `.onChange` on either slider handle.
    /// Clears the stale error, resets pagination, marks the range as
    /// "actively adjusting" so the view can suppress the error UI,
    /// and schedules a 500ms commit. Replacement-cancel semantics
    /// match `filterDidChange()` — the latest call wins.
    func rangeDidChange() {
        error = nil
        resetPagination()
        isRangeAdjusting = true
        scheduleCommit(after: Self.rangeDebounceNanos)
    }

    /// Shared debounce — cancel any running task, start a fresh
    /// timer, fire `commit()` when it elapses. Cancellation throws
    /// out of `Task.sleep`; the catch swallows it because a newer
    /// scheduled commit is on the way.
    private func scheduleCommit(after nanos: UInt64) {
        debounceTask?.cancel()
        debounceTask = Task { @MainActor [weak self] in
            do {
                try await Task.sleep(nanoseconds: nanos)
                try Task.checkCancellation()
                self?.commit()
            } catch {
                // Cancelled by a fresher change. The replacement
                // task carries the next commit.
            }
        }
    }

    /// Single shared "fire the fetch" point. Promotes the live slider
    /// values to committed (idempotent when the slider didn't move),
    /// clears the range-adjusting flag, and bumps `commitToken` —
    /// the only field the view's FetchKey actually watches. The
    /// `&+` overflow-safe increment is paranoia: even at one commit
    /// per second the Int wraparound is centuries away, but explicit
    /// is fine.
    private func commit() {
        error = nil
        committedYearFrom = selectedYearFrom
        committedYearTo   = selectedYearTo
        isRangeAdjusting  = false
        commitToken &+= 1
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
            // Year range only flows to the backend in All-Time /
            // Career — Season mode is already a single-year filter.
            // Also skip when the range is the full bounds, so the
            // URL doesn't carry redundant params on the default view.
            // Reads committed values (post-debounce), not the live
            // slider values, so the fetch matches what the FetchKey
            // observed when it triggered this load.
            let sendRange = !selectedMode.usesYear
                && (committedYearFrom != Self.yearRangeBounds.lowerBound
                    || committedYearTo   != Self.yearRangeBounds.upperBound)
            let response = try await api.getLeaderboard(
                stat:       selectedStat,
                year:       selectedMode.usesYear ? selectedYear : nil,
                playerType: playerKind.rawValue,
                mode:       selectedMode.rawValue,
                league:     selectedLeague.apiValue,
                team:       selectedTeam.apiCode,
                yearFrom:   sendRange ? committedYearFrom : nil,
                yearTo:     sendRange ? committedYearTo   : nil,
                limit:      displayedLimit
            )
            entries = Self.dedupe(response?.leaders ?? [])
        } catch {
            self.error = error.localizedDescription
            entries = []
        }
        isLoading = false
        isLoadingMore = false
    }

    /// Drop any (player_id, year) collisions from the server response,
    /// keeping the first occurrence (i.e. the better-ranked row).
    /// Belt-and-suspenders backstop for the backend SQL-level CTE
    /// dedupe — if a stale or partially-deployed backend ever ships
    /// duplicate entries, the iOS list still renders without the
    /// "ForEach with duplicate IDs" SwiftUI crash, and the user sees
    /// each player-season exactly once. Order is preserved from the
    /// API response (already sorted by stat).
    static func dedupe(_ leaders: [LeaderboardEntry]) -> [LeaderboardEntry] {
        var seen = Set<String>()
        var out: [LeaderboardEntry] = []
        out.reserveCapacity(leaders.count)
        for entry in leaders {
            // (player_id, year) — career rows have year=nil, which
            // collapses to the same key per player (correct, since
            // career mode emits one row per player by construction).
            let key = "\(entry.player.player_id)-\(entry.year ?? -1)"
            if seen.insert(key).inserted {
                out.append(entry)
            }
        }
        return out
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
        // Show more is an explicit single tap — fire the next fetch
        // immediately rather than routing through the debouncer.
        commitToken &+= 1
    }
}
