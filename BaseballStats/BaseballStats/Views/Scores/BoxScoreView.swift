//
//  BoxScoreView.swift
//  BaseballStats
//
//  Detail view pushed from a Scores tab game card. Surfaces the
//  game header (logos + score + status), inning-by-inning
//  linescore, and per-team batting + pitching lineups.
//
//  Player rows are tappable: the row's MLB id maps to our DB via
//  `APIClient.getPlayerByMlbId`, then we push the existing
//  `PlayerProfileView`. Lookups that miss (player not in our DB)
//  show a brief inline error and stay on this screen.
//

import Combine
import SwiftUI

@MainActor
final class BoxScoreViewModel: ObservableObject {
    /// Mutable so the end-transition can replace it with a coherent `.final`
    /// game (see `beginFinalize`). Every display path reads `game` as its
    /// not-live fallback, so swapping in a final game — together with clearing
    /// `live` — flips the whole view to final at once (single source, no
    /// per-path suppression).
    @Published private(set) var game: Game

    @Published var boxScore: BoxScoreResponse?
    /// Cumulative W-L-SV through this game's date for each decision
    /// pitcher, keyed by MLBAM id (per the contextual record endpoint).
    /// Populated by `loadPitcherRecords` after the box score lands.
    @Published var pitcherRecords: [Int: PitcherRecord] = [:]
    /// Same records, keyed by BDL player id — convenience for the
    /// view layer, which only has `BoxPlayer.person.id` (BDL) on hand
    /// and would otherwise need to walk the bdl→mlbam mapping per
    /// render.
    @Published var pitcherRecordsByBDL: [Int: PitcherRecord] = [:]
    /// BDL ids already asked about, so a live game only resolves pitchers who
    /// have NEWLY earned a decision rather than re-fetching the whole set each
    /// snapshot. Holds ids whose lookup failed too — a miss is not worth
    /// retrying every 15 seconds for the life of a game. Sister to
    /// `batterStatsRequested`.
    private var pitcherRecordsRequested: Set<Int> = []
    /// Point-in-time HR / 2B / 3B totals for every batter with a
    /// notable hit in this game, keyed by BDL player id. Mirrors
    /// the pitcher-record dict; populated by `loadBatterStatsAtDate`
    /// after the box score lands and the notable batters can be
    /// identified.
    @Published var batterStatsAtDate: [Int: BatterStatsAtDate] = [:]
    /// BDL ids already asked about, so a live game only resolves batters who
    /// have NEWLY become notable rather than re-fetching the whole set each
    /// snapshot. Holds ids whose lookup failed too — a miss is not worth
    /// retrying every 15 seconds for the life of a game.
    private var batterStatsRequested: Set<Int> = []
    /// Live situation snapshot. For LIVE games `applyLiveDetail` sets this from
    /// the shared `LiveGameStore`'s `LiveGameDetail` (Phase 2, step 5); for
    /// final / pre-game it stays nil and the view falls back to `game`.
    @Published var live: LiveFeedResponse?
    /// Full play stream. Used by the `playsSection` to render the "Scoring" and
    /// "All" modes. For LIVE games `applyLiveDetail` writes it from the store's
    /// snapshot; for final games `loadPlays` does a one-shot fetch.
    @Published var plays: [BDLPlay] = []
    @Published var isLoading = false
    @Published var error: String?

    private let bdl: BallDontLieClient
    private let api: APIClient

    init(game: Game, bdl: BallDontLieClient = .shared, api: APIClient = .shared) {
        self.game = game
        self.bdl  = bdl
        self.api  = api
    }

    // MARK: - Display source (live payload when live, frozen `game` otherwise)
    //
    // One place decides where the header + linescore read from — matching the
    // backend's "single source per field" principle. While the game is LIVE and
    // a polled snapshot is present, the banner score / inning label / linescore
    // come from `live` (the SAME object, refreshed every poll, that the
    // situation card and plays list already use), so all four surfaces agree.
    // For FINAL / PRE-GAME there is no `live`, so they fall back to the initial
    // `game` object (which the live poll never updates).

    /// Live linescore from the applied store snapshot — non-nil ONLY once a
    /// live snapshot has been applied (via `applyLiveDetail`). Keys off the
    /// SNAPSHOT, not the frozen `game.phase`, so a game opened pre-game that
    /// goes live switches to live values; for final / pre-game `live` stays nil
    /// and callers fall back to `game`.
    private var liveLinescore: LiveLinescore? {
        live?.liveData.linescore
    }

    /// Banner score: play-derived from the live snapshot when live, else the
    /// frozen game score (finished / scheduled games).
    var displayAwayScore: Int? { liveLinescore?.teams?.away?.runs ?? game.teams.away.score }
    var displayHomeScore: Int? { liveLinescore?.teams?.home?.runs ?? game.teams.home.score }

    /// Banner inning/status ordinal ("Bot 6th"): fresh from the live snapshot
    /// when live, else the frozen game's.
    var displayInningOrdinal: String? {
        liveLinescore?.currentInningOrdinal ?? game.linescore?.currentInningOrdinal
    }

    /// Linescore card source: rebuilt from the live snapshot when live (grid +
    /// R/H/E + inning state), else the frozen game linescore. Same `Inning` /
    /// `LinescoreTeamsTotals` types either way, so the view renders unchanged.
    var displayLinescore: Linescore? {
        guard let ll = liveLinescore else { return game.linescore }
        return Linescore(
            currentInning:        ll.currentInning,
            currentInningOrdinal: ll.currentInningOrdinal,
            inningState:          ll.inningState,
            innings:              ll.innings,
            teams:                ll.teams,
            scheduledInnings:     ll.scheduledInnings,
            isTopInning:          ll.isTopInning,
            balls:                ll.balls,
            strikes:              ll.strikes,
            outs:                 ll.outs,
        )
    }

    func load() async {
        isLoading = true
        error = nil
        // LIVE games: `LiveGameStore` owns the /live/games/{id} detail now
        // (Phase 2, step 5). The view subscribes and feeds `applyLiveDetail`,
        // which populates boxScore / live / plays (and, once, the derived stats)
        // and clears `isLoading`. So there's nothing to fetch here for a live
        // game — leaving `isLoading` true until the first snapshot lands.
        guard game.phase != .live else { return }

        // Final / preview: stats + plays are independent fetches — run them in
        // parallel so the box score + plays section land together.
        async let boxTask   = loadBoxScore()
        async let playsTask = loadPlays()
        _ = await boxTask
        _ = await playsTask
        // Pitcher records + batter point-in-time totals both depend on the box
        // score (need decisions / notable hits) and are independent of each
        // other, so fan them out in parallel.
        async let pitcherRecordsTask = loadPitcherRecords(force: true)
        async let batterStatsTask    = loadBatterStatsAtDate()
        _ = await pitcherRecordsTask
        _ = await batterStatsTask
        isLoading = false
    }

    /// Apply a live snapshot from `LiveGameStore` (Phase 2, step 5). Populates
    /// boxScore / live / plays from ONE consistent `LiveGameDetail` — the same
    /// source the situation card and plays list read — so every box-score
    /// element stays mutually consistent (the score-vs-plays skew fix). On the
    /// FIRST snapshot it also kicks the derived point-in-time stats once,
    /// mirroring the old flow (which loaded them once after the initial live
    /// fetch and did NOT refresh them each poll tick).
    func applyLiveDetail(_ detail: LiveGameDetail) {
        plays     = detail.playsAsBDL
        live      = detail.toLiveFeedResponse()
        boxScore  = detail.toBoxScoreResponse()
        error     = nil
        isLoading = false
        // Pitcher records on EVERY snapshot, for the same reason as the batters
        // below: decisions are ASSIGNED as the game unfolds. This used to run
        // once, on the first live snapshot, where the decision set is normally
        // still empty — `loadPitcherRecords` early-returned on it and the flag
        // meant it never ran again, so a live game showed no records at all.
        // `loadPitcherRecords` skips anyone it has already asked about, so the
        // usual tick resolves to a set difference and no network call.
        Task { await self.loadPitcherRecords() }
        // Batter totals on EVERY snapshot, because which batters need one
        // changes as the game goes on: a double in the 7th makes that batter
        // notable for the first time. `loadBatterStatsAtDate` skips anyone it
        // has already asked about, so the usual tick resolves to a set
        // difference and no network call at all.
        Task { await self.loadBatterStatsAtDate() }
    }

    /// SYNCHRONOUS first half of the end-transition. Folds the last live
    /// snapshot into a coherent `.final` `game` — capturing the on-screen
    /// near-final score/linescore via the existing display accessors (which read
    /// `live`) BEFORE clearing `live`, so the provisional final equals exactly
    /// what was on screen — then drops `live`. Runs in the same actor turn as the
    /// membership `onChange`, so the next frame already reads final everywhere:
    /// no stale-mid-game flash. Idempotent: once `live` is nil this early-returns.
    func beginFinalize() {
        guard live != nil else { return }
        game = game.asFinal(
            awayScore: displayAwayScore,
            homeScore: displayHomeScore,
            linescore: displayLinescore,
        )
        live = nil
    }

    /// ASYNC second half. Refetches the AUTHORITATIVE final box score / plays /
    /// decision records (the same fetches a game opened after it ended runs),
    /// then reconciles the final `game`'s SCORE from the box score — team runs =
    /// sum of that side's batters' runs — catching a walk-off run that scored
    /// after the last live poll, or an official scoring change. The BDL box score
    /// carries no team linescore grid, so the grid stays on the near-final
    /// snapshot values from `beginFinalize` (acceptable). Best-effort: on fetch
    /// failure the provisional final stands, so the view is still coherently
    /// FINAL. `load()` can't be reused (its `guard game.phase != .live` — and
    /// `game` is now `.final` — would short-circuit).
    func completeFinalize() async {
        async let boxTask   = loadBoxScore()
        async let playsTask = loadPlays()
        _ = await boxTask
        _ = await playsTask
        if let bs = boxScore {
            game = game.asFinal(
                awayScore: Self.teamRuns(bs.teams.away) ?? game.teams.away.score,
                homeScore: Self.teamRuns(bs.teams.home) ?? game.teams.home.score,
                linescore: game.linescore,   // grid stays on the near-final snapshot
            )
        }
        async let pitcherRecordsTask = loadPitcherRecords(force: true)
        async let batterStatsTask    = loadBatterStatsAtDate()
        _ = await pitcherRecordsTask
        _ = await batterStatsTask
        isLoading = false
    }

    /// A team's total runs from the box score = the sum of its batters' runs
    /// scored. Returns nil if no batter carries a `runs` value (incomplete box
    /// score) so the caller keeps the provisional near-final score.
    private static func teamRuns(_ team: BoxScoreTeam) -> Int? {
        let runs = team.players.values.compactMap { $0.stats?.batting?.runs }
        return runs.isEmpty ? nil : runs.reduce(0, +)
    }

    /// Resolve each decision pitcher's MLBAM id (via the backend's
    /// bdl→mlbam endpoint) and fetch their cumulative record through
    /// the game's date. Stores both an MLBAM-keyed and a BDL-keyed
    /// view of the same records — the BDL key is what the view layer
    /// has on hand without walking the resolution map per render.
    /// Failures degrade silently to the placeholder + isGameToday
    /// fallback (the previous behavior).
    /// - Parameter force: re-resolve every decision pitcher and REPLACE the
    ///   dicts, rather than fetching only the newly-decided ones and merging.
    ///   The non-live paths (`load`, `completeFinalize`) pass true so their
    ///   behaviour is exactly what it was before the live path became
    ///   incremental; live snapshots pass false.
    func loadPitcherRecords(force: Bool = false) async {
        guard let bs = boxScore else { return }
        guard let gameDate = Self.etDateString(from: game.startDate) else { return }
        var decisionBdlIds: Set<Int> = []
        for team in [bs.teams.away, bs.teams.home] {
            for (_, p) in team.players {
                guard let g = p.stats?.pitching else { continue }
                if (g.wins ?? 0) > 0 || (g.losses ?? 0) > 0 || (g.saves ?? 0) > 0 {
                    decisionBdlIds.insert(p.person.id)
                }
            }
        }
        guard !decisionBdlIds.isEmpty else { return }
        if force { pitcherRecordsRequested.removeAll() }
        let unfetched = decisionBdlIds.subtracting(pitcherRecordsRequested)
        guard !unfetched.isEmpty else { return }
        pitcherRecordsRequested.formUnion(unfetched)
        let triples = await withTaskGroup(
            of: (bdlId: Int, mlbam: Int, record: PitcherRecord)?.self,
        ) { group in
            for bdlId in unfetched {
                group.addTask { [bdl, api] in
                    guard let player = try? await bdl.resolveBDLPlayerId(bdlId)
                    else { return nil }
                    let mlbam = player.player_id
                    let outer = try? await api.getPitcherRecordAtDate(
                        playerId: mlbam, gameDate: gameDate,
                    )
                    guard let record = outer ?? nil else { return nil }
                    return (bdlId, mlbam, record)
                }
            }
            var out: [(Int, Int, PitcherRecord)] = []
            for await maybe in group {
                if let m = maybe { out.append(m) }
            }
            return out
        }
        // Merge, never replace: on a live tick `triples` holds only the pitchers
        // who just earned a decision, and the ones fetched earlier must survive.
        // A pitcher who LOSES his decision (a blown save reassigns the win) is
        // left in the dict harmlessly — `pitcherDecisionTag` gates on the live
        // box score's W/L/SV, so a record with no decision behind it renders
        // nothing. `force` starts from empty, reproducing the old replace.
        if force {
            self.pitcherRecords      = [:]
            self.pitcherRecordsByBDL = [:]
        }
        for (bdlId, mlbam, record) in triples {
            self.pitcherRecords[mlbam]    = record
            self.pitcherRecordsByBDL[bdlId] = record
        }
    }

    /// Sister to `loadPitcherRecords` — picks every batter with
    /// HR / 2B / 3B > 0 in this game and fetches their point-in-time
    /// totals from `/batter-stats-at-date`. Stored by BDL id so the
    /// view layer can read it without re-running the bdl→mlbam
    /// resolution per render.
    func loadBatterStatsAtDate() async {
        guard let bs = boxScore else { return }
        guard let gameDate = Self.etDateString(from: game.startDate) else { return }
        var notableBdlIds: Set<Int> = []
        for team in [bs.teams.away, bs.teams.home] {
            for (_, p) in team.players {
                guard let b = p.stats?.batting else { continue }
                if (b.homeRuns ?? 0) > 0
                    || (b.doubles  ?? 0) > 0
                    || (b.triples  ?? 0) > 0 {
                    notableBdlIds.insert(p.person.id)
                }
            }
        }
        // Fetch only players we haven't already resolved. This used to run ONCE,
        // on the first live snapshot, which was survivable while home runs were
        // the only thing on this line and mostly wrong even then: a batter who
        // homered in the 7th was never fetched, so his total rendered bare. Now
        // that doubles and triples reach the line too, the notable set grows
        // through the game, and a once-only fetch would leave most of them
        // without a total. Re-running per snapshot is cheap because this
        // difference is almost always empty.
        let unfetched = notableBdlIds.subtracting(batterStatsRequested)
        guard !unfetched.isEmpty else { return }
        batterStatsRequested.formUnion(unfetched)
        let pairs = await withTaskGroup(
            of: (Int, BatterStatsAtDate)?.self,
        ) { group in
            for bdlId in unfetched {
                group.addTask { [bdl, api] in
                    guard let player = try? await bdl.resolveBDLPlayerId(bdlId)
                    else { return nil }
                    let outer = try? await api.getBatterStatsAtDate(
                        playerId: player.player_id, gameDate: gameDate,
                    )
                    guard let stats = outer ?? nil else { return nil }
                    return (bdlId, stats)
                }
            }
            var out: [(Int, BatterStatsAtDate)] = []
            for await maybe in group {
                if let p = maybe { out.append(p) }
            }
            return out
        }
        // MERGE, not replace — each pass now carries only the newly-notable
        // players, so overwriting would drop everyone resolved earlier.
        for (bdlId, stats) in pairs { self.batterStatsAtDate[bdlId] = stats }
    }

    /// ET-anchored `yyyy-MM-dd` for the pitcher-record endpoint's
    /// `game_date` param. MLB schedules off Eastern, so a 10pm PT
    /// game has to file under its ET date or the gamelog scan
    /// returns the wrong day's games.
    private static func etDateString(from date: Date?) -> String? {
        guard let date else { return nil }
        return etDateFormatter.string(from: date)
    }

    private static let etDateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.calendar = .init(identifier: .gregorian)
        f.timeZone = TimeZone(identifier: "America/New_York") ?? .current
        f.locale   = .init(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()

    /// One-shot plays fetch for non-live games. Final games don't
    /// poll; the play stream is frozen, so a single call covers it.
    private func loadPlays() async {
        if let p = try? await bdl.getPlays(gameId: game.gamePk) {
            self.plays = p
        }
    }

    private func loadBoxScore() async {
        // Lineup and stats fetched in parallel via `async let` so a
        // slow lineup doesn't extend total latency. Lineup is
        // best-effort: `try?` collapses any failure to an empty
        // slice and the synthesizer falls back to BDL-response order
        // for the batting table. Stats is critical: without it we
        // have no rows to render, so a failure flips `error` (which
        // the view renders with a Retry button) and skips the rest.
        async let statsTask  = bdl.getGameStats(gameId: game.gamePk)
        async let lineupTask = bdl.getGameLineup(gameId: game.gamePk)
        let lineup = (try? await lineupTask) ?? []

        let stats: [BDLPlayerStat]
        do {
            stats = try await statsTask
        } catch {
            self.error = error.localizedDescription
            boxScore = nil
            return
        }

        // Season-stats fetch covers BOTH lineup ids AND any player
        // who shows up in the stats payload (relievers / pinch
        // hitters not in the lineup). Without the stats-side pids,
        // a reliever who earned today's decision shows "(W)" with
        // no record because their season W/L isn't fetched.
        // Failures degrade to "—" placeholders rather than
        // blocking the box score.
        let seasonStatsByPid = await loadSeasonStats(
            lineup: lineup, stats: stats,
        )

        // Resolve which BDL team object pairs with each side of
        // the game. When resolution fails (BDL team id not in our
        // hardcoded map, or stats payload lacks a usable nested
        // team — both happen periodically as BDL adds new ids),
        // we build stub BDLTeam objects from the Game's existing
        // TeamInfo so the box score still renders. Logos may
        // degrade but the batting/pitching tables work fine.
        let (awayBDL, homeBDL) = bdlTeams(forGame: game, fromStats: stats)
        boxScore = stats.toBoxScoreResponse(
            awayTeam:         awayBDL,
            homeTeam:         homeBDL,
            awayBDLTeamId:    game.bdlAwayTeamId,
            homeBDLTeamId:    game.bdlHomeTeamId,
            lineup:           lineup,
            seasonStatsByPid: seasonStatsByPid,
        )
        self.error = nil
    }

    /// Bulk-fetch season AVG / OPS / ERA / W-L-SV for every player
    /// who could appear in the box score: union of the starting
    /// lineup and every pid from the per-game stats payload. The
    /// stats-side union catches relievers and pinch hitters who
    /// aren't in the lineup but need their season W-L-SV to render
    /// a complete decision tag. Returns an empty dict on failure
    /// (placeholders fall back to "—" via the synthesizer's
    /// nil-coalesce).
    private func loadSeasonStats(
        lineup: [BDLGameLineup],
        stats:  [BDLPlayerStat],
    ) async -> [Int: BDLSeasonStat] {
        let ids = Array(Set(lineup.map(\.player.id))
                              .union(stats.map(\.player.id)))
        guard !ids.isEmpty else { return [:] }
        // BDL standings / season stats are season-keyed; use the
        // year the game was actually played in (game.startDate falls
        // back to "now" when BDL ships an unparseable date).
        let season = Calendar.current.component(.year, from: game.startDate ?? Date())
        do {
            let rows = try await bdl.getSeasonStats(playerIds: ids, season: season)
            return Dictionary(
                rows.map { ($0.player.id, $0) },
                uniquingKeysWith: { a, _ in a },
            )
        } catch {
            return [:]
        }
    }

    /// Re-derive `BDLTeam` objects for the two sides from the stats
    /// payload (each row carries its team's `BDLTeam` via the player
    /// nesting). When resolution fails for either side, falls back
    /// to a stub built from the game's existing `TeamInfo` so the
    /// synthesizer always has inputs and the box-score view never
    /// blocks on a team-lookup miss. Stubs lose the BDL `id` (set
    /// to 0) — logos go through the MLBAM bridge instead, so they
    /// still resolve via `Game.teams.{home,away}.team`.
    private func bdlTeams(
        forGame game: Game, fromStats stats: [BDLPlayerStat],
    ) -> (away: BDLTeam, home: BDLTeam) {
        // Try to pull from the stats nesting: any row's `player.team`
        // is the BDLTeam for that player's side this game.
        let byName: [String: BDLTeam] = Dictionary(
            stats.compactMap { s -> (String, BDLTeam)? in
                guard let t = s.player.team else { return nil }
                return (t.name, t)
            },
            uniquingKeysWith: { a, _ in a },
        )

        let awayName = game.teams.away.team.abbreviation ?? game.teams.away.team.name
        let homeName = game.teams.home.team.abbreviation ?? game.teams.home.team.name

        // Names ship differently depending on path — try a few
        // shapes (BDL `name` is the short franchise, `abbreviation`
        // is "NYY" etc., `displayName` is "New York Yankees").
        func resolve(_ ref: String, mlbId: Int) -> BDLTeam? {
            if let t = byName[ref] { return t }
            return byName.values.first { t in
                t.abbreviation == ref || t.displayName == ref || t.name == ref
            } ?? byName.values.first { t in
                mlbTeamId(forBDLId: t.id) == mlbId
            }
        }

        let away = resolve(awayName, mlbId: game.teams.away.team.id)
                   ?? Self.stubBDLTeam(from: game.teams.away.team)
        let home = resolve(homeName, mlbId: game.teams.home.team.id)
                   ?? Self.stubBDLTeam(from: game.teams.home.team)
        return (away, home)
    }

    /// Synthesize a minimal `BDLTeam` from a legacy `TeamInfo` for
    /// the fallback path above. The id is left at 0 (BDL doesn't
    /// know about this stub, by construction) — every other
    /// downstream consumer reads `name`/`displayName`/`abbreviation`,
    /// which we have, or hops through the MLBAM bridge for logos.
    private static func stubBDLTeam(from info: TeamInfo) -> BDLTeam {
        let abbr = info.abbreviation ?? String(info.name.prefix(3)).uppercased()
        return BDLTeam(
            id:                0,
            slug:              nil,
            abbreviation:      abbr,
            displayName:       info.name,
            shortDisplayName:  nil,
            name:              info.name,
            location:          "",
            league:            nil,
            division:          nil,
        )
    }

    /// Resolve a BDL-id-keyed player → our backend's PlayerSearchResult.
    /// Returns nil if the player isn't BDL-mapped in our DB (the
    /// bootstrap walk is still adding bdl_ids to historical rows);
    /// caller surfaces that as "Profile not available."
    func playerProfile(bdlId: Int) async -> PlayerSearchResult? {
        (try? await bdl.resolveBDLPlayerId(bdlId)) ?? nil
    }
}

struct BoxScoreView: View {
    @StateObject private var vm: BoxScoreViewModel
    /// BDL team id → `(rank, "AL East")` snapshot passed through
    /// from `ScoresView` so the header card can render the per-team
    /// "1st AL East" sub-label under each scoreboard cell. Plumbing
    /// the dict here keeps the box-score view's data flow obvious
    /// (no `@EnvironmentObject` indirection through the standings).
    let teamStandings: [Int: TeamStandingInfo]
    /// BDL team id → current-season W-L, same source the score
    /// cards use. Threaded in alongside `teamStandings` so the
    /// header sub-label reads "(21-27) • 3rd AL East".
    let teamRecords: [Int: TeamRecord]
    /// Parent (`ScoresView`) owns the NavigationStack path; we append
    /// to it when the user taps a player so the existing
    /// `.navigationDestination(for: PlayerSearchResult.self)` on
    /// ScoresView fires and pushes the profile view.
    @Binding var path: NavigationPath
    /// The tab this box score was pushed from. Live polling is gated on
    /// `navigation.shouldPoll(on: owningTab)` so it pauses when the user
    /// switches tabs or backgrounds the app, and resumes on return. Passed
    /// explicitly (with `navigation` below) rather than read from the
    /// environment so it survives the ScheduleSheet sheet boundary.
    let owningTab: AppNavigation.Tab
    /// Shared lifecycle/tab coordinator, injected explicitly (NOT via
    /// `@EnvironmentObject`) so it reaches this view identically whether it's
    /// pushed on a tab's NavigationStack or presented inside a sheet.
    @ObservedObject var navigation: AppNavigation
    /// Shared live-game store, also injected explicitly for the same sheet-
    /// boundary reason as `navigation`. For LIVE games the box score subscribes
    /// to `liveStore.detail[gamePk]` instead of running its own poll loop.
    @ObservedObject var liveStore: LiveGameStore
    /// Stable per-view identity for the store's refcounted detail subscription
    /// (Phase 2, step 5). Opening a box score OVER its Scores/Home card takes
    /// that game's refcount 1→2, sharing the existing loop.
    @State private var subscriberID = LiveGameStore.SubscriberID()
    @State private var pendingPlayerLookup: Int?
    @State private var navigationError: String?
    /// User override for the team-selector segmented control. nil
    /// → use `defaultSide` (home for final, offensive team for
    /// live). The actual rendered side is `currentSide`.
    @State private var selectedSide: TeamSide?

    /// Which team's batting + pitching table to render. The view
    /// shows one team at a time instead of stacking both — toggled
    /// via the segmented control at the top.
    enum TeamSide: String, Hashable, Identifiable, CaseIterable {
        case away, home
        var id: String { rawValue }
    }

    init(
        game: Game,
        teamStandings: [Int: TeamStandingInfo] = [:],
        teamRecords: [Int: TeamRecord] = [:],
        path: Binding<NavigationPath>,
        owningTab: AppNavigation.Tab,
        navigation: AppNavigation,
        liveStore: LiveGameStore,
    ) {
        _vm = StateObject(wrappedValue: BoxScoreViewModel(game: game))
        self.teamStandings = teamStandings
        self.teamRecords = teamRecords
        _path = path
        self.owningTab = owningTab
        self.navigation = navigation
        self.liveStore = liveStore
    }

    /// Whether the shared store currently considers this game live — the
    /// backend's definition (present in `liveList`), independent of the frozen
    /// initial `game.phase`. Drives the live subscription so a game opened
    /// pre-game that flips to live starts streaming (Option (b) of the fix).
    private var isLive: Bool {
        // Membership in `liveList` is the backend's definition of live, and it
        // is authoritative in BOTH directions — but only once the store has
        // actually answered.
        //
        // This used to be `liveList[pk] != nil || game.phase == .live`. The `||`
        // exists for a real case (a box score opened while the game is live but
        // before the store's first fetch lands), yet it also let a STALE phase
        // outvote a correct `liveList`: a game that had ended still read as live,
        // so the view subscribed to `/live/games/{id}`, got a 404, and never
        // loaded. The old comment claimed `beginFinalize` prevented that, but
        // that only runs for a box score already open when the game ends — not
        // one opened afterwards, which is the case that broke.
        //
        // Splitting on `listLoaded` keeps the pre-live case and drops the stale
        // one: before the first answer, trust the phase we were handed; after
        // it, trust the store and nothing else. The rule itself lives in
        // `LiveStatus` so it can be tested without standing up a view.
        LiveStatus.isLive(
            inLiveList:  liveStore.liveList[vm.game.gamePk] != nil,
            listLoaded:  liveStore.listLoaded,
            phaseIsLive: vm.game.phase == .live,
        )
    }

    /// Subscribe to the shared detail loop only while live AND this tab is
    /// visible / the app active.
    private var shouldSubscribeLive: Bool {
        isLive && navigation.shouldPoll(on: owningTab)
    }

    /// Render the live chrome (LIVE badge, inning label, situation card) once a
    /// real snapshot has been APPLIED (`vm.live != nil`) — not off the frozen
    /// `game.phase` — so the header / situation flip to live exactly when live
    /// data arrives, and fall back to `game` for final / pre-game.
    private var isLiveNow: Bool { vm.live != nil }

    /// Default-selected team when the user hasn't tapped the segmented control
    /// yet. Live → the batting side (top of inning → away is offense); else
    /// home (final / pre-game — a consistent, non-surprising default).
    private var defaultSide: TeamSide {
        if let ls = vm.live?.liveData.linescore {
            return (ls.isTopInning ?? false) ? .away : .home
        }
        return .home
    }

    private var currentSide: TeamSide {
        selectedSide ?? defaultSide
    }

    var body: some View {
        ScrollView {
            // Each card below applies its own `.glassEffect`. Grouping them in a
            // GlassEffectContainer renders the Liquid Glass in one pass so
            // adjacent cards don't independently sample the background and bleed
            // into each other at their facing edges (the header/situation
            // "overlap"). The 16pt container spacing matches the VStack spacing —
            // wider than any card gap, so distinct cards stay distinct rather
            // than morphing into one blob.
            GlassEffectContainer(spacing: 16) {
                VStack(spacing: 16) {
                    headerCard
                    if let live = vm.live?.liveData {
                        liveSituationCard(live)
                    }
                    if vm.game.phase == .postponed {
                        postponedNotice
                    } else if let bs = vm.boxScore {
                        linescoreCard
                        teamPicker(bs: bs)
                        teamSection(side: currentSide, bs: bs)
                    } else if vm.isLoading {
                        ProgressView().controlSize(.large)
                            .frame(maxWidth: .infinity, minHeight: 120)
                    } else if let error = vm.error {
                        VStack(spacing: 12) {
                            Text(error)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .multilineTextAlignment(.center)
                                .padding(.horizontal, 24)
                            Button("Retry") {
                                Task { await vm.load() }
                            }
                            .buttonStyle(.borderedProminent)
                            .controlSize(.small)
                        }
                    }
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 16)
            }
        }
        .navigationTitle(navigationTitle)
        .navigationBarTitleDisplayMode(.inline)
        .task {
            await vm.load()
            guard shouldSubscribeLive else { return }   // final / pre-game (not in live set): no subscription
            // Seed from whatever the store already has — a box score usually
            // opens OVER a Scores/Home card already subscribed to this game, so
            // detail is present and renders instantly (no wait for the next tick).
            if let detail = liveStore.detail[vm.game.gamePk] {
                vm.applyLiveDetail(detail)
            }
            // Subscribe (immediate: true). Over a card already watching this game
            // it's refcount 1→2 sharing one loop; a fresh open (e.g. ScheduleSheet)
            // is 0→1, which the store always leads with a fetch.
            liveStore.subscribeDetail(vm.game.gamePk, owner: subscriberID, immediate: true)
        }
        // Feed each fresh store snapshot into the view model (drives the banner /
        // linescore / situation / plays, all from one consistent LiveGameDetail).
        // This also catches the pre-game→live transition: when a card (or our own
        // subscribe below) first populates detail for this game, it applies here.
        .onChange(of: liveStore.detail[vm.game.gamePk]) { _, detail in
            if let detail { vm.applyLiveDetail(detail) }
        }
        // Subscribe/unsubscribe as liveness + visibility change. A game that
        // flips to live while open (enters the store's live set) subscribes here;
        // hiding the tab / backgrounding unsubscribes. Idempotent + refcounted,
        // so it never stops a loop a card still needs.
        .onChange(of: shouldSubscribeLive) { _, canPoll in
            if canPoll {
                if let detail = liveStore.detail[vm.game.gamePk] {
                    vm.applyLiveDetail(detail)
                }
                liveStore.subscribeDetail(vm.game.gamePk, owner: subscriberID, immediate: true)
            } else {
                liveStore.unsubscribeDetail(vm.game.gamePk, owner: subscriberID)
            }
        }
        // END transition: the game left the store's live set while we were
        // streaming it. `beginFinalize()` (SYNCHRONOUS, this same actor turn)
        // folds the last snapshot into a coherent `.final` game and clears
        // `vm.live`, so every display path resolves to final on the very next
        // frame — no stale-mid-game flash. `completeFinalize()` then refetches
        // the authoritative final box score / plays and reconciles the score.
        // Idempotent without a latch: after `beginFinalize` `vm.live` is nil so
        // this guard blocks re-entry, and a final game never re-enters `liveList`.
        .onChange(of: liveStore.liveList[vm.game.gamePk] != nil) { wasInList, isInList in
            if wasInList, !isInList, vm.live != nil {
                liveStore.unsubscribeDetail(vm.game.gamePk, owner: subscriberID)
                vm.beginFinalize()
                Task { await vm.completeFinalize() }
            }
        }
        .onDisappear { liveStore.unsubscribeDetail(vm.game.gamePk, owner: subscriberID) }
        .overlay(alignment: .top) {
            VStack(spacing: 6) {
                if pendingPlayerLookup != nil {
                    ProgressView()
                        .controlSize(.small)
                        .padding(8)
                        .background(.ultraThinMaterial, in: Capsule())
                }
                if let navigationError {
                    Text(navigationError)
                        .font(.caption.weight(.medium))
                        .foregroundStyle(.primary)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 8)
                        .background(.ultraThinMaterial, in: Capsule())
                        .overlay(
                            Capsule().stroke(Color(.separator).opacity(0.4), lineWidth: 0.5)
                        )
                        .shadow(color: .black.opacity(0.08), radius: 6, x: 0, y: 2)
                        .transition(.move(edge: .top).combined(with: .opacity))
                }
            }
            .padding(.top, 8)
            .animation(.easeInOut(duration: 0.2), value: navigationError)
        }
    }

    private var navigationTitle: String {
        let away = vm.game.teams.away.team.abbreviation
            ?? String(vm.game.teams.away.team.name.prefix(3)).uppercased()
        let home = vm.game.teams.home.team.abbreviation
            ?? String(vm.game.teams.home.team.name.prefix(3)).uppercased()
        return "\(away) @ \(home)"
    }

    // MARK: - Header

    private var headerCard: some View {
        VStack(spacing: 10) {
            // LIVE badge sits centered above the score row so it
            // owns the headline visual; the team-by-score row stays
            // symmetric below.
            if isLiveNow {
                LiveBadge()
            }
            HStack(spacing: 12) {
                teamHeader(
                    side:      vm.game.teams.away,
                    score:     vm.displayAwayScore,
                    bdlTeamId: vm.game.bdlAwayTeamId,
                )
                Spacer()
                Text(centerStatus)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(isLiveNow ? Color.red : Color.secondary)
                Spacer()
                teamHeader(
                    side:      vm.game.teams.home,
                    score:     vm.displayHomeScore,
                    bdlTeamId: vm.game.bdlHomeTeamId,
                )
            }
            if let venue = vm.game.venue?.name {
                Text(venue)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 14)
        .frame(maxWidth: .infinity)
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 16))
        .shadow(color: .black.opacity(0.06), radius: 8, x: 0, y: 3)
    }

    /// Live in-game card — surfaces the current matchup, base
    /// runners, and count. Only rendered while `phase == .live`
    /// (the view-body branch is already gated on that), so we can
    /// freely assume the linescore has live state.
    private func liveSituationCard(_ live: LiveData) -> some View {
        let ls = live.linescore
        let play = live.plays?.currentPlay
        let batter = play?.matchup?.batter ?? ls?.offense?.batter
        let pitcher = play?.matchup?.pitcher ?? ls?.defense?.pitcher
        let balls = play?.count?.balls ?? ls?.balls ?? 0
        let strikes = play?.count?.strikes ?? ls?.strikes ?? 0
        let outs = play?.count?.outs ?? ls?.outs ?? 0
        let inningArrow = (ls?.isTopInning).map { $0 ? "▲" : "▼" } ?? ""
        let inningOrd = ls?.currentInningOrdinal ?? "—"
        return VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Text("\(inningArrow) \(inningOrd)")
                    .font(.subheadline.weight(.bold))
                    .foregroundStyle(.red)
                    .monospacedDigit()
                Spacer()
                Text("\(balls)-\(strikes) · \(outs) out\(outs == 1 ? "" : "s")")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
            HStack(alignment: .center, spacing: 16) {
                BaseRunnerView(
                    first:  ls?.offense?.first  != nil,
                    second: ls?.offense?.second != nil,
                    third:  ls?.offense?.third  != nil,
                    size: 44
                )
                VStack(alignment: .leading, spacing: 4) {
                    if let pitcher {
                        Text("Pitching: \(pitcher.fullName)")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                    if let batter {
                        Text("Batting: \(batter.fullName)")
                            .font(.caption.weight(.semibold))
                            .lineLimit(1)
                    }
                }
                Spacer(minLength: 0)
            }
            if let desc = lastPlayDescription(play) {
                Text(desc)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            Divider().opacity(0.4)
            PlaysView(
                plays:               vm.plays,
                awayAbbr:            teamAbbr(vm.game.teams.away.team),
                homeAbbr:            teamAbbr(vm.game.teams.home.team),
                autoExpandOnScoring: true,
                isEmbedded:          true,
            )
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 14)
        .frame(maxWidth: .infinity)
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 16))
        .shadow(color: .black.opacity(0.06), radius: 8, x: 0, y: 3)
    }

    /// Prefer the resolved PA description; fall back to the last
    /// individual pitch event's description for mid-PA states.
    private func lastPlayDescription(_ play: LivePlay?) -> String? {
        if let desc = play?.result?.description, !desc.isEmpty { return desc }
        return play?.playEvents?.compactMap(\.details?.description).last
    }

    private func teamHeader(side: GameTeam, score: Int?, bdlTeamId: Int?) -> some View {
        // Two stacked sub-lines under the score:
        //   line 1: "(21-27)"  — current-season W-L
        //   line 2: "3rd AL East" — division rank label
        // Each segment is independently optional; missing pieces
        // collapse without leaving an empty row.
        let recordText: String? = {
            guard let r = bdlTeamId.flatMap({ teamRecords[$0] }),
                  let w = r.wins, let l = r.losses else { return nil }
            return "(\(w)-\(l))"
        }()
        let standingText: String? = bdlTeamId
            .flatMap { teamStandings[$0] }
            .map { $0.displayString }
        return VStack(spacing: 2) {
            // A 56pt circle used to head this column. The swatch does not
            // simply take its place — a 5pt bar alone in a stack under a
            // .title score reads as a stray tick — so it moves onto the
            // abbreviation's line and leads it, as everywhere else. 16pt to
            // match .subheadline.bold rather than the 18 used against larger text.
            HStack(spacing: 5) {
                TeamColorSwatch(team: side.team, height: 16)
                Text(side.team.abbreviation ?? String(side.team.name.prefix(3)).uppercased())
                    .font(.subheadline.weight(.bold))
            }
            Text(score.map(String.init) ?? "—")
                .font(.title.weight(.bold))
                .monospacedDigit()
                .padding(.bottom, 2)
            if let recordText {
                Text(recordText)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
                    .lineLimit(1)
            }
            if let standingText {
                Text(standingText)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
    }

    private var centerStatus: String {
        // Live-now wins over the frozen schedule phase, so a game that flipped
        // to live while open shows "LIVE · <inning>" instead of its start time.
        if isLiveNow {
            return vm.displayInningOrdinal.map { "LIVE · \($0)" } ?? "LIVE"
        }
        switch vm.game.phase {
        case .final:     return "FINAL"
        case .live:      return vm.displayInningOrdinal.map { "LIVE · \($0)" } ?? "LIVE"
        case .postponed: return "POSTPONED"
        case .preview:   return vm.game.startDate.map { Self.timeFormatter.string(from: $0) } ?? "SCHEDULED"
        case .other:     return vm.game.status.detailedState.uppercased()
        }
    }

    /// Shown in place of the linescore / box-score tables when a game
    /// has been postponed — there are no stats to render, so a clear
    /// notice reads better than an empty "Retry" error state.
    private var postponedNotice: some View {
        VStack(spacing: 10) {
            Image(systemName: "calendar.badge.exclamationmark")
                .font(.largeTitle)
                .foregroundStyle(.orange)
            Text("Game Postponed")
                .font(.headline)
            Text("This game won't be played as scheduled. Check back for a makeup date.")
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 24)
        }
        .frame(maxWidth: .infinity, minHeight: 160)
    }

    // MARK: - Linescore

    private var linescoreCard: some View {
        let innings = vm.displayLinescore?.innings ?? []
        let totals = vm.displayLinescore?.teams
        let inningCount = max(innings.count, vm.displayLinescore?.scheduledInnings ?? 9)
        return VStack(alignment: .leading, spacing: 8) {
            Text("Linescore").font(.headline)
            ScrollView(.horizontal, showsIndicators: false) {
                VStack(alignment: .leading, spacing: 4) {
                    linescoreRow(label: "", cells: (1...inningCount).map { String($0) },
                                 totals: ["R", "H", "E"], bold: true, secondary: true)
                    Divider()
                    linescoreRow(
                        label: vm.game.teams.away.team.abbreviation ?? "AWAY",
                        cells: (1...inningCount).map { i in
                            cellValue(innings.first(where: { $0.num == i })?.away?.runs)
                        },
                        totals: [
                            cellValue(totals?.away?.runs),
                            cellValue(totals?.away?.hits),
                            cellValue(totals?.away?.errors),
                        ],
                        bold: false, secondary: false
                    )
                    linescoreRow(
                        label: vm.game.teams.home.team.abbreviation ?? "HOME",
                        cells: (1...inningCount).map { i in
                            cellValue(innings.first(where: { $0.num == i })?.home?.runs)
                        },
                        totals: [
                            cellValue(totals?.home?.runs),
                            cellValue(totals?.home?.hits),
                            cellValue(totals?.home?.errors),
                        ],
                        bold: false, secondary: false
                    )
                }
                .padding(.horizontal, 4)
            }
            // Live games render the plays expander inside the situation card
            // above this one; final / preview games get it here so it's always
            // reachable. Keyed off `isLiveNow` (an applied snapshot), so a game
            // that transitioned to live doesn't render the plays list twice.
            if !isLiveNow {
                Divider().opacity(0.4)
                PlaysView(
                    plays:               vm.plays,
                    awayAbbr:            teamAbbr(vm.game.teams.away.team),
                    homeAbbr:            teamAbbr(vm.game.teams.home.team),
                    autoExpandOnScoring: false,
                    isEmbedded:          true,
                )
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 16))
        .shadow(color: .black.opacity(0.06), radius: 8, x: 0, y: 3)
    }

    private func linescoreRow(label: String, cells: [String], totals: [String],
                              bold: Bool, secondary: Bool) -> some View {
        HStack(spacing: 0) {
            Text(label)
                .font(.caption.weight(bold ? .bold : .semibold))
                .frame(width: 50, alignment: .leading)
            ForEach(cells.indices, id: \.self) { i in
                Text(cells[i])
                    .font(.caption.weight(bold ? .bold : .regular))
                    .frame(width: 22, alignment: .trailing)
                    .monospacedDigit()
            }
            Spacer().frame(width: 8)
            ForEach(totals.indices, id: \.self) { i in
                Text(totals[i])
                    .font(.caption.weight(.bold))
                    .frame(width: 22, alignment: .trailing)
                    .monospacedDigit()
            }
        }
        .foregroundStyle(secondary ? Color.secondary : Color.primary)
    }

    private func cellValue(_ v: Int?) -> String {
        guard let v else { return "-" }
        return String(v)
    }

    // MARK: - Per-team batting + pitching

    /// Segmented control sitting above the per-team box-score
    /// tables. Labels show each team's full short name so the
    /// active side reads cleanly at a glance; the segmented style
    /// matches the Recent Games window picker on the player
    /// profile so the control feels like part of the same family.
    private func teamPicker(bs: BoxScoreResponse) -> some View {
        Picker(
            "Team",
            selection: Binding(
                get: { currentSide },
                set: { selectedSide = $0 }
            )
        ) {
            Text(bs.teams.away.team.name).tag(TeamSide.away)
            Text(bs.teams.home.team.name).tag(TeamSide.home)
        }
        .pickerStyle(.segmented)
    }

    private func teamSection(side: TeamSide, bs: BoxScoreResponse) -> some View {
        let team = side == .away ? bs.teams.away : bs.teams.home
        return VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                // Leads the club's full name, so 18pt against .headline.
                TeamColorSwatch(team: team.team)
                Text(team.team.name).font(.headline)
            }
            battingTable(team: team)
            pitchingTable(team: team)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 16))
        .shadow(color: .black.opacity(0.06), radius: 8, x: 0, y: 3)
    }

    private func battingTable(team: BoxScoreTeam) -> some View {
        let rows = team.batters.compactMap { id -> BoxPlayer? in
            team.players["ID\(id)"]
        }.filter { $0.stats?.batting?.atBats != nil || ($0.stats?.batting?.baseOnBalls ?? 0) > 0 }
        let tint = teamColor(for: team)
        // Live-feed batter id is BDL-keyed; lineup row ids
        // (`p.person.id`) are also BDL-keyed (see Scores.swift
        // synth), so a direct equality match in `battingRow` lands
        // on the active hitter. Only meaningful while the game is
        // live — when nil, every row falls through to the default
        // (non-highlighted) branch.
        let currentBatterId = vm.live?.liveData.linescore?.offense?.batter?.id

        return VStack(alignment: .leading, spacing: 4) {
            Text("BATTING").font(.caption.weight(.bold)).foregroundStyle(.secondary)
            ScrollView(.horizontal, showsIndicators: false) {
                VStack(alignment: .leading, spacing: 2) {
                    battingHeader
                    Divider().opacity(0.4)
                    ForEach(rows, id: \.person.id) { player in
                        battingRow(
                            player, tint: tint, currentBatterId: currentBatterId,
                        )
                    }
                    if !rows.isEmpty {
                        Divider().opacity(0.6)
                        battingTotalsRow(rows: rows)
                    }
                }
            }
            notableBlock(rows: rows)
        }
    }

    /// Resolve a `BoxScoreTeam`'s `TeamColors` tint. Matches by team
    /// abbreviation against the game's away/home sides — the box-
    /// score's MLBAM `team.id` ships as 0 on the BDL-synthesized
    /// shape, so an id comparison would never resolve. Once a side
    /// is identified, we bridge through `bdl{Away,Home}TeamId` →
    /// Lahman → `TeamColors`. Falls back to `.accentColor` whenever
    /// any hop misses (no abbreviation on the payload, an unmapped
    /// BDL id, or a Lahman gap for a relocated franchise).
    private func teamColor(for team: BoxScoreTeam) -> Color {
        guard let abbr = team.team.abbreviation?.uppercased(),
              !abbr.isEmpty else {
            return .accentColor
        }
        let bdlId: Int?
        if vm.game.teams.away.team.abbreviation?.uppercased() == abbr {
            bdlId = vm.game.bdlAwayTeamId
        } else if vm.game.teams.home.team.abbreviation?.uppercased() == abbr {
            bdlId = vm.game.bdlHomeTeamId
        } else {
            bdlId = nil
        }
        guard let bdlId, let lahman = bdlToLahmanTeamId[bdlId] else {
            return .accentColor
        }
        return TeamColors.color(for: lahman) ?? .accentColor
    }

    /// Team totals row anchoring the bottom of the batting table.
    /// Counting-only — AVG and OPS are intentionally left "—".
    /// A "team AVG" from row totals is rarely the number you want
    /// (it weights the leadoff hitter's 5 PAs the same as the 9-hole
    /// hitter's 4), and team OPS needs OBP/SLG components the
    /// table's compact columns don't surface.
    private func battingTotalsRow(rows: [BoxPlayer]) -> some View {
        var AB = 0, R = 0, H = 0, RBI = 0, BB = 0, SO = 0
        for p in rows {
            guard let b = p.stats?.batting else { continue }
            AB  += b.atBats      ?? 0
            R   += b.runs        ?? 0
            H   += b.hits        ?? 0
            RBI += b.rbi         ?? 0
            BB  += b.baseOnBalls ?? 0
            SO  += b.strikeOuts  ?? 0
        }
        return HStack(spacing: 0) {
            Text("Totals")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
                .frame(width: BattingCol.name, alignment: .leading)
            totalsCell(AB,  width: BattingCol.ab)
            totalsCell(R,   width: BattingCol.r)
            totalsCell(H,   width: BattingCol.h)
            totalsCell(RBI, width: BattingCol.rbi)
            totalsCell(BB,  width: BattingCol.bb)
            totalsCell(SO,  width: BattingCol.so)
            // AVG / OPS deliberately blank — a row-sum team rate
            // weights every PA equally and rarely matches expectations.
            Text("")
                .frame(width: BattingCol.avg, alignment: .trailing)
            Text("")
                .frame(width: BattingCol.ops, alignment: .trailing)
        }
        .padding(.vertical, 2)
    }

    /// Per-column widths for the batting table. Kept in one place
    /// so header + row stay aligned and the total stays under the
    /// iPhone safe-width — `nameCol + sum(statCols)` should clear
    /// ~360pt with room for safe-area padding so OPS lands without
    /// horizontal scrolling.
    private enum BattingCol {
        static let name: CGFloat = 110
        static let ab:   CGFloat = 24
        static let r:    CGFloat = 24
        static let h:    CGFloat = 24
        static let rbi:  CGFloat = 28
        static let bb:   CGFloat = 24
        static let so:   CGFloat = 24
        static let avg:  CGFloat = 34
        static let ops:  CGFloat = 34
    }

    private var battingHeader: some View {
        HStack(spacing: 0) {
            Text("").frame(width: BattingCol.name, alignment: .leading)
            battingHeaderCell("AB",  width: BattingCol.ab)
            battingHeaderCell("R",   width: BattingCol.r)
            battingHeaderCell("H",   width: BattingCol.h)
            battingHeaderCell("RBI", width: BattingCol.rbi)
            battingHeaderCell("BB",  width: BattingCol.bb)
            battingHeaderCell("SO",  width: BattingCol.so)
            battingHeaderCell("AVG", width: BattingCol.avg)
            battingHeaderCell("OPS", width: BattingCol.ops)
        }
    }

    private func battingHeaderCell(_ label: String, width: CGFloat) -> some View {
        Text(label)
            .font(.caption2.weight(.bold))
            .foregroundStyle(.secondary)
            .frame(width: width, alignment: .trailing)
            .monospacedDigit()
    }

    private func battingRow(
        _ p: BoxPlayer, tint: Color, currentBatterId: Int?,
    ) -> some View {
        let b = p.stats?.batting
        let avg = p.seasonStats?.batting?.avg ?? "—"
        let ops = p.seasonStats?.batting?.ops ?? "—"
        let isCurrent = currentBatterId != nil && p.person.id == currentBatterId
        return Button { tapPlayer(id: p.person.id, name: p.person.fullName, isPitcher: false) } label: {
            HStack(spacing: 0) {
                HStack(spacing: 4) {
                    if isCurrent {
                        Image(systemName: "figure.baseball")
                            .font(.caption2)
                            .foregroundStyle(tint)
                    }
                    playerLabel(p, isPitcher: false)
                }
                .frame(width: BattingCol.name, alignment: .leading)
                cell(b?.atBats,      width: BattingCol.ab)
                cell(b?.runs,        width: BattingCol.r)
                cell(b?.hits,        width: BattingCol.h)
                cell(b?.rbi,         width: BattingCol.rbi)
                cell(b?.baseOnBalls, width: BattingCol.bb)
                cell(b?.strikeOuts,  width: BattingCol.so)
                Text(avg)
                    .font(.caption)
                    .monospacedDigit()
                    .lineLimit(1)
                    .frame(width: BattingCol.avg, alignment: .trailing)
                // AVG never exceeds .999 (".399" / ".305" — 4 chars
                // with the dropped leading zero), so no scaling. OPS
                // can break 1.000 (".850" → "1.023" — 5 chars), so
                // we shrink only the 1.xxx case — keeps sub-1.000
                // values rendering at full size next to AVG.
                Text(ops)
                    .font(.caption)
                    .monospacedDigit()
                    .lineLimit(1)
                    .minimumScaleFactor(ops.hasPrefix("1.") ? 0.85 : 1.0)
                    .frame(width: BattingCol.ops, alignment: .trailing)
            }
            .padding(.vertical, 2)
            .bold(isCurrent)
            .background(
                isCurrent ? tint.opacity(0.15) : Color.clear,
                in: RoundedRectangle(cornerRadius: 6),
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    /// "Notable" highlights below the batting table — 2B / 3B / HR
    /// per category, only rendered when at least one batter on the
    /// team logged the outcome this game. Season totals come from
    /// `seasonStats.batting.{doubles,triples,homeRuns}` so the line
    /// reads as "Acuña Jr. (12)" — the parenthetical year-to-date
    /// count gives the user instant context without leaving the
    /// box score. Multiple players with the same outcome are
    /// comma-joined, ordered by their batting-order appearance.
    @ViewBuilder
    private func notableBlock(rows: [BoxPlayer]) -> some View {
        let doubles  = rows.filter { ($0.stats?.batting?.doubles  ?? 0) > 0 }
        let triples  = rows.filter { ($0.stats?.batting?.triples  ?? 0) > 0 }
        let homeRuns = rows.filter { ($0.stats?.batting?.homeRuns ?? 0) > 0 }
        if !doubles.isEmpty || !triples.isEmpty || !homeRuns.isEmpty {
            VStack(alignment: .leading, spacing: 2) {
                if !doubles.isEmpty {
                    notableLine(label: "2B", players: doubles, totalKey: \.doubles)
                }
                if !triples.isEmpty {
                    notableLine(label: "3B", players: triples, totalKey: \.triples)
                }
                if !homeRuns.isEmpty {
                    notableLine(label: "HR", players: homeRuns, totalKey: \.homeRuns)
                }
            }
            .padding(.top, 4)
        }
    }

    private func notableLine(
        label: String,
        players: [BoxPlayer],
        totalKey: KeyPath<BoxBatting, Int?>,
    ) -> some View {
        // `seasonStats.batting.{homeRuns,doubles,triples}` is the
        // PRE-game cumulative loaded once from BDL's `/season_stats`
        // at box-score open. Per-game `stats.batting.{…}` is just
        // this game's count. When the game is today's slate the
        // box-score header should read the LIVE post-game total
        // (pre-game season + this-game count); the placeholder
        // value alone goes stale the instant the player connects.
        // For historical games we trust the pre-game value as
        // already post-game on BDL's side and don't double-count.
        //
        // The per-game count also doubles as a multi-occurrence
        // prefix when > 1 ("Alvarez 2 (21)") so a 2-HR night stays
        // visible at a glance.
        let pieces: [String] = players.map { bp in
            let last = lastName(bp.person.fullName)
            let gameCount = bp.stats?.batting?[keyPath: totalKey] ?? 0
            let prefix = gameCount > 1 ? "\(last) \(gameCount)" : last
            // Prefer the contextual total from
            // `/players/{id}/batter-stats-at-date` — counts gamelog
            // rows up to this game's date. Bump only when the game
            // is today AND the log hasn't absorbed today yet
            // (`includesToday=false`), mirroring the pitcher path.
            if let stats = vm.batterStatsAtDate[bp.person.id] {
                let atDate: Int
                if      totalKey == \BoxBatting.homeRuns { atDate = stats.homeRuns }
                else if totalKey == \BoxBatting.doubles  { atDate = stats.doubles }
                else if totalKey == \BoxBatting.triples  { atDate = stats.triples }
                else                                     { atDate = 0 }
                let bump = !stats.includesToday ? gameCount : 0
                return "\(prefix) (\(atDate + bump))"
            }
            let liveIncrement = gameCount
            guard let season = bp.seasonStats?.batting?[keyPath: totalKey] else {
                return prefix
            }
            return "\(prefix) (\(season + liveIncrement))"
        }
        return (Text("\(label): ").font(.caption2.weight(.bold))
                + Text(pieces.joined(separator: ", ")).font(.caption2))
            .foregroundStyle(.secondary)
            .lineLimit(2)
            .fixedSize(horizontal: false, vertical: true)
    }

    /// True iff `vm.game.startDate` falls on today's ET-local
    /// calendar day. MLB schedules its slate off Eastern, so the
    /// gate has to anchor there — comparing against the device's
    /// local timezone would put a 10pm PT first-pitch on the wrong
    /// day half the time.
    private var isGameToday: Bool {
        guard let start = vm.game.startDate else { return false }
        let f = Self.etDateFormatter
        return f.string(from: start) == f.string(from: Date())
    }

    private static let etDateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.calendar = .init(identifier: .gregorian)
        f.timeZone = TimeZone(identifier: "America/New_York") ?? .current
        f.locale   = .init(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()

    /// "Acuña Jr." — keep the last token of a hyphenated/multi-word
    /// surname (handles Vladimir Guerrero Jr., Ronald Acuña Jr.,
    /// J.D. Martinez). Mirrors the `lastName` helper used in the
    /// decisions row on Final game cards.
    private func lastName(_ full: String) -> String {
        full.split(separator: " ").last.map(String.init) ?? full
    }

    /// Per-column widths for the pitching table. Name column matches
    /// `BattingCol.name` so the IP column starts at the same x as
    /// AB in the batting table — both tables read as a single wide
    /// scoreboard. Stat-column widths are tuned to common values
    /// (IP "10.2", ERA "12.34", PC "123") at caption-monospaced.
    private enum PitchingCol {
        static let name: CGFloat = BattingCol.name  // 110
        static let ip:   CGFloat = 34
        static let h:    CGFloat = 22
        static let r:    CGFloat = 22
        static let er:   CGFloat = 22
        static let bb:   CGFloat = 22
        static let so:   CGFloat = 22
        static let era:  CGFloat = 38
        static let pc:   CGFloat = 28
    }

    private func pitchingTable(team: BoxScoreTeam) -> some View {
        let rows = team.pitchers.compactMap { id -> BoxPlayer? in
            team.players["ID\(id)"]
        }
        return VStack(alignment: .leading, spacing: 4) {
            Text("PITCHING").font(.caption.weight(.bold)).foregroundStyle(.secondary)
            ScrollView(.horizontal, showsIndicators: false) {
                VStack(alignment: .leading, spacing: 2) {
                    pitchingHeader
                    Divider().opacity(0.4)
                    ForEach(rows, id: \.person.id) { player in
                        pitchingRow(player)
                    }
                    if !rows.isEmpty {
                        Divider().opacity(0.6)
                        pitchingTotalsRow(rows: rows)
                    }
                }
            }
        }
    }

    private var pitchingHeader: some View {
        HStack(spacing: 0) {
            Text("").frame(width: PitchingCol.name, alignment: .leading)
            pitchingHeaderCell("IP",  width: PitchingCol.ip)
            pitchingHeaderCell("H",   width: PitchingCol.h)
            pitchingHeaderCell("R",   width: PitchingCol.r)
            pitchingHeaderCell("ER",  width: PitchingCol.er)
            pitchingHeaderCell("BB",  width: PitchingCol.bb)
            pitchingHeaderCell("SO",  width: PitchingCol.so)
            pitchingHeaderCell("ERA", width: PitchingCol.era)
            pitchingHeaderCell("PC",  width: PitchingCol.pc)
        }
    }

    private func pitchingHeaderCell(_ label: String, width: CGFloat) -> some View {
        Text(label)
            .font(.caption2.weight(.bold))
            .foregroundStyle(.secondary)
            .frame(width: width, alignment: .trailing)
            .monospacedDigit()
    }

    private func pitchingRow(_ p: BoxPlayer) -> some View {
        let pit = p.stats?.pitching
        let era = p.seasonStats?.pitching?.era ?? "—"
        let decisionTag = pitcherDecisionTag(for: p)
        return Button { tapPlayer(id: p.person.id, name: p.person.fullName, isPitcher: true) } label: {
            HStack(spacing: 0) {
                pitcherLabel(p, decisionTag: decisionTag)
                    .frame(width: PitchingCol.name, alignment: .leading)
                Text(Self.formatPitcherIP(pit?.inningsPitched))
                    .font(.caption).monospacedDigit()
                    .frame(width: PitchingCol.ip, alignment: .trailing)
                cell(pit?.hits,        width: PitchingCol.h)
                cell(pit?.runs,        width: PitchingCol.r)
                cell(pit?.earnedRuns,  width: PitchingCol.er)
                cell(pit?.baseOnBalls, width: PitchingCol.bb)
                cell(pit?.strikeOuts,  width: PitchingCol.so)
                Text(era).font(.caption).monospacedDigit()
                    .frame(width: PitchingCol.era, alignment: .trailing)
                cell(pit?.pitchCount,  width: PitchingCol.pc)
            }
            .padding(.vertical, 2)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    /// "5.2" → "5.2"; "1" → "1.0"; "" → "-". Box-score IPs lacking
    /// a decimal (zero-out outings, or BDL shipping a bare whole
    /// number) get a ".0" tacked on for visual consistency.
    private static func formatPitcherIP(_ s: String?) -> String {
        guard let s, !s.isEmpty else { return "-" }
        return s.contains(".") ? s : "\(s).0"
    }

    /// Pitcher name + position with the game decision tag inline:
    /// "C. Sale (W 8-3)" / "G. Cole (L 5-4)" / "K. Jansen (SV 12)".
    /// The decision flag (`stats.pitching.wins == 1` etc.) is on the
    /// per-game row; the season record (`seasonStats.pitching.wins`)
    /// rides along thanks to the field-aware merge in Scores.swift.
    private func pitcherLabel(_ p: BoxPlayer, decisionTag: String?) -> some View {
        HStack(spacing: 4) {
            Text(shortName(p.person.fullName))
                .font(.caption.weight(.medium))
                .lineLimit(1)
            if let tag = decisionTag {
                Text(tag)
                    .font(.caption2.weight(.bold))
                    .foregroundStyle(.secondary)
            }
        }
    }

    /// Returns "W 8-3" / "L 5-4" / "SV 12" when this pitcher earned
    /// today's decision; nil otherwise. Per-game decision flags
    /// (`stats.pitching.wins/losses/saves`) ship as 0/1 from BDL.
    ///
    /// `seasonStats.pitching` is the PRE-game W-L-SV pulled once from
    /// BDL's `/season_stats` at box-score open. For today's slate
    /// the displayed line has to fold today's decision in — a W
    /// reads as "(W 9-3)" within seconds of the final out instead
    /// of staying at "(W 8-3)" until BDL re-publishes. Historical
    /// games trust the placeholder as already post-game and don't
    /// double-count.
    private func pitcherDecisionTag(for p: BoxPlayer) -> String? {
        let game = p.stats?.pitching
        // Prefer the contextual record from
        // `/players/{id}/pitcher-record-at-date` — that endpoint
        // counts gamelog rows up to and including this game's date,
        // so it always reflects the player's true post-game W-L-SV
        // regardless of whether BDL has absorbed today into its
        // season-stats snapshot. Falls through to the placeholder
        // + isGameToday bump when the resolve/fetch hasn't landed.
        if let rec = vm.pitcherRecordsByBDL[p.person.id] {
            // Bump only when the game is today AND our log scan
            // didn't already absorb today's row. `includesToday`
            // tracks the latter; flip the bump off as soon as the
            // catch-up writes today's gamelog row so the displayed
            // record doesn't double-count this decision.
            let bump = !rec.includesToday ? 1 : 0
            if (game?.wins ?? 0) > 0 {
                return "(W \(rec.wins + bump)-\(rec.losses))"
            }
            if (game?.losses ?? 0) > 0 {
                return "(L \(rec.wins)-\(rec.losses + bump))"
            }
            if (game?.saves ?? 0) > 0 {
                return "(SV \(rec.saves + bump))"
            }
            return nil
        }
        let season = p.seasonStats?.pitching
        let bump = 1
        if (game?.wins ?? 0) > 0 {
            if let w = season?.wins, let l = season?.losses {
                return "(W \(w + bump)-\(l))"
            }
            return "(W)"
        }
        if (game?.losses ?? 0) > 0 {
            if let w = season?.wins, let l = season?.losses {
                return "(L \(w)-\(l + bump))"
            }
            return "(L)"
        }
        if (game?.saves ?? 0) > 0 {
            if let sv = season?.saves {
                return "(SV \(sv + bump))"
            }
            return "(SV)"
        }
        return nil
    }

    /// Team totals row anchoring the bottom of the pitching table.
    /// Counting-only — ERA is intentionally left "—". A row-sum
    /// "team ERA" weights mop-up relief equally with the starter's
    /// 6 IP, which is misleading more often than it's useful.
    private func pitchingTotalsRow(rows: [BoxPlayer]) -> some View {
        var ipDec: Double = 0
        var H = 0, R = 0, ER = 0, BB = 0, SO = 0
        var pcTotal = 0
        var pcAny = false
        for p in rows {
            guard let pit = p.stats?.pitching else { continue }
            ipDec += Self.ipStringToDecimal(pit.inningsPitched)
            H  += pit.hits        ?? 0
            R  += pit.runs        ?? 0
            ER += pit.earnedRuns  ?? 0
            BB += pit.baseOnBalls ?? 0
            SO += pit.strikeOuts  ?? 0
            if let pc = pit.pitchCount { pcAny = true; pcTotal += pc }
        }
        return HStack(spacing: 0) {
            Text("Totals")
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
                .frame(width: PitchingCol.name, alignment: .leading)
            Text(Self.decimalIPToBaseball(ipDec))
                .font(.caption.weight(.semibold))
                .monospacedDigit()
                .frame(width: PitchingCol.ip, alignment: .trailing)
            totalsCell(H,  width: PitchingCol.h)
            totalsCell(R,  width: PitchingCol.r)
            totalsCell(ER, width: PitchingCol.er)
            totalsCell(BB, width: PitchingCol.bb)
            totalsCell(SO, width: PitchingCol.so)
            // ERA deliberately blank — see batting-totals comment.
            Text("")
                .frame(width: PitchingCol.era, alignment: .trailing)
            Text(pcAny ? String(pcTotal) : "—")
                .font(.caption.weight(.semibold))
                .monospacedDigit()
                .frame(width: PitchingCol.pc, alignment: .trailing)
        }
        .padding(.vertical, 2)
    }

    private func totalsCell(_ v: Int, width: CGFloat) -> some View {
        Text(String(v))
            .font(.caption.weight(.semibold))
            .monospacedDigit()
            .frame(width: width, alignment: .trailing)
    }

    /// Parse baseball notation ("5.2" = 5⅔) to decimal (5.667).
    /// Empty / nil / unparseable → 0.
    private static func ipStringToDecimal(_ s: String?) -> Double {
        guard let s, !s.isEmpty else { return 0 }
        if let dot = s.firstIndex(of: ".") {
            let whole = Double(s[..<dot]) ?? 0
            let frac = Double(s[s.index(after: dot)...]) ?? 0
            return whole + frac / 3.0
        }
        return Double(s) ?? 0
    }

    /// Render a true-decimal IP back in baseball notation:
    /// 5.667 → "5.2", 9.0 → "9.0".
    private static func decimalIPToBaseball(_ d: Double) -> String {
        guard d > 0 else { return "0.0" }
        let whole = Int(d)
        let outs = Int(((d - Double(whole)) * 3).rounded())
        if outs >= 3 { return "\(whole + 1).0" }
        return "\(whole).\(outs)"
    }

    private func playerLabel(_ p: BoxPlayer, isPitcher: Bool) -> some View {
        HStack(spacing: 4) {
            Text(shortName(p.person.fullName))
                .font(.caption.weight(.medium))
                .lineLimit(1)
            if let pos = p.position?.abbreviation, !isPitcher {
                Text(pos)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func cell(_ v: Int?, width: CGFloat = 28) -> some View {
        Text(v.map(String.init) ?? "-")
            .font(.caption)
            .monospacedDigit()
            .frame(width: width, alignment: .trailing)
    }

    /// Team abbreviation with a name-prefix fallback so the plays
    /// score line ("NYY 2, TOR 0") never reads "nil".
    private func teamAbbr(_ info: TeamInfo) -> String {
        info.abbreviation ?? String(info.name.prefix(3)).uppercased()
    }

    private func shortName(_ full: String) -> String {
        let parts = full.split(separator: " ")
        guard let first = parts.first, parts.count >= 2 else { return full }
        // `lastNameWithSuffix` handles the Jr./Sr./II/III/IV cases —
        // "Fernando Tatis Jr." → "Tatis Jr." rather than "Jr.".
        return "\(first.prefix(1)). \(lastNameWithSuffix(full))"
    }

    // MARK: - Navigation

    private func tapPlayer(id: Int, name: String, isPitcher: Bool) {
        // `id` is a BDL player id (BoxScoreResponse synthesized
        // from BDL keys players by BDL id, not MLBAM). The resolve
        // call hops through our backend's `/players/by-bdl-id/{id}`
        // to land on an MLBAM-keyed PlayerSearchResult that the
        // existing profile destination can consume.
        guard pendingPlayerLookup == nil else { return }
        pendingPlayerLookup = id
        navigationError = nil
        Task { @MainActor in
            let player = await vm.playerProfile(bdlId: id)
            pendingPlayerLookup = nil
            if let player {
                // Force the profile's default role to the table the
                // user tapped: a two-way player (Ohtani) tapped in the
                // batting lineup opens to batting, tapped in the
                // pitching table opens to pitching. by-bdl-id resolves
                // one canonical side per player, so without this every
                // tap would land on that single side. Mirrors how the
                // leaderboard path stamps `is_pitcher` per board.
                path.append(player.withIsPitcher(isPitcher))
                return
            }
            // 404 → player's bdl_id isn't mapped in our DB yet.
            // The mapping bootstrap walk is still extending into
            // historical rows; toast wording reflects that ("yet")
            // rather than implying permanent unavailability.
            navigationError = "\(name)'s profile isn't available yet."
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            if navigationError != nil { navigationError = nil }
        }
    }

    private static let timeFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "h:mm a"
        return f
    }()
}
