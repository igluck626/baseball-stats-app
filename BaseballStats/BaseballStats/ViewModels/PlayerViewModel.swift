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

/// One-game stat overlay applied on top of the overnight season
/// totals in the player profile. Populated by `loadTodayStats()`
/// when the player's team has an in-progress or just-finished game
/// and the player appeared in the box score.
struct TodayBattingLine: Hashable {
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
    /// that the one-PA imprecision per game is acceptable for a
    /// "today only" overlay.
    var PA: Int { AB + BB + HBP + SF }

    /// "Did the batter actually appear today?" — at least one PA
    /// of any kind. Defines whether the LIVE badge fires for
    /// batters; a position player who sat out shouldn't trigger it.
    var appeared: Bool { PA > 0 }
}

struct TodayPitchingLine: Hashable {
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

    /// Today's box-score line for the player, if their team has a
    /// game today that's already started and they appeared. nil
    /// otherwise (no game / didn't play / not yet started / retired).
    /// View merges these on top of the overnight `currentBatting` /
    /// `currentPitching` season totals.
    @Published var todayBatting: TodayBattingLine?
    @Published var todayPitching: TodayPitchingLine?
    /// True once `loadTodayStats()` completes AND found a real
    /// appearance (batting and/or pitching). Drives the LIVE badge
    /// on the season-card header. Won't flip if the player sat out
    /// or their team didn't play today.
    @Published var todayStatsLoaded: Bool = false

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
    /// `loadTodayStats()` task — that fetch hits MLB Stats API
    /// directly and overlays today's box-score line onto the season
    /// totals if the player's team has played today. The overlay is
    /// silent (no spinner, no blocking) so the initial profile
    /// render stays fast.
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
        Task { [weak self] in
            await self?.loadTodayStats()
        }
    }

    // MARK: - Today's stats overlay

    /// Fetches the player's box-score line for today's game (if any)
    /// and stores it as a `TodayBattingLine` / `TodayPitchingLine`
    /// overlay. Silently bails on every "doesn't apply" case:
    ///   • No teamCode / can't resolve to MLB team id (retired,
    ///     historical players, unmapped code)
    ///   • Team has no game today
    ///   • Game hasn't started yet (status "Preview" / "Scheduled")
    ///   • Player isn't in the box score (DNP)
    ///   • Player appeared but with zero PA/IP (e.g. courtesy runner)
    /// All failures are non-fatal — `todayStatsLoaded` stays false
    /// and the LIVE badge doesn't fire.
    func loadTodayStats() async {
        guard !isRetired else { return }
        guard let teamId = mlbTeamId(for: player.teamCode) else { return }

        let mlb = MLBStatsAPIClient.shared
        do {
            let schedule = try await mlb.getTeamSchedule(date: Date(), teamId: teamId)
            guard let game = schedule.dates.flatMap(\.games).first(where: {
                // "Preview" / "Scheduled" → not started; skip.
                $0.status.abstractGameState != "Preview"
            }) else { return }

            let feed = try await mlb.getLiveFeed(gamePk: game.gamePk)
            guard let teams = feed.liveData.boxscore?.teams else { return }
            let key = "ID\(player.player_id)"
            guard let boxPlayer = teams.away.players[key] ?? teams.home.players[key]
            else { return }

            let bat = boxPlayer.stats?.batting.flatMap(Self.parseBatting)
            let pit = boxPlayer.stats?.pitching.flatMap(Self.parsePitching)

            // Only flip the LIVE badge when there's a real appearance
            // on at least one side. A DNP row that exists in the box
            // score but with zero PA/IP shouldn't pretend the stats
            // have been updated.
            let batAppeared = bat?.appeared == true
            let pitAppeared = pit?.appeared == true
            guard batAppeared || pitAppeared else { return }

            todayBatting  = batAppeared ? bat : nil
            todayPitching = pitAppeared ? pit : nil
            todayStatsLoaded = true
        } catch {
            // Silent failure path — no badge, original totals shown.
        }
    }

    private static func parseBatting(_ b: BoxBatting) -> TodayBattingLine {
        TodayBattingLine(
            AB:      b.atBats        ?? 0,
            R:       b.runs          ?? 0,
            H:       b.hits          ?? 0,
            doubles: b.doubles       ?? 0,
            triples: b.triples       ?? 0,
            HR:      b.homeRuns      ?? 0,
            RBI:     b.rbi           ?? 0,
            BB:      b.baseOnBalls   ?? 0,
            SO:      b.strikeOuts    ?? 0,
            // BoxBatting doesn't carry SB / HBP / SF in the current
            // model — they're zeroed; one-game error margin on the
            // resulting AVG/OBP recomputation is below display
            // precision (third decimal place rarely shifts).
            SB:  0,
            HBP: 0,
            SF:  0
        )
    }

    private static func parsePitching(_ p: BoxPitching) -> TodayPitchingLine {
        TodayPitchingLine(
            IP: Self.parseInningsString(p.inningsPitched),
            H:  p.hits         ?? 0,
            R:  p.runs         ?? 0,
            ER: p.earnedRuns   ?? 0,
            BB: p.baseOnBalls  ?? 0,
            SO: p.strikeOuts   ?? 0,
            HR: p.homeRuns     ?? 0
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
