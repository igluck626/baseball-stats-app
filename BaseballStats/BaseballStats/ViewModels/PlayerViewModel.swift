//
//  PlayerViewModel.swift
//  BaseballStats
//
//  Drives PlayerProfileView. The bio block is already on the
//  PlayerSearchResult passed in — we only fetch current + career here.
//
//  Two-way handling: every player gets four parallel fetches (batting
//  current, batting career, pitching current, pitching career). Whichever
//  endpoints return data determines isBatter/isPitcher/isTwoWay. The
//  backend returns 404 for the wrong group, which APIClient maps to nil
//  rather than throwing — so a position player just naturally has both
//  pitching responses as nil.
//

import Combine
import Foundation

/// Box-score line overlay applied on top of the overnight season
/// totals in the player profile. Populated by `loadRecentGameStats()`
/// across today's live/final games, with per-game lines summed
/// into a single cumulative overlay before being merged with the
/// overnight totals. Same shape serves both per-game parsing and
/// the accumulated total.
struct BoxBattingLine: Hashable {
    /// Number of games this line represents. A parsed single-game
    /// line has games == 1; the cumulative overlay accumulates this
    /// so the season G can be incremented by the correct amount when
    /// summing across multiple games (i.e. a doubleheader).
    var games:   Int = 0
    var AB:      Int = 0
    var R:       Int = 0
    var H:       Int = 0
    var doubles: Int = 0
    var triples: Int = 0
    var HR:      Int = 0
    var RBI:     Int = 0
    var BB:      Int = 0
    var SO:      Int = 0
    var SB:      Int = 0
    var HBP:     Int = 0
    var SF:      Int = 0

    /// Plate appearances — approximated as AB + BB + HBP + SF. SH
    /// (sacrifice bunts) is omitted; rare enough at modern usage
    /// that the one-PA imprecision is acceptable for this overlay.
    var PA: Int { AB + BB + HBP + SF }

    /// "Did the batter actually appear?" — at least one PA of any
    /// kind. A pinch-runner-only row or DNP shouldn't trigger the
    /// overlay.
    var appeared: Bool { PA > 0 }

    /// Accumulator — sum another box-score line into this one.
    /// Used when the player appeared in more than one game today
    /// (i.e. a doubleheader).
    mutating func add(_ o: BoxBattingLine) {
        games += o.games
        AB += o.AB; R += o.R; H += o.H
        doubles += o.doubles; triples += o.triples; HR += o.HR
        RBI += o.RBI; BB += o.BB; SO += o.SO; SB += o.SB
        HBP += o.HBP; SF += o.SF
    }
}

struct BoxPitchingLine: Hashable {
    var games: Int = 0
    /// Already in true-decimal form (5.667 = 5⅔). The MLB box score
    /// ships "5.2" as a string; conversion happens at parse time.
    var IP: Double = 0
    var H:  Int    = 0
    var R:  Int    = 0
    var ER: Int    = 0
    var BB: Int    = 0
    var SO: Int    = 0
    var HR: Int    = 0

    var appeared: Bool { IP > 0 }

    mutating func add(_ o: BoxPitchingLine) {
        games += o.games
        IP += o.IP
        H  += o.H;  R  += o.R;  ER += o.ER
        BB += o.BB; SO += o.SO; HR += o.HR
    }
}

@MainActor
final class PlayerViewModel: ObservableObject {
    /// Source of truth for the bio shown in the header. Comes from the
    /// search row that pushed this screen — no extra fetch required.
    let player: PlayerSearchResult

    @Published var currentBatting: PlayerCurrentStats?
    @Published var careerBatting: PlayerCareerStats?
    @Published var currentPitching: PitcherCurrentStats?
    @Published var careerPitching: PitcherCareerStats?
    /// Career-wide awards + vote-share data — keyed lookups built off
    /// `awards?.career_by_year` drive the per-season chiclets in the
    /// frozen pane and the headline-counts row in the header card.
    @Published var awards: PlayerAwardsResponse?

    /// Cumulative box-score overlay for the player across today's
    /// live/final games. Summed in `loadRecentGameStats()` and
    /// merged into the season totals at render time. nil → no
    /// overlay applied (no eligible games, or the player didn't
    /// appear in any of them).
    @Published var recentBatting: BoxBattingLine?
    @Published var recentPitching: BoxPitchingLine?
    /// True once `loadRecentGameStats()` has applied an overlay —
    /// any appearance in today's live or final games. Drives the
    /// silent stat update behavior but NOT the LIVE badge; see
    /// `hasLiveGame` for that.
    @Published var recentStatsLoaded: Bool = false
    /// True iff at least one of the games whose stats were folded
    /// into the overlay is currently in-progress. Final-only
    /// overlays (today's already-completed game) don't flip this.
    /// Gates the pulsing LIVE badge so the badge only appears
    /// when there's something genuinely live to watch — final-
    /// game stat fill-in stays silent.
    @Published var hasLiveGame: Bool = false
    /// Broader signal than `hasLiveGame`: true iff this player's
    /// team has *any* game currently in-progress, even when the
    /// player himself hasn't entered yet (a setup man waiting in
    /// the bullpen, a bench bat not yet called on). Drives the
    /// auto-refresh timer so we keep polling until either the
    /// player appears or the game ends.
    @Published var teamHasLiveGame: Bool = false

    /// Backing task for the 60-second `loadRecentGameStats()` poll
    /// loop. Started after the initial load lands; self-terminates
    /// when `teamHasLiveGame` flips false (all relevant games
    /// finished); cancelled on view disappear via
    /// `stopRecentGameRefresh()`.
    private var refreshTask: Task<Void, Never>?

    @Published var isLoadingCurrentBatting = false
    @Published var isLoadingCareerBatting = false
    @Published var isLoadingCurrentPitching = false
    @Published var isLoadingCareerPitching = false
    @Published var isLoadingAwards = false
    @Published var error: String?

    /// Year → per-season awards block. Lazy-built; rebuilds whenever
    /// `awards` is republished (the parallel fetch only publishes
    /// once, so the cost is trivial).
    var awardsByYear: [Int: PlayerAwardYear] {
        var map: [Int: PlayerAwardYear] = [:]
        for entry in awards?.career_by_year ?? [] {
            map[entry.year] = entry
        }
        return map
    }

    private let api: APIClient

    init(player: PlayerSearchResult, api: APIClient = .shared) {
        self.player = player
        self.api = api
    }

    // MARK: - Role detection

    /// Average PA-per-season threshold for "true batter" status. Rate
    /// rather than total because pre-DH NL pitchers (deGrom: 423 PA over
    /// 8 NL seasons = 53 PA/season) easily clear any reasonable absolute
    /// PA bar but show up correctly here. 250 PA/season is comfortably
    /// below everyday-player rates (Ohtani 497, Ruth 483, Trout ~500)
    /// and well above any pitcher hitting in their own at-bats (~50–80).
    private static let batterPAPerSeasonThreshold = 250

    /// IP threshold for "true pitcher" status. 50 IP filters out position
    /// players who pitched a single mop-up inning in a blowout.
    private static let pitcherIPThreshold: Double = 50

    /// "True batter" — has batting career stats AND a per-season PA rate
    /// at or above the everyday-player threshold. Seasons with zero PA
    /// don't dilute the rate (denominator counts only seasons with
    /// PA > 0), so deGrom's 2020/2022+ shutout years aren't averaged in.
    var isBatter: Bool {
        guard careerBatting != nil else { return false }
        let counting = seasonsWithPA
        guard counting > 0 else { return false }
        return careerPA / counting >= Self.batterPAPerSeasonThreshold
    }

    /// "True pitcher" — has pitching career stats AND >= 50 career IP.
    var isPitcher: Bool {
        guard careerPitching != nil else { return false }
        return careerIP >= Self.pitcherIPThreshold
    }

    /// Player is retired iff we know their last season AND it's strictly
    /// before the current year. Unknown last_season is treated as active
    /// (rookies whose row hasn't landed yet).
    var isRetired: Bool {
        guard let last = player.mlb_last_season else { return false }
        let currentYear = Calendar.current.component(.year, from: Date())
        return last < currentYear
    }

    /// Both thresholds met — Ohtani, Babe Ruth. UI surfaces a role toggle.
    var isTwoWay: Bool { isBatter && isPitcher }

    // MARK: - Career-totals based role detection

    /// Threshold for "this player has a real career on this side of
    /// the ball." 50 PA / 50 IP is loose enough to include NL-era
    /// pitchers like deGrom (423 career PA — never 250 in a season,
    /// so `isBatter` rejects him, but he genuinely has a batting
    /// career worth surfacing) and tight enough to exclude pinch-hit-
    /// pitcher novelty stints.
    private static let meaningfulPA: Int     = 50
    private static let meaningfulIP: Double  = 50

    /// True iff this player's career batting volume crosses the
    /// "meaningful" line. Drives whether the profile should expose
    /// the Batting/Pitching role toggle alongside `hasMeaningfulPitching`.
    var hasMeaningfulBatting: Bool {
        careerPA > Self.meaningfulPA
    }

    /// True iff this player's career pitching volume crosses the
    /// "meaningful" line. Same purpose as `hasMeaningfulBatting`.
    var hasMeaningfulPitching: Bool {
        careerIP > Self.meaningfulIP
    }

    /// Heuristic the profile uses to pick a default role tab when the
    /// leaderboard `is_pitcher` hint isn't available (e.g. the user
    /// reached the player via search). Pitching wins when career IP
    /// exceeds career PA — true for pure pitchers (1500 IP, 0 PA) and
    /// for NL-era starters (deGrom: 1500 IP > 423 PA), false for
    /// position players and two-way batting-leans (Ohtani: 600 IP <
    /// 1500 PA, Ruth: 1221 IP < 10600 PA).
    var inferredPitcherRole: Bool {
        careerIP > Double(careerPA)
    }

    /// Whether any batting data is loaded — used by the View's fallback
    /// branch when neither threshold is met (e.g. rookies, sub-threshold
    /// careers, or before career data has loaded). Don't conflate with
    /// `isBatter`, which is the threshold-gated definition.
    var hasAnyBatting: Bool {
        if currentBatting != nil { return true }
        if let seasons = careerBatting?.seasons, !seasons.isEmpty { return true }
        return false
    }

    var hasAnyPitching: Bool {
        if currentPitching != nil { return true }
        if let seasons = careerPitching?.seasons, !seasons.isEmpty { return true }
        return false
    }

    /// Career PA, summed across the seasons array. Returns 0 when nothing
    /// is loaded or every season is missing PA.
    private var careerPA: Int {
        (careerBatting?.seasons ?? []).reduce(0) { $0 + ($1.PA ?? 0) }
    }

    /// Number of batting seasons with PA > 0 — the denominator for the
    /// PA-per-season rate. Seasons where the player didn't bat at all
    /// (pitchers in DH-era leagues, or skipped years) are excluded so
    /// they don't drag the average toward false negatives.
    private var seasonsWithPA: Int {
        (careerBatting?.seasons ?? []).filter { ($0.PA ?? 0) > 0 }.count
    }

    /// Career IP from the totals payload. The pitcher career_totals
    /// always carries IP when seasons exist, so no per-season fallback
    /// is needed here.
    private var careerIP: Double {
        careerPitching?.career_totals?.IP ?? 0
    }

    // MARK: - Loading

    /// Fires all four endpoints in parallel via `async let`. Each branch
    /// owns its own loading flag so the UI can render whichever finishes
    /// first; a slow career fetch doesn't block the overview.
    ///
    /// Once the main parallel loads finish, kicks off a background
    /// `loadRecentGameStats()` task — that fetch hits MLB Stats API
    /// directly and overlays the player's recent box-score lines
    /// onto the season totals. The overlay is silent (no spinner,
    /// no blocking) so the initial profile render stays fast.
    func loadData() async {
        error = nil

        async let currentBattingDone:  Void = loadCurrentBatting()
        async let careerBattingDone:   Void = loadCareerBatting()
        async let currentPitchingDone: Void = loadCurrentPitching()
        async let careerPitchingDone:  Void = loadCareerPitching()
        async let awardsDone:          Void = loadAwards()

        _ = await (
            currentBattingDone, careerBattingDone,
            currentPitchingDone, careerPitchingDone,
            awardsDone
        )

        // Background task — never awaited from `loadData`'s caller so
        // a slow/failed MLB Stats API call can't block UI updates.
        // Once the initial overlay lands, start the 60-second poll
        // loop so a profile opened before the player appears in
        // the box score still catches their stats once they do.
        Task { [weak self] in
            await self?.loadRecentGameStats()
            self?.startRecentGameRefresh()
        }
    }

    // MARK: - Recent-game stats overlay

    /// Fetches the player's box-score lines for today's games and
    /// folds them into a cumulative overlay on top of the overnight
    /// season totals. Eligible games are today's Live + Final
    /// entries — Preview / Scheduled / Postponed skip.
    ///
    /// Yesterday's schedule is NOT consulted: the ~02:00 UTC
    /// nightly run reliably absorbs every game that finished
    /// yesterday (essentially nothing past 02:00 UTC ≈ 9 PM PT
    /// extra-innings edge case). Pulling yesterday risked double-
    /// counting more often than it fixed missing stats.
    ///
    /// Box scores are fetched in parallel and summed before applying
    /// so a doubleheader produces one merged overlay (not two
    /// stacked applications). The LIVE badge fires only when at
    /// least one folded game is actually live; pure final overlays
    /// fill stats silently.
    func loadRecentGameStats() async {
        guard !isRetired else { return }
        guard let teamId = mlbTeamId(for: player.teamCode) else { return }
        let mlb = MLBStatsAPIClient.shared

        let todaySchedule = try? await mlb.getTeamSchedule(date: Date(), teamId: teamId)

        // Build the eligible-games list, tagging each with whether
        // it's currently live — the `hasLiveGame` flag below is the
        // OR across these tags. `teamHasLiveGame` is a separate
        // signal flipped before we even look at the player's box-
        // score line so the refresh loop keeps polling for
        // not-yet-appeared players.
        var eligible: [(gamePk: Int, isLive: Bool)] = []
        var liveOnSchedule = false
        for g in todaySchedule?.dates.flatMap(\.games) ?? [] {
            switch g.status.abstractGameState {
            case "Live":
                liveOnSchedule = true
                eligible.append((g.gamePk, true))
            case "Final":
                eligible.append((g.gamePk, false))
            default:
                break
            }
        }
        // Publish the team-live signal even if there's nothing to
        // overlay yet — it gates whether the refresh loop keeps
        // running. A bench player whose team is mid-game but who
        // hasn't pinch-hit yet has no overlay, but we want the
        // loop to keep checking until they appear.
        teamHasLiveGame = liveOnSchedule

        guard !eligible.isEmpty else { return }

        // Fan out box-score fetches in parallel — typical case is
        // 1-2 games so the cost is small, but TaskGroup keeps the
        // wall-clock at one round trip even if it grows.
        let playerKey = "ID\(player.player_id)"
        let perGame: [(bat: BoxBattingLine?, pit: BoxPitchingLine?, isLive: Bool)]
            = await withTaskGroup(
                of: (BoxBattingLine?, BoxPitchingLine?, Bool)?.self
            ) { group in
                for entry in eligible {
                    let pk = entry.gamePk
                    let live = entry.isLive
                    group.addTask {
                        guard let feed = try? await mlb.getLiveFeed(gamePk: pk) else { return nil }
                        guard let teams = feed.liveData.boxscore?.teams else { return nil }
                        guard let bp = teams.away.players[playerKey]
                                    ?? teams.home.players[playerKey] else { return nil }
                        let bat = bp.stats?.batting.flatMap(Self.parseBatting)
                        let pit = bp.stats?.pitching.flatMap(Self.parsePitching)
                        return (bat, pit, live)
                    }
                }
                var hits: [(BoxBattingLine?, BoxPitchingLine?, Bool)] = []
                for await maybe in group {
                    if let m = maybe { hits.append(m) }
                }
                return hits.map { (bat: $0.0, pit: $0.1, isLive: $0.2) }
            }

        var totalBat = BoxBattingLine()
        var totalPit = BoxPitchingLine()
        var sawBat = false
        var sawPit = false
        var anyLive = false

        for entry in perGame {
            if let b = entry.bat, b.appeared { totalBat.add(b); sawBat = true }
            if let p = entry.pit, p.appeared { totalPit.add(p); sawPit = true }
            if entry.isLive { anyLive = true }
        }

        guard sawBat || sawPit else { return }
        recentBatting    = sawBat ? totalBat : nil
        recentPitching   = sawPit ? totalPit : nil
        recentStatsLoaded = true
        hasLiveGame      = anyLive
    }

    /// Kick off a 60-second poll that re-runs `loadRecentGameStats()`
    /// while the player's team has a game in progress. Idempotent —
    /// cancels any existing task before installing a new one so it's
    /// safe to call multiple times. Self-terminates when
    /// `teamHasLiveGame` flips to false (all games finished); the
    /// view-side `.onDisappear` calls `stopRecentGameRefresh()` as a
    /// belt-and-suspenders so a backgrounded profile doesn't keep
    /// the task alive across screens.
    func startRecentGameRefresh() {
        stopRecentGameRefresh()
        guard !isRetired else { return }
        guard teamHasLiveGame else { return }
        refreshTask = Task { @MainActor [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 60 * 1_000_000_000)
                guard !Task.isCancelled, let self else { return }
                await self.loadRecentGameStats()
                // Exit once the schedule no longer has any live
                // games for this team — once everything's final,
                // overnight totals are the next state change.
                if !self.teamHasLiveGame { return }
            }
        }
    }

    func stopRecentGameRefresh() {
        refreshTask?.cancel()
        refreshTask = nil
    }

    private static func parseBatting(_ b: BoxBatting) -> BoxBattingLine {
        BoxBattingLine(
            games:   1,
            AB:      b.atBats      ?? 0,
            R:       b.runs        ?? 0,
            H:       b.hits        ?? 0,
            doubles: b.doubles     ?? 0,
            triples: b.triples     ?? 0,
            HR:      b.homeRuns    ?? 0,
            RBI:     b.rbi         ?? 0,
            BB:      b.baseOnBalls ?? 0,
            SO:      b.strikeOuts  ?? 0,
            // BoxBatting doesn't carry SB / HBP / SF — zeroed.
            // One-game error margin on AVG/OBP recomputation is
            // below display precision; same applies summed across
            // a small handful of games.
            SB:  0,
            HBP: 0,
            SF:  0
        )
    }

    private static func parsePitching(_ p: BoxPitching) -> BoxPitchingLine {
        BoxPitchingLine(
            games: 1,
            IP:    Self.parseInningsString(p.inningsPitched),
            H:     p.hits        ?? 0,
            R:     p.runs        ?? 0,
            ER:    p.earnedRuns  ?? 0,
            BB:    p.baseOnBalls ?? 0,
            SO:    p.strikeOuts  ?? 0,
            HR:    p.homeRuns    ?? 0
        )
    }

    /// MLB box scores ship innings as "5.2" → 5 and ⅔ innings, NOT
    /// 5.2 in decimal. Convert to true decimal (5.667) so it can be
    /// added to the overnight Float-stored IP without distortion.
    private static func parseInningsString(_ s: String?) -> Double {
        guard let s, !s.isEmpty else { return 0 }
        if let dot = s.firstIndex(of: ".") {
            let whole = Double(s[..<dot]) ?? 0
            let after = s.index(after: dot)
            let frac = Double(s[after...]) ?? 0
            return whole + frac / 3.0
        }
        return Double(s) ?? 0
    }

    private func loadAwards() async {
        isLoadingAwards = true
        do {
            awards = try await api.getPlayerAwards(playerId: player.player_id)
        } catch {
            // Award absence shouldn't bubble a screen-level error
            // ("Couldn't load profile") — a player with no awards
            // and no votes legitimately 404s.
        }
        isLoadingAwards = false
    }

    private func loadCurrentBatting() async {
        isLoadingCurrentBatting = true
        do {
            currentBatting = try await api.getPlayerCurrentStats(playerId: player.player_id)
        } catch {
            recordError(error)
        }
        isLoadingCurrentBatting = false
    }

    private func loadCareerBatting() async {
        isLoadingCareerBatting = true
        do {
            careerBatting = try await api.getPlayerCareerStats(playerId: player.player_id)
        } catch {
            recordError(error)
        }
        isLoadingCareerBatting = false
    }

    private func loadCurrentPitching() async {
        isLoadingCurrentPitching = true
        do {
            currentPitching = try await api.getPitcherCurrentStats(playerId: player.player_id)
        } catch {
            recordError(error)
        }
        isLoadingCurrentPitching = false
    }

    private func loadCareerPitching() async {
        isLoadingCareerPitching = true
        do {
            careerPitching = try await api.getPitcherCareerStats(playerId: player.player_id)
        } catch {
            recordError(error)
        }
        isLoadingCareerPitching = false
    }

    /// Don't clobber the first error — once we've surfaced a failure, keep
    /// it visible. The other parallel branches may succeed and replace
    /// their data; we only show one error in the UI at a time.
    private func recordError(_ error: Error) {
        if self.error == nil {
            self.error = error.localizedDescription
        }
    }
}
