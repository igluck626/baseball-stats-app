//
//  StandingsViewModel.swift
//  BaseballStats
//
//  Drives the Standings tab. One fetch per year selection, then partition
//  the 30 rows into AL/NL × E/C/W buckets sorted by win_pct.
//

import Combine
import Foundation

@MainActor
final class StandingsViewModel: ObservableObject {
    /// AL standings keyed by single-letter division code ("E", "C", "W").
    /// Each bucket is sorted by win_pct desc with rank as a tiebreaker.
    @Published var alStandings: [String: [TeamStanding]] = [:]
    @Published var nlStandings: [String: [TeamStanding]] = [:]
    /// Wildcard race per league — all non-division-leader teams sorted
    /// by win_pct desc. The Standings view highlights the top 3 as
    /// "in" via the rank position; everything below is "out / chasing."
    @Published var alWildcard: [TeamStanding] = []
    @Published var nlWildcard: [TeamStanding] = []
    @Published var selectedYear: Int
    @Published var isLoading = false
    @Published var error: String?
    /// ISO-8601 timestamp from the response — surfaced as "Updated …"
    /// at the bottom of the view.
    @Published var lastUpdated: String?

    /// Per-team W/L bumps for today's finals that the official standings
    /// haven't absorbed yet, keyed by **BDL team id**. Empty for
    /// historical years and when nothing today is unreflected. The row
    /// view folds these into the displayed W-L and flags them with "†".
    @Published var todayAdjustments: [Int: (wDelta: Int, lDelta: Int)] = [:]

    /// Recomputed GB / WCGB strings keyed by Lahman `team_id`, derived
    /// from the today-adjusted records so those columns stay consistent
    /// with the bumped W-L. Empty when there are no adjustments (rows
    /// then keep the backend / leader-derived values).
    @Published var adjustedGB: [String: (gb: String, wcgb: String, pct: Double?, strk: String?, homeW: Int?, homeL: Int?, awayW: Int?, awayL: Int?, runsScored: Int?, runsAllowed: Int?)] = [:]

    private let api: APIClient
    private let bdl: BallDontLieClient
    private let favorites: FavoriteTeamStore

    static var currentYear: Int {
        Calendar.current.component(.year, from: Date())
    }

    init(
        api: APIClient = .shared,
        bdl: BallDontLieClient = .shared,
        favorites: FavoriteTeamStore = .shared,
    ) {
        self.api = api
        self.bdl = bdl
        self.favorites = favorites
        self.selectedYear = Self.currentYear
    }

    /// Lahman team code of the user's favorite team (bridged from the
    /// stored BDL id), or nil when no favorite is set. Drives the
    /// default league tab, the favorite-division-first ordering, and
    /// the row star.
    var favoriteLahmanCode: String? {
        favorites.bdlTeamId.flatMap { bdlToLahmanTeamId[$0] }
    }

    /// The favorite team's league ("AL"/"NL") and division code
    /// ("E"/"C"/"W") for the currently-loaded standings, found by
    /// locating its row in the partitioned buckets. nil when there's
    /// no favorite or the team isn't in this year's standings (e.g. a
    /// historical year predating the franchise). Resolving off the
    /// loaded rows — rather than a static map — keeps it correct for
    /// teams that changed leagues across eras (1990s Brewers).
    var favoriteLeagueDivision: (league: String, division: String)? {
        guard let code = favoriteLahmanCode else { return nil }
        for (div, teams) in alStandings where teams.contains(where: { $0.team_id == code }) {
            return ("AL", div)
        }
        for (div, teams) in nlStandings where teams.contains(where: { $0.team_id == code }) {
            return ("NL", div)
        }
        return nil
    }

    func loadStandings() async {
        isLoading = true
        error = nil
        do {
            let response = try await api.getStandings(year: selectedYear)
            partition(response)
            lastUpdated = response?.last_updated
        } catch {
            self.error = error.localizedDescription
            alStandings = [:]
            nlStandings = [:]
            lastUpdated = nil
        }
        isLoading = false
        // Overlay today's not-yet-official results once the standings
        // (and their `last_updated` cutoff) have landed.
        await loadTodayAdjustments()
    }

    /// Fold today's already-final games into a per-team W/L delta keyed
    /// by BDL team id. Mirrors the Scores tab's ±1 UTC day envelope +
    /// ET-date filter so evening games (which land on "UTC tomorrow")
    /// are still attributed to today. Only the current season is
    /// adjusted; a game is counted only when it finished AFTER the
    /// standings' `last_updated` (approximated by its start time, which
    /// is strictly before its end) so already-absorbed games aren't
    /// double-counted.
    func loadTodayAdjustments() async {
        guard selectedYear == Self.currentYear else {
            todayAdjustments = [:]
            adjustedGB = [:]
            return
        }

        // ±1 day UTC envelope — yesterday / today / tomorrow in UTC —
        // so evening-ET games (which land on "UTC tomorrow") are still
        // fetched. The ET-date filter, `last_updated` cutoff, and
        // winner/loser delta math live in the shared helper.
        var utcCal = Calendar(identifier: .gregorian)
        utcCal.timeZone = TimeZone(identifier: "UTC")!
        let utcFormatter = DateFormatter()
        utcFormatter.calendar = utcCal
        utcFormatter.timeZone = TimeZone(identifier: "UTC")
        utcFormatter.dateFormat = "yyyy-MM-dd"
        let now = Date()
        let dateStrings: [String] = [-1, 0, 1].compactMap { offset in
            utcCal.date(byAdding: .day, value: offset, to: now)
                .map { utcFormatter.string(from: $0) }
        }

        var seen: Set<Int> = []
        var games: [Game] = []
        for ds in dateStrings {
            guard let bdlGames = try? await bdl.getGames(date: ds) else { continue }
            for g in bdlGames where !seen.contains(g.id) {
                seen.insert(g.id)
                games.append(g.toGame())
            }
        }

        todayAdjustments = TodayRecordAdjustments.deltas(
            from: games, lastUpdated: lastUpdated,
        )
        // Recompute GB / WCGB off the adjusted records so those columns
        // stay in step with the bumped W-L. The flat team list is the
        // union of the division buckets (every team sits in one).
        let allTeams = alStandings.values.flatMap { $0 }
            + nlStandings.values.flatMap { $0 }
        adjustedGB = TodayRecordAdjustments.recalculateGB(
            standings: allTeams, games: games,
            adjustments: todayAdjustments, lastUpdated: lastUpdated,
        )
    }

    /// Split the flat array into AL/NL × division buckets and sort each
    /// bucket by win_pct desc. Teams with unknown league/division (very
    /// old Lahman pre-divisional years) get dropped from the buckets.
    /// Also builds the wildcard list per league (non-division-leaders
    /// ranked by win_pct desc).
    private func partition(_ response: StandingsResponse?) {
        let teams = response?.standings ?? []
        var al: [String: [TeamStanding]] = [:]
        var nl: [String: [TeamStanding]] = [:]
        var alWC: [TeamStanding] = []
        var nlWC: [TeamStanding] = []
        for team in teams {
            guard let div = team.division else { continue }
            switch team.league {
            case "AL":
                al[div, default: []].append(team)
                if team.rank != 1 { alWC.append(team) }
            case "NL":
                nl[div, default: []].append(team)
                if team.rank != 1 { nlWC.append(team) }
            default:
                continue
            }
        }
        for key in al.keys { al[key]?.sort(by: Self.standingsSort) }
        for key in nl.keys { nl[key]?.sort(by: Self.standingsSort) }
        alStandings = al
        nlStandings = nl
        // Contenders are every team not ranked first in its division
        // (rank == 1 is the authoritative division-leader indicator;
        // a missing rank keeps the team in the contender pool). If
        // rank is absent across the board both arrays come back empty
        // — the view falls back to hiding the tab in that case.
        alWildcard = alWC.sorted(by: Self.standingsSort)
        nlWildcard = nlWC.sorted(by: Self.standingsSort)
    }

    /// Best record first. win_pct is the primary key; if two teams are
    /// tied (rare exact decimal collision), fall back to the backend's
    /// `rank` field, then to wins.
    private static func standingsSort(_ a: TeamStanding, _ b: TeamStanding) -> Bool {
        let aPct = a.win_pct ?? 0
        let bPct = b.win_pct ?? 0
        if aPct != bPct { return aPct > bPct }
        let aRank = a.rank ?? Int.max
        let bRank = b.rank ?? Int.max
        if aRank != bRank { return aRank < bRank }
        return (a.W ?? 0) > (b.W ?? 0)
    }
}
