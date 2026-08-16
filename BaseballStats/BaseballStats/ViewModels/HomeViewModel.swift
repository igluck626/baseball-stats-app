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

    /// Favorite team's current streak ("W2" / "L3") + last-ten record
    /// (e.g. (6, 4) for L10 6-4). Pulled from our backend's
    /// `/teams/standings?year=N` endpoint — BDL's standings response
    /// only carries W/L, not these dynamic fields. Optional everywhere
    /// because pre-current-season + cold-start scenarios leave them
    /// nil; the hero card hides the row when they are.
    @Published var teamStreakCode: String?
    @Published var teamLastTenW: Int?
    @Published var teamLastTenL: Int?
    @Published var isLoading: Bool = false
    @Published var didLoad: Bool = false
    @Published var error: String?

    /// Standings + records for ALL teams — passed into BoxScoreView
    /// when the user taps a strip card, so the box score's header
    /// can render division rank + W-L for both sides.
    @Published var teamRecords: [Int: TeamRecord] = [:]
    @Published var teamStandings: [Int: TeamStandingInfo] = [:]

    /// Team-leader cards for the four batting + four pitching stats.
    /// nil before the first fetch; an empty inner array means the
    /// fetch landed but the leaderboard returned no qualifying rows.
    @Published var teamLeaders: TeamLeaders?
    @Published var isLoadingLeaders: Bool = false

    /// Last stat the user tapped on the See-All Team Leaders sheet.
    /// Persisted on the VM so re-opening the sheet returns to the
    /// same column. nil before any selection is made — the sheet
    /// falls back to its own default in that case.
    @Published var selectedLeaderStat: String?

    /// Hydrated bio + summary stat line for each id in
    /// `FavoritePlayersStore.shared.playerIds`, preserving the
    /// store's order. Refetched whenever the id list changes.
    @Published var favoritePlayers: [FavoritePlayerDisplay] = []
    @Published var isLoadingFavorites: Bool = false

    /// Active roster for the favorite team — BDL's active list with
    /// each player's MLBAM id resolved + current-season stat line
    /// folded in. Empty before the first fetch; the compact roster
    /// strip and the Roster sheet both read from this.
    @Published var roster: [RosterPlayer] = []
    @Published var isLoadingRoster: Bool = false

    /// Currently-injured players for the favorite team, sorted by
    /// severity ascending (Day-To-Day → 10-Day IL → 15-Day IL →
    /// 60-Day IL). Empty when there are no injuries OR before the
    /// first fetch completes; the Home tab hides the whole Injury
    /// Report section in either case.
    @Published var injuredPlayers: [InjuredPlayer] = []
    /// `{bdl_id → resolved PlayerSearchResult}`. Populated in
    /// parallel with the injury fetch via `bdl.resolveBDLPlayerId`.
    /// Missing entries mean the lookup failed — the corresponding
    /// row is rendered as non-tappable.
    @Published var injuredPlayersResolved: [Int: PlayerSearchResult] = [:]
    @Published var isLoadingInjuries: Bool = false

    /// Season-by-season history for the favorite franchise, most-
    /// recent year first, filtered to `year >= 1900` so the Hub
    /// table doesn't get cluttered with 19th-century NL/AA rows.
    /// Uses the existing `TeamStanding` shape (shared with the
    /// Standings tab) — it's a strict superset of what the history
    /// sheet renders.
    @Published var teamHistory: [TeamStanding] = []
    /// Every postseason series the franchise has played in (winner
    /// or loser), pulled from the SeriesPost pipeline. Used together
    /// with `teamHistory` so the year row can display "🏆 WS" /
    /// "NLCS" / etc. without per-row fetches.
    @Published var teamPostseason: [TeamPostseasonSeries] = []
    /// Major-award winners (MVP / CY Young / ROY / Gold Glove /
    /// Silver Slugger) across the franchise's history, grouped by
    /// award type. Loaded alongside `teamHistory`; empty before the
    /// first fetch or when the franchise has no winners on record.
    @Published var teamAwards: [TeamAwardGroup] = []
    @Published var isLoadingHistory: Bool = false

    /// Team news (MLB.com RSS). Nice-to-have: a failed/empty fetch leaves
    /// this empty and the Home section hides itself rather than erroring.
    @Published var news: [NewsArticle] = []
    @Published var isLoadingNews: Bool = false

    /// League-wide news teaser (team omitted), deduped to collapse the
    /// cross-team syndicated copies the league feed returns. Powers the
    /// "League" toggle in the Home news carousel.
    @Published var leagueNews: [NewsArticle] = []

    /// `{year → list of series the team played that year}`. Built
    /// off `teamPostseason` so the history table can do an O(1)
    /// lookup per row instead of re-scanning the array per cell.
    var postseasonByYear: [Int: [TeamPostseasonSeries]] {
        Dictionary(grouping: teamPostseason) { $0.year }
    }

    /// First live game in the favorite team's ±5-day window, if any.
    /// `recentAndUpcoming` is already team-scoped (every entry has
    /// the favorite in either `home` or `away`), so a plain
    /// `phase == .live` scan is sufficient.
    var liveGame: Game? {
        recentAndUpcoming.first { $0.phase == .live }
    }

    /// True when the favorite team is currently in a live game.
    /// Drives the hero card's live-score branch and the 30s auto-
    /// refresh cadence on the polling task.
    var hasLiveGame: Bool {
        liveGame != nil
    }

    private let bdl: BallDontLieClient
    private let api: APIClient

    init(bdl: BallDontLieClient = .shared, api: APIClient = .shared) {
        self.bdl = bdl
        self.api = api
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

        // Streak + last-ten come from our backend (BDL doesn't ship
        // them on `/standings`). Best-effort: failures leave the
        // fields nil and the hero card just hides the secondary
        // line. Fired AFTER the primary state lands so the card can
        // paint everything else without waiting on this.
        var standingsLastUpdated: String?
        // Hoisted out of the fetch block below so the streak overlay
        // (which needs the full league rows + the favorite's Lahman
        // code) can reuse them after the `deltas` are computed.
        let favLahmanCode = bdlToLahmanTeamId[bdlTeamId]
        var standingsRows: [TeamStanding] = []
        if let lahmanCode = favLahmanCode {
            let year = cal.component(.year, from: today)
            if let resp = (try? await api.getStandings(year: year)) ?? nil {
                standingsLastUpdated = resp.last_updated
                standingsRows = resp.standings ?? []
                if let row = resp.standings?.first(where: { $0.team_id == lahmanCode }) {
                    self.teamStreakCode = row.streak_code
                    self.teamLastTenW   = row.last_ten_w
                    self.teamLastTenL   = row.last_ten_l
                }
            }
        }

        // Fold today's not-yet-official finals into the record so the
        // hero card matches the Standings tab. Uses the ±5-day team
        // window already in `recentAndUpcoming`; gated by the backend
        // standings' `last_updated` so already-absorbed games don't
        // double-count.
        let deltas = TodayRecordAdjustments.deltas(
            from: recentAndUpcoming, lastUpdated: standingsLastUpdated,
        )
        if !deltas.isEmpty {
            self.teamRecords = TodayRecordAdjustments.apply(deltas, to: self.teamRecords)
            self.teamRecord  = self.teamRecords[bdlTeamId]

            // Also adjust the streak to match the standings overlay so
            // the STREAK pill doesn't lag the W/L record beside it.
            // `recalculateGB` rolls `streak_code` forward across today's
            // qualifying finals using the same logic the Standings tab
            // runs; we read just the favorite team's entry by Lahman code.
            let adjusted = TodayRecordAdjustments.recalculateGB(
                standings: standingsRows,
                games: recentAndUpcoming,
                adjustments: deltas,
                lastUpdated: standingsLastUpdated,
            )
            if let code = favLahmanCode,
               let favEntry = adjusted[code],
               let strk = favEntry.strk {
                self.teamStreakCode = strk
            }
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

    /// Fold the shared `LiveGameStore`'s live-games list into the hero strip —
    /// the SAME merge the old self-fetched loop did, minus the fetch:
    /// `LiveGameStore` now owns the single `/live/games` poll (Phase 2, step 3),
    /// and the view feeds its `liveList` in here on every store update. Only the
    /// favorite's in-progress game (matched by gamePk) gets its score / inning
    /// refreshed; when it ends (drops out of the live set) do one full `load` to
    /// capture its final state + records.
    func applyLiveList(_ liveById: [Int: LiveGameSummary], bdlTeamId: Int) async {
        guard hasLiveGame else { return }
        let wereLive = Set(recentAndUpcoming.filter { $0.phase == .live }.map(\.gamePk))

        recentAndUpcoming = recentAndUpcoming.map { g in
            liveById[g.gamePk].map { g.merging(live: $0) } ?? g
        }
        if let ng = nextGame, let live = liveById[ng.gamePk] {
            nextGame = ng.merging(live: live)
        }

        let ended = wereLive.subtracting(liveById.keys)
        if !ended.isEmpty {
            await load(bdlTeamId: bdlTeamId)   // a game finished — refresh finals/records
        }
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

    // MARK: - Team Leaders

    /// Stats surfaced on the Home tab's compact leader card. Order
    /// drives the on-screen section order. Only four per role —
    /// the See-All Stats sheet (`TeamLeadersSheet.battingStats`/
    /// `pitchingStats`) carries the longer list for users who want
    /// the deeper breakdown.
    private static let battingLeaderStats:  [String] = ["WAR", "AVG", "HR", "OPS"]
    private static let pitchingLeaderStats: [String] = ["WAR", "ERA", "SO", "WHIP"]

    /// Fetch the top-3 rows for each of the four batting + four
    /// pitching stats, scoped to the favorite team. Each call is a
    /// single `/leaderboards?team=<Lahman>&limit=3` hit; fans out
    /// in parallel via task group and preserves input order so the
    /// compact card lays the sections out under their declared
    /// header order.
    func loadTeamLeaders(bdlTeamId: Int) async {
        guard let lahmanCode = bdlToLahmanTeamId[bdlTeamId] else { return }
        let year = Calendar.current.component(.year, from: Date())
        isLoadingLeaders = true
        async let batting  = Self.fetchLeaderGroups(
            stats: Self.battingLeaderStats, year: year,
            playerType: "batter", team: lahmanCode, api: api,
        )
        async let pitching = Self.fetchLeaderGroups(
            stats: Self.pitchingLeaderStats, year: year,
            playerType: "pitcher", team: lahmanCode, api: api,
        )
        let (b, p) = await (batting, pitching)
        teamLeaders = TeamLeaders(batting: b, pitching: p)
        isLoadingLeaders = false
    }

    /// Fetch the favorite team's latest news. Fail-silent: any error or an
    /// empty result leaves `news` empty so the Home section stays hidden.
    func loadNews(bdlTeamId: Int) async {
        guard let lahmanCode = bdlToLahmanTeamId[bdlTeamId] else {
            news = []
            return
        }
        isLoadingNews = true
        // Carousel is a top-headlines teaser; the full set is behind "See all"
        // (TeamNewsListView, limit 25).
        news = ((try? await api.getNews(team: lahmanCode, limit: 10)) ?? [])
        isLoadingNews = false
    }

    /// Fetch the league-wide news teaser (team omitted). Fail-silent like
    /// `loadNews`. The league feed is heavily syndicated — a single wire
    /// story is stored once per team, so the newest ~25 can be 20+ copies of
    /// one article. Pull the endpoint max (50) so dedup leaves enough DISTINCT
    /// stories to fill the 10-card carousel.
    func loadLeagueNews() async {
        let raw = (try? await api.getNews(team: nil, limit: 50)) ?? []
        leagueNews = Array(NewsArticle.deduplicated(raw).prefix(10))
    }

    nonisolated private static func fetchLeaderGroups(
        stats: [String], year: Int, playerType: String,
        team: String, api: APIClient,
    ) async -> [StatLeaderGroup] {
        await withTaskGroup(of: (Int, StatLeaderGroup?).self) { group in
            for (i, stat) in stats.enumerated() {
                let capturedStat = stat
                let capturedIdx  = i
                group.addTask {
                    let cards = await fetchTop3(
                        stat:       capturedStat,
                        year:       year,
                        playerType: playerType,
                        team:       team,
                        api:        api,
                    )
                    return (capturedIdx,
                            cards.isEmpty
                                ? nil
                                : StatLeaderGroup(stat: capturedStat, cards: cards))
                }
            }
            var pairs: [(Int, StatLeaderGroup)] = []
            for await (i, g) in group {
                if let g { pairs.append((i, g)) }
            }
            return pairs.sorted { $0.0 < $1.0 }.map { $0.1 }
        }
    }

    nonisolated private static func fetchTop3(
        stat: String, year: Int, playerType: String,
        team: String, api: APIClient,
    ) async -> [LeaderCard] {
        // No ERA IP-floor walk in the top-3 path: surfacing three
        // candidates with their actual values lets the user spot
        // tiny-sample outliers themselves. The backend's pro-rated
        // qualifier still applies; this is just a different shape.
        let outer = try? await api.getLeaderboard(
            stat: stat, year: year, playerType: playerType,
            team: team, limit: 3,
        )
        let inner: LeaderboardResponse? = outer ?? nil
        let candidates = inner?.leaders ?? []
        return candidates.map {
            LeaderCard(stat: stat, value: $0.value, player: $0.player)
        }
    }

    /// Fetch the top-10 leaderboard rows for a single stat, scoped to
    /// the user's favorite team (same Lahman code resolution the
    /// hero-card team-leaders fetch uses). Used by the See-All sheet
    /// — returns an empty list when there's no favorite team set,
    /// the Lahman code can't be resolved, or the backend ships no
    /// qualifying rows. ERA does NOT get the special IP-floor walk
    /// the top-1 card path uses; a 10-row list surfaces the leaders
    /// at a glance and the user can judge minimums in-context.
    func loadTeamLeaderboard(
        stat: String, playerType: String, mode: String = "season",
        year: Int? = nil, yearFrom: Int? = nil, yearTo: Int? = nil,
    ) async -> [LeaderCard] {
        guard let bdlTeamId = FavoriteTeamStore.shared.bdlTeamId,
              let lahmanCode = bdlToLahmanTeamId[bdlTeamId] else {
            return []
        }
        // `year` only matters in season mode (a specific season's team
        // leaders). All-time / career aggregate across seasons, so the
        // backend ignores `year` there; the optional `yearFrom`/`yearTo`
        // window those modes instead (nil = full franchise history).
        let resolvedYear: Int? = mode == "season"
            ? (year ?? Calendar.current.component(.year, from: Date()))
            : nil
        let outer = try? await api.getLeaderboard(
            stat: stat, year: resolvedYear,
            playerType: playerType,
            mode: mode,
            team: lahmanCode,
            yearFrom: yearFrom, yearTo: yearTo,
            limit: 10,
        )
        let inner: LeaderboardResponse? = outer ?? nil
        let candidates = inner?.leaders ?? []
        return candidates.map {
            LeaderCard(stat: stat, value: $0.value, player: $0.player, year: $0.year)
        }
    }

    // MARK: - Team history

    /// Fetch the favorite franchise's full season history + every
    /// postseason series in parallel, then surface them on
    /// `teamHistory` / `teamPostseason`. Both endpoints key on the
    /// Lahman teamID (resolved from `bdlTeamId` via the existing
    /// map). The history list is reversed at the API client side
    /// (year asc) so we sort year desc here, and pre-1900 rows are
    /// filtered out — those Old NL / AA / NA rows are noise for a
    /// modern Home-tab card.
    func loadTeamHistory(bdlTeamId: Int) async {
        guard let lahmanCode = bdlToLahmanTeamId[bdlTeamId] else { return }
        isLoadingHistory = true
        async let historyTask    = (try? await api.getTeamHistory(teamId: lahmanCode)) ?? nil
        async let postseasonTask = (try? await api.getTeamPostseason(teamId: lahmanCode)) ?? nil
        // Awards load alongside history — same Lahman code, same
        // trigger, fetched concurrently with the two above.
        async let awardsTask: Void = loadTeamAwards(teamId: lahmanCode)
        let (historyResp, postResp, _) = await (historyTask, postseasonTask, awardsTask)

        let rows = (historyResp?.history ?? [])
            .filter { ($0.year ?? 0) >= 1900 }
            .sorted { ($0.year ?? 0) > ($1.year ?? 0) }
        self.teamHistory   = rows
        self.teamPostseason = postResp?.postseason ?? []
        self.isLoadingHistory = false
    }

    /// Fetch the franchise's major-award winners (grouped by award
    /// type, year-desc within each group) and surface them on
    /// `teamAwards`. `teamId` is the Lahman code (e.g. "LAN"). A 404
    /// or network failure leaves the list empty — the Awards section
    /// just renders its empty state.
    func loadTeamAwards(teamId: String) async {
        let resp = (try? await api.getTeamAwards(teamId: teamId)) ?? nil
        self.teamAwards = resp?.awards ?? []
    }

    // MARK: - Injuries

    /// Fetch the favorite team's injury list, sort by severity, and
    /// resolve each BDL id to its MLBAM bio in parallel so tap
    /// targets light up as soon as the resolutions land. Severity
    /// order is least-severe first: Day-To-Day → 10-Day IL →
    /// 15-Day IL → 60-Day IL. The list reads bottom-up — heaviest
    /// IL stints sit at the bottom of the sheet's scroll.
    func loadInjuries(bdlTeamId: Int) async {
        isLoadingInjuries = true
        let raw = (try? await bdl.getTeamInjuries(bdlTeamId: bdlTeamId)) ?? []
        let sorted = raw.sorted {
            Self.severityRank($0.status) < Self.severityRank($1.status)
        }
        self.injuredPlayers = sorted

        // Resolve all in parallel. Misses stay absent from the dict
        // and the UI just renders the row without a tap target.
        let pairs = await withTaskGroup(of: (Int, PlayerSearchResult?).self) { group in
            for player in sorted {
                let bdlId = player.bdl_id
                group.addTask { [bdl] in
                    let resolved = try? await bdl.resolveBDLPlayerId(bdlId)
                    return (bdlId, resolved)
                }
            }
            var pairs: [(Int, PlayerSearchResult)] = []
            for await (bdlId, resolved) in group {
                if let r = resolved { pairs.append((bdlId, r)) }
            }
            return pairs
        }
        var resolved: [Int: PlayerSearchResult] = [:]
        for (bdlId, r) in pairs {
            resolved[bdlId] = r
        }
        self.injuredPlayersResolved = resolved
        self.isLoadingInjuries = false
    }

    /// Sort key — lower number = less severe so the list reads
    /// least → most severe (Day-To-Day at top, 60-Day-IL at
    /// bottom). Anything unrecognized gets the trailing bucket so
    /// it falls to the end rather than crashing the sort with a
    /// fatal default. Match keys are BDL's hyphenated wire format
    /// ("60-Day-IL", etc.).
    nonisolated private static func severityRank(_ status: String) -> Int {
        switch status {
        case "Day-To-Day": return 1
        case "10-Day-IL":  return 2
        case "15-Day-IL":  return 3
        case "60-Day-IL":  return 4
        default:           return 5
        }
    }

    // MARK: - Roster

    /// Fetch the favorite team's active roster from BDL, then in
    /// parallel resolve each player's MLBAM id via our backend and
    /// hydrate their current-season stat line. Pitchers go through
    /// `getPitcherCurrentStats`; everyone else uses
    /// `getPlayerCurrentStats`. Players whose BDL → MLBAM resolution
    /// fails still appear in the list with an empty stat line — the
    /// bio is the operator's signal that something didn't map
    /// cleanly, and the UI shows them as "—" rather than dropping
    /// them entirely.
    func loadRoster(bdlTeamId: Int) async {
        isLoadingRoster = true
        let bdlPlayers = (try? await bdl.getActivePlayers(teamId: bdlTeamId)) ?? []
        let players = await withTaskGroup(of: (Int, RosterPlayer?).self) { group in
            for (i, bp) in bdlPlayers.enumerated() {
                let capturedIdx = i
                let capturedBp  = bp
                group.addTask { [bdl, api] in
                    let rp = await Self.hydrateRosterPlayer(
                        bp: capturedBp, bdl: bdl, api: api,
                    )
                    return (capturedIdx, rp)
                }
            }
            var pairs: [(Int, RosterPlayer)] = []
            for await (i, rp) in group {
                if let rp { pairs.append((i, rp)) }
            }
            return pairs.sorted { $0.0 < $1.0 }.map { $0.1 }
        }
        self.roster = players
        isLoadingRoster = false
    }

    nonisolated private static func hydrateRosterPlayer(
        bp: BDLPlayer, bdl: BallDontLieClient, api: APIClient,
    ) async -> RosterPlayer? {
        let position = bp.position ?? ""
        let isPitcher = Self.bdlPositionIsPitcher(position)
        // Resolve BDL → MLBAM. Failures yield a stub roster entry —
        // we'd rather show the player with "—" stats than silently
        // drop them.
        let resolved = try? await bdl.resolveBDLPlayerId(bp.id)
        let mlbamId = resolved?.player_id ?? 0
        let name = resolved?.name ?? bp.fullName
        guard mlbamId > 0 else {
            return RosterPlayer(
                player_id:    0,
                name:         name,
                bdl_id:       bp.id,
                position:     position,
                jersey:       bp.jersey,
                resolved:     nil,
                currentStats: nil,
            )
        }
        let stats: PlayerStatLine?
        if isPitcher {
            let raw = (try? await api.getPitcherCurrentStats(playerId: mlbamId)) ?? nil
            let s = raw?.standard
            let a = raw?.advanced
            stats = PlayerStatLine(
                avg: nil, hr: nil, rbi: nil, ops: nil,
                era: s?.ERA, w: s?.W, so: s?.SO, whip: s?.WHIP,
                g: s?.G,
                war: a?.WAR,
            )
        } else {
            let raw = (try? await api.getPlayerCurrentStats(playerId: mlbamId)) ?? nil
            let s = raw?.standard
            let a = raw?.advanced
            stats = PlayerStatLine(
                avg: s?.BA, hr: s?.HR, rbi: s?.RBI, ops: s?.OPS,
                era: nil, w: nil, so: nil, whip: nil,
                g: nil,
                war: a?.WAR,
            )
        }
        return RosterPlayer(
            player_id:    mlbamId,
            name:         name,
            bdl_id:       bp.id,
            position:     position,
            jersey:       bp.jersey,
            resolved:     resolved,
            currentStats: stats,
        )
    }

    /// Position-string → pitcher? heuristic. BDL surfaces "SP" /
    /// "RP" / "P" for pitchers and conventional position abbrevs
    /// otherwise. Bare "P" defaults to pitcher.
    nonisolated static func bdlPositionIsPitcher(_ raw: String) -> Bool {
        switch raw.uppercased() {
        case "P", "SP", "RP", "CL": return true
        default:                    return false
        }
    }

    // MARK: - Favorite Players

    /// Hydrate every id in `ids` in parallel. Each pid spawns two
    /// fetches — bio + current-side stats — so the strip can render
    /// name/team/position alongside a per-side stat summary. Order
    /// is preserved via index pairs; missing pids (404 / network)
    /// are silently dropped so one stale id doesn't blank the strip.
    func loadFavoritePlayers(ids: [Int]) async {
        guard !ids.isEmpty else {
            favoritePlayers = []
            return
        }
        isLoadingFavorites = true
        let displays = await withTaskGroup(
            of: (Int, FavoritePlayerDisplay?).self,
        ) { group in
            for (i, id) in ids.enumerated() {
                group.addTask { [api] in
                    guard let player = (try? await api.getPlayerByMlbId(id)) ?? nil
                    else { return (i, nil) }
                    let isPitcher = Self.deriveIsPitcher(player: player)
                    let line = await Self.buildStatLine(
                        player: player, isPitcher: isPitcher, api: api,
                    )
                    return (i, FavoritePlayerDisplay(
                        player:    player,
                        isPitcher: isPitcher,
                        statLine:  line,
                    ))
                }
            }
            var pairs: [(Int, FavoritePlayerDisplay)] = []
            for await (i, d) in group {
                if let d { pairs.append((i, d)) }
            }
            return pairs.sorted { $0.0 < $1.0 }.map { $0.1 }
        }
        favoritePlayers = displays
        isLoadingFavorites = false
    }

    /// Two-way players (Ohtani) — we route them through the pitcher
    /// summary line. Most "two-way" entries are starters whose bio
    /// position field is "P" anyway; for the edge case where the
    /// bio explicitly carries `is_pitcher = true`, that flag wins.
    nonisolated private static func deriveIsPitcher(player: PlayerSearchResult) -> Bool {
        if let explicit = player.is_pitcher { return explicit }
        switch (player.position ?? "").uppercased() {
        case "P", "SP", "RP": return true
        default: return false
        }
    }

    nonisolated private static func buildStatLine(
        player: PlayerSearchResult, isPitcher: Bool, api: APIClient,
    ) async -> String {
        if isPitcher {
            let stats = (try? await api.getPitcherCurrentStats(
                playerId: player.player_id,
            )) ?? nil
            let s = stats?.standard
            let era = s?.ERA.map { String(format: "%.2f", $0) } ?? "—"
            let w   = s?.W.map(String.init) ?? "—"
            let so  = s?.SO.map(String.init) ?? "—"
            return "ERA \(era) · W \(w) · SO \(so)"
        } else {
            let stats = (try? await api.getPlayerCurrentStats(
                playerId: player.player_id,
            )) ?? nil
            let s = stats?.standard
            let avg = s?.BA.map { String(format: "%.3f", $0) } ?? "—"
            let hr  = s?.HR.map(String.init) ?? "—"
            let rbi = s?.RBI.map(String.init) ?? "—"
            return "AVG \(avg) · HR \(hr) · RBI \(rbi)"
        }
    }
}

// MARK: - Display types

struct LeaderCard: Identifiable, Hashable {
    let stat: String          // "AVG" / "HR" / "ERA" / "WHIP" / …
    let value: Double?
    let player: PlayerSearchResult
    /// Season the value occurred in. Set for season / all-time entries,
    /// nil for career aggregates. Used by the All-Time team-leaders rows
    /// to show e.g. "1961" under the name.
    var year: Int? = nil
    var id: String { "\(stat)-\(player.player_id)-\(year ?? 0)" }
}

/// Three leaders for a single stat. Used by the compact home-card
/// section so each stat's section can render its own ordered rows.
struct StatLeaderGroup: Identifiable, Hashable {
    let stat: String
    /// Up to three cards in rank order. May be empty if the backend
    /// returned no qualifying rows for this stat (cold-start, low
    /// PA / IP threshold, etc.).
    let cards: [LeaderCard]
    var id: String { stat }
}

struct TeamLeaders: Hashable {
    let batting:  [StatLeaderGroup]
    let pitching: [StatLeaderGroup]
}

struct FavoritePlayerDisplay: Identifiable, Hashable {
    let player: PlayerSearchResult
    let isPitcher: Bool
    /// Pre-formatted "AVG .305 · HR 12 · RBI 32" or
    /// "ERA 2.87 · W 5 · SO 64", rendered on the card under the name.
    let statLine: String
    var id: Int { player.player_id }
}

/// Current-season stat snapshot for a roster player. Only one side
/// (batter or pitcher) is populated for each entry — the empty side
/// stays nil. The UI picks the right side off `position`.
struct PlayerStatLine: Hashable {
    // Batters
    let avg: Double?
    let hr:  Int?
    let rbi: Int?
    let ops: Double?
    // Pitchers
    let era:  Double?
    let w:    Int?
    let so:   Int?
    let whip: Double?
    /// Games (appearances). Populated for pitchers — used by the
    /// roster sheet to sort relievers by usage. nil for batters.
    let g:    Int?
    /// bref WAR — populated for both sides from the `advanced` block
    /// on the current-stats response. Surfaced as the leading column
    /// on the Roster sheet's tables.
    let war: Double?
}

/// One row in the roster strip / sheet. Keeps both the BDL id (for
/// the source row) and the resolved MLBAM id (for navigation into
/// the existing profile view). `currentStats` is nil until the
/// hydrate task completes; the UI renders "—" placeholders while
/// it's missing.
struct RosterPlayer: Identifiable, Hashable {
    /// Resolved MLBAM id. 0 means the BDL → MLBAM resolution didn't
    /// succeed — UI should disable the tap-into-profile gesture for
    /// these rows.
    let player_id: Int
    let name: String
    let bdl_id: Int
    /// Raw BDL position string ("SP" / "RP" / "P" / "1B" / "OF" /
    /// "DH" / etc.). Pass through `RosterPositionGroup.from(_:)` to
    /// bucket for the segmented picker.
    let position: String
    /// Uniform number from BDL (`BDLPlayer.jersey`); nil/blank when BDL
    /// doesn't carry one. Rendered as the leading anchor on each row.
    let jersey: String?
    /// The full `PlayerSearchResult` from `resolveBDLPlayerId` —
    /// needed to push the profile destination on tap (the existing
    /// nav route takes a `PlayerSearchResult`). nil when resolution
    /// failed.
    let resolved: PlayerSearchResult?
    let currentStats: PlayerStatLine?
    var id: Int { bdl_id }
}

/// Coarse-grained position bucket used by the Roster sheet's
/// segmented picker and to group the compact home-tab strip.
enum RosterPositionGroup: String, CaseIterable, Hashable {
    case sp = "SP"   // Starting pitchers
    case rp = "RP"   // Relief pitchers
    case c  = "C"    // Catchers
    case infield  = "IF"   // 1B / 2B / 3B / SS
    case outfield = "OF"   // LF / CF / RF / OF
    case dh = "DH"   // Designated hitter

    var displayName: String {
        switch self {
        case .sp: return "SP"
        case .rp: return "RP"
        case .c:  return "C"
        case .infield:  return "IF"
        case .outfield: return "OF"
        case .dh: return "DH"
        }
    }

    /// Map a raw BDL position string to its bucket. Falls back to
    /// `rp` for an ambiguous "P" (most roster pitchers without an
    /// SP/RP qualifier are relievers in modern MLB rosters) and to
    /// `infield` for generic "IF". Anything unrecognized maps to
    /// `infield` so the player still surfaces somewhere.
    static func from(_ raw: String) -> RosterPositionGroup {
        switch raw.uppercased() {
        case "SP":                                 return .sp
        case "RP", "CL":                           return .rp
        case "P":                                  return .rp
        case "C":                                  return .c
        case "1B", "2B", "3B", "SS", "IF":         return .infield
        case "LF", "CF", "RF", "OF":               return .outfield
        case "DH":                                 return .dh
        default:                                   return .infield
        }
    }
}
