//
//  HomeViewModel.swift
//  BaseballStats
//
//  Drives the Home tab. Fetches a ±5-day window of games for the
//  user's favorite team and the current-year standings, then
//  projects a few derived shapes (last/next game, recent+upcoming
//  strip, record, standing) the view binds to directly.
//
//  Auto-refresh: 60s polling while any game in the window is live.
//

import Combine
import Foundation

@MainActor
final class HomeViewModel: ObservableObject {
    /// Last 3 finals + next 3 upcoming (or live if any), oldest →
    /// newest. Up to 7 entries; fewer when the team is off mid-week.
    @Published var recentAndUpcoming: [Game] = []
    @Published var lastGame: Game?
    @Published var nextGame: Game?
    @Published var teamRecord: TeamRecord?
    @Published var teamStanding: TeamStandingInfo?
    @Published var isLoading: Bool = false
    @Published var didLoad: Bool = false
    @Published var error: String?

    /// Standings + records for ALL teams — passed into BoxScoreView
    /// when the user taps a strip card, so the box score's header
    /// can render division rank + W-L for both sides.
    @Published var teamRecords: [Int: TeamRecord] = [:]
    @Published var teamStandings: [Int: TeamStandingInfo] = [:]

    private let bdl: BallDontLieClient
    private var refreshTask: Task<Void, Never>?

    init(bdl: BallDontLieClient = .shared) {
        self.bdl = bdl
    }

    /// Full reload: ±5 days of team games + standings. Cheap on
    /// repeat calls — BDL responses are cached for 30s per date.
    func load(bdlTeamId: Int) async {
        isLoading = true
        error = nil
        let today = Date()
        let cal = Calendar.current
        let year = cal.component(.year, from: today)

        async let standingsTask: [BDLStandingsEntry]? = try? bdl.getStandings(season: year)

        let dates: [String] = (-5...5).compactMap { offset in
            cal.date(byAdding: .day, value: offset, to: today)
                .map { ScoresViewModel.iso($0) }
        }

        // Fan out the per-day fetches in parallel — sequential would
        // be ~11×latency in the worst case. Each call hits a 30s BDL
        // cache so subsequent loads (auto-refresh, tab re-entry) are
        // mostly free.
        var fetched: [Game] = []
        var seen: Set<Int> = []
        await withTaskGroup(of: [BDLGame].self) { group in
            for d in dates {
                group.addTask { [bdl] in
                    (try? await bdl.getTeamGames(date: d, teamId: bdlTeamId)) ?? []
                }
            }
            for await items in group {
                for g in items where !seen.contains(g.id) {
                    seen.insert(g.id)
                    fetched.append(g.toGame())
                }
            }
        }
        let sorted = fetched.sorted {
            ($0.startDate ?? .distantFuture) < ($1.startDate ?? .distantFuture)
        }

        let finals = sorted.filter { $0.phase == .final }
        let liveOrPreview = sorted.filter { $0.phase != .final }
        self.lastGame = finals.last
        // `nextGame` prioritizes live > earliest upcoming. Falls back
        // to nil when the season's over.
        self.nextGame = liveOrPreview.first(where: { $0.phase == .live })
            ?? liveOrPreview.first
        self.recentAndUpcoming = Self.buildStrip(
            finals: finals, liveOrPreview: liveOrPreview,
        )

        if let standings = await standingsTask {
            self.teamRecords   = Self.recordsByBDLTeamId(standings)
            self.teamStandings = Self.standingsByBDLTeamId(standings)
            self.teamRecord    = self.teamRecords[bdlTeamId]
            self.teamStanding  = self.teamStandings[bdlTeamId]
        }

        isLoading = false
        didLoad = true
    }

    /// Up to 7-card strip: last 3 finals (oldest-first) + first
    /// 4 from the live/upcoming slice. Bias toward upcoming when
    /// the team has more in front of them than behind (cold start
    /// in March / preseason).
    private static func buildStrip(
        finals: [Game], liveOrPreview: [Game],
    ) -> [Game] {
        let recents  = finals.suffix(3)
        let upcoming = liveOrPreview.prefix(4)
        return Array(recents) + Array(upcoming)
    }

    /// 60s auto-refresh while any game in the strip is live. Mirrors
    /// ScoresViewModel's pattern — cancellable, weak-self loop.
    func startAutoRefresh(bdlTeamId: Int) {
        stopAutoRefresh()
        guard recentAndUpcoming.contains(where: { $0.phase == .live }) else {
            return
        }
        refreshTask = Task { @MainActor [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 60 * 1_000_000_000)
                guard !Task.isCancelled, let self else { return }
                await self.load(bdlTeamId: bdlTeamId)
            }
        }
    }

    func stopAutoRefresh() {
        refreshTask?.cancel()
        refreshTask = nil
    }

    // MARK: - Standings projection (duplicated from ScoresViewModel)

    private static func recordsByBDLTeamId(
        _ standings: [BDLStandingsEntry],
    ) -> [Int: TeamRecord] {
        var dict: [Int: TeamRecord] = [:]
        for s in standings {
            dict[s.team.id] = TeamRecord(wins: s.wins, losses: s.losses, pct: nil)
        }
        return dict
    }

    private static func standingsByBDLTeamId(
        _ standings: [BDLStandingsEntry],
    ) -> [Int: TeamStandingInfo] {
        var buckets: [String: [BDLStandingsEntry]] = [:]
        for s in standings {
            guard let lg  = leagueCode(s.team.league),
                  let div = divisionCode(s.team.division) else { continue }
            buckets["\(lg) \(div)", default: []].append(s)
        }
        var out: [Int: TeamStandingInfo] = [:]
        for (divisionLabel, entries) in buckets {
            let sorted = entries.sorted {
                if $0.wins != $1.wins { return $0.wins > $1.wins }
                return $0.losses < $1.losses
            }
            for (i, e) in sorted.enumerated() {
                out[e.team.id] = TeamStandingInfo(
                    rank: i + 1, divisionLabel: divisionLabel,
                )
            }
        }
        return out
    }

    private static func leagueCode(_ s: String?) -> String? {
        switch s {
        case "American": return "AL"
        case "National": return "NL"
        default:         return nil
        }
    }

    private static func divisionCode(_ s: String?) -> String? {
        switch s {
        case "East", "Central", "West": return s
        default: return nil
        }
    }
}
