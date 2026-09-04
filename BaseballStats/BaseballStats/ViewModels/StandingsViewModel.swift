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

    /// The live game-feed overlay from `/teams/standings`, when the response
    /// carried one. Current season only; nil for historical years and when the
    /// feed was unavailable, in which case the adjustment falls back to the
    /// behaviour that shipped before it existed.
    private var recentForm: RecentForm?

    /// The win percentage each row is ORDERED by, keyed by Lahman code.
    ///
    /// ⚠️ THIS IS A PINNED ORDER, NOT A LIVE ONE, AND THAT IS THE POINT. It is
    /// recomputed only on a load the user asked for — opening the tab, pulling
    /// to refresh, changing the year. The 90-second background tick updates
    /// every NUMBER on screen and leaves this alone, so a table someone is
    /// reading does not rearrange under them.
    ///
    /// Rows still sort by it on every pass, including quiet ones. Without that
    /// a quiet tick would re-partition from the response and silently revert
    /// to the backend's unadjusted order — suppressing the re-sort has to mean
    /// "keep the order you have", never "fall back to the old one".
    ///
    /// Empty on first load, where every row falls back to `win_pct` and the
    /// order is exactly what shipped before.
    private var orderingPct: [String: Double] = [:]

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

    /// - Parameter quiet: run as a background refresh — no spinner, and a failed
    ///   fetch leaves the table on screen instead of emptying it. Standings are
    ///   the third screen to need this (after the Scores slate and the Home hero
    ///   card) for the same reason each time: the user-initiated `load` is
    ///   written for someone who asked and is watching, so it announces itself
    ///   and treats a failure as a result. On a 90-second clock that is a
    ///   spinner flash and a table that empties on one dropped request — both
    ///   worse than the staleness the refresh exists to fix.
    func loadStandings(quiet: Bool = false) async {
        if !quiet {
            isLoading = true
            error = nil
        }
        do {
            let response = try await api.getStandings(year: selectedYear)
            partition(response)
            lastUpdated = response?.last_updated
            recentForm  = response?.recent_form
        } catch {
            // A quiet tick keeps what is on screen; only a load the user asked
            // for is allowed to replace the table with an error.
            if !quiet {
                self.error = error.localizedDescription
                alStandings = [:]
                nlStandings = [:]
                lastUpdated = nil
                recentForm  = nil
            }
        }
        if !quiet { isLoading = false }
        // Overlay today's not-yet-official results once the standings
        // (and their `last_updated` cutoff) have landed.
        //
        // ⚠️ `quiet` IS EXACTLY THE USER-INITIATED TEST, which is why the order
        // hangs off it rather than a second flag that could drift out of step.
        // `quiet: false` is every load a person caused — the `.task` on tab
        // entry, pull to refresh, and a year change; `quiet: true` is only the
        // 90-second `periodicRefresh` tick.
        await loadTodayAdjustments(commitOrder: !quiet)
    }

    /// Fold today's already-final games into a per-team W/L delta keyed
    /// by BDL team id. Mirrors the Scores tab's ±1 UTC day envelope +
    /// ET-date filter so evening games (which land on "UTC tomorrow")
    /// are still attributed to today. Only the current season is
    /// adjusted; a game is counted only when it finished AFTER the
    /// standings' `last_updated` (approximated by its start time, which
    /// is strictly before its end) so already-absorbed games aren't
    /// double-counted.
    /// - Parameter commitOrder: whether this pass is allowed to REORDER the
    ///   table. True for a load the user asked for — tab entry, pull to
    ///   refresh, a year change — and false for the 90-second background tick.
    ///
    ///   ⚠️ FALSE DOES NOT MEAN "SKIP THE REFRESH". Every number still updates
    ///   on a quiet tick: the records, the streak, GB, the splits. Only the
    ///   row ORDER is held, so the table cannot rearrange under someone who is
    ///   reading it. The cost is bounded and visible — between a quiet tick
    ///   and the next real load a club can show a record that its position
    ///   contradicts, which is the same 79-54-above-78-54 shape the reorder
    ///   exists to fix, except temporary. The dagger on an adjusted row is
    ///   what marks those numbers as provisional while that is true.
    func loadTodayAdjustments(commitOrder: Bool = true) async {
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

        // ⚠️ `.counted` REPLACES `.unanchored`, AND THE OLD COMMENT HERE WAS
        // WRONG ABOUT WHY THAT WAS SAFE. It argued the cutoff was trustworthy
        // because the base and `lastUpdated` came from the same response — true,
        // but beside the point. `lastUpdated` records when WE fetched, not what
        // the fetch contained, and BDL trails a final by ~9.4h. So a game could
        // start before our refresh, be dropped by the cutoff as "already
        // counted", and still be missing from the base. That is the reversion:
        // right all evening, wrong from 06:00 ET until BDL catches up.
        //
        // The feed's `games_played` is an independent live count of what each
        // club has actually played, which is the second source this tab never
        // had — its base and its cutoff were the same daily table talking to
        // itself. Falls back to `.unanchored` when the overlay is absent
        // (historical seasons, or a BDL outage), which is exactly today's
        // behaviour rather than a new failure mode.
        // The flat team list is the union of the division buckets (every team
        // sits in one). Needed by both the absorption below and `recalculateGB`.
        let allTeams = alStandings.values.flatMap { $0 }
            + nlStandings.values.flatMap { $0 }

        let absorption: TodayRecordAdjustments.Absorption = {
            guard let feed = recentForm?.gamesPlayedByBDLId, !feed.isEmpty else {
                return .unanchored
            }
            // The displayed record, keyed by BDL id — the standings rows this
            // view renders, bridged through the map the tab already holds.
            var current: [Int: TeamRecord] = [:]
            for row in allTeams {
                guard let code = row.team_id, let bdlId = lahmanToBDLTeamId[code],
                      let w = row.W, let l = row.L else { continue }
                // `pct` is a display String on TeamRecord and is unused by the
                // absorption, which reads only wins + losses.
                current[bdlId] = TeamRecord(wins: w, losses: l, pct: nil)
            }
            return .counted(feedGamesPlayed: feed, current: current)
        }()

        todayAdjustments = TodayRecordAdjustments.deltas(
            from: games, lastUpdated: lastUpdated, absorption: absorption,
        )
        // The SAME absorption the deltas used. These two derive their game set
        // from `unabsorbedResultsByTeam` together, and handing them different
        // absorptions would put a record and the streak beside it on different
        // evenings — the same class of fault as the record double-counting,
        // one field over.
        adjustedGB = TodayRecordAdjustments.recalculateGB(
            standings: allTeams, games: games,
            adjustments: todayAdjustments, lastUpdated: lastUpdated,
            absorption: absorption,
        )

        // Pin the new order and re-sort into it. Only now — the adjusted pcts
        // do not exist until `recalculateGB` has run, so the sort in
        // `partition` above necessarily used the PREVIOUS pinned values.
        guard commitOrder else { return }
        var pinned: [String: Double] = [:]
        for (code, cols) in adjustedGB {
            if let pct = cols.pct { pinned[code] = pct }
        }
        // Unchanged when nothing was adjusted, so a day with no finals yet
        // sorts exactly as the backend ordered it.
        guard pinned != orderingPct else { return }
        orderingPct = pinned
        resortIntoPinnedOrder()
    }

    /// Re-sort the already-partitioned buckets into `orderingPct` and restamp
    /// `rank` from the new positions. Separate from `partition` because the
    /// order can only be settled AFTER the adjustment, and re-partitioning
    /// from the response to achieve that would mean holding two sort orders
    /// at once — the shape that invites someone to collapse it back.
    private func resortIntoPinnedOrder() {
        let pinned = orderingPct
        func fix(_ bucket: inout [String: [TeamStanding]]) {
            for key in bucket.keys {
                bucket[key]?.sort { Self.standingsSort($0, $1, pinned) }
                for i in bucket[key]!.indices { bucket[key]![i].rank = i + 1 }
            }
        }
        fix(&alStandings)
        fix(&nlStandings)
        // Contention follows the restamped ranks, same rule as `partition`.
        alWildcard = alStandings.values.flatMap { $0 }.filter { $0.rank != 1 }
            .sorted { Self.standingsSort($0, $1, pinned) }
        nlWildcard = nlStandings.values.flatMap { $0 }.filter { $0.rank != 1 }
            .sorted { Self.standingsSort($0, $1, pinned) }
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
        for team in teams {
            guard let div = team.division else { continue }
            switch team.league {
            case "AL": al[div, default: []].append(team)
            case "NL": nl[div, default: []].append(team)
            default:   continue
            }
        }
        let pinned = orderingPct
        for key in al.keys {
            al[key]?.sort { Self.standingsSort($0, $1, pinned) }
            // ⚠️ RANK IS RESTAMPED FROM POSITION, and this is where the two
            // notions of "leader" stop being able to disagree. The view had
            // both `team.rank == 1` (a stored field) and `index == 0` (list
            // position) driving different parts of the same row; after a
            // re-sort those are the same fact, so it is derived once here.
            for i in al[key]!.indices { al[key]![i].rank = i + 1 }
        }
        for key in nl.keys {
            nl[key]?.sort { Self.standingsSort($0, $1, pinned) }
            for i in nl[key]!.indices { nl[key]![i].rank = i + 1 }
        }
        alStandings = al
        nlStandings = nl
        // Contenders are every team not ranked first in its division.
        //
        // ⚠️ REBUILT FROM THE RESTAMPED ROWS, not from the pre-sort copies
        // collected above. Contention is "not first in its division", so a
        // club whose adjusted record moved it into first has to leave this
        // list and one it displaced has to enter it. Reading the stale copies
        // would leave the wild-card race describing yesterday's division.
        let alRestamped = al.values.flatMap { $0 }.filter { $0.rank != 1 }
        let nlRestamped = nl.values.flatMap { $0 }.filter { $0.rank != 1 }
        alWildcard = alRestamped.sorted { Self.standingsSort($0, $1, pinned) }
        nlWildcard = nlRestamped.sorted { Self.standingsSort($0, $1, pinned) }
    }

    /// Best record first. win_pct is the primary key; if two teams are
    /// tied (rare exact decimal collision), fall back to the backend's
    /// `rank` field, then to wins.
    /// The pct a row sorts on: the pinned adjusted value when there is one,
    /// else the backend's. Free function rather than a closure capture so the
    /// division buckets and the wild-card lists cannot drift apart.
    static func orderingValue(_ t: TeamStanding,
                                      _ pinned: [String: Double]) -> Double {
        if let code = t.team_id, let p = pinned[code] { return p }
        return t.win_pct ?? 0
    }

    /// Internal rather than private so the ordering rules can be tested
    /// without standing up a view model and a network.
    static func standingsSort(_ a: TeamStanding, _ b: TeamStanding,
                                      _ pinned: [String: Double] = [:]) -> Bool {
        let aPct = orderingValue(a, pinned)
        let bPct = orderingValue(b, pinned)
        if aPct != bPct { return aPct > bPct }
        let aRank = a.rank ?? Int.max
        let bRank = b.rank ?? Int.max
        if aRank != bRank { return aRank < bRank }
        return (a.W ?? 0) > (b.W ?? 0)
    }
}
