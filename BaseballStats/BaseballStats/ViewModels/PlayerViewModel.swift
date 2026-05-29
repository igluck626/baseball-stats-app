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

    /// Season-to-date rate stats as BDL has them after THIS game.
    /// Authoritative for display — BDL computes them off their own
    /// season counts (which already include today's PA), so using
    /// them avoids the rounding drift you get when the overlay
    /// recomputes from `overnight + this-game-counts`. nil when
    /// BDL didn't ship them on this row (early-season cold-start
    /// or the player has zero qualifying PAs).
    var seasonAVG: Double? = nil
    var seasonOBP: Double? = nil
    var seasonSLG: Double? = nil
    var seasonOPS: Double? = nil

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
    /// (i.e. a doubleheader). For the season rate stats we take
    /// whichever side has non-nil values — BDL's second-game
    /// rates already reflect the first game's contributions, so
    /// "most recent non-nil wins" gives the right cumulative
    /// view.
    mutating func add(_ o: BoxBattingLine) {
        games += o.games
        AB += o.AB; R += o.R; H += o.H
        doubles += o.doubles; triples += o.triples; HR += o.HR
        RBI += o.RBI; BB += o.BB; SO += o.SO; SB += o.SB
        HBP += o.HBP; SF += o.SF
        if let v = o.seasonAVG { seasonAVG = v }
        if let v = o.seasonOBP { seasonOBP = v }
        if let v = o.seasonSLG { seasonSLG = v }
        if let v = o.seasonOPS { seasonOPS = v }
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
    /// Per-game decision flags. Zero for most box-score appearances
    /// (one pitcher per game earns each); the winning / losing /
    /// saving pitcher gets a 1. Folded into the season W-L-SV
    /// totals at render time.
    var W:  Int    = 0
    var L:  Int    = 0
    var SV: Int    = 0
    /// Games started. Only meaningful on the BDL-direct path
    /// (`BDLSeasonStat.pitchingGs`); per-game stats rows ship the
    /// flag too but the existing overlay path doesn't propagate it.
    var GS: Int    = 0

    var appeared: Bool { IP > 0 }

    mutating func add(_ o: BoxPitchingLine) {
        games += o.games
        IP += o.IP
        H  += o.H;  R  += o.R;  ER += o.ER
        BB += o.BB; SO += o.SO; HR += o.HR
        W  += o.W;  L  += o.L;  SV += o.SV
        GS += o.GS
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
    /// True when the overlay is showing BDL's full season totals
    /// rather than a per-game delta on top of overnight. Set when
    /// `bdl_G > db_G` — BDL has absorbed a final our backend's
    /// nightly hasn't yet, so DB stats are stale. The renderer
    /// (`makeEffectiveBatting`/`makeEffectivePitching`) consults
    /// this flag to REPLACE the overnight stats with `recentBatting`
    /// / `recentPitching` rather than adding them on top.
    @Published var usesBDLDirectStats: Bool = false
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
        // Reset the BDL-direct flag at the top of every call —
        // it's recomputed below per the games-played comparison.
        usesBDLDirectStats = false
        // BDL team id is the hop the team-scoped game query needs.
        // Falls back silently when the player's Lahman teamCode
        // isn't in the BDL map (extreme edge case — rebranded
        // teams whose code we haven't added yet).
        guard let teamCode = player.teamCode,
              let bdlTeamId = lahmanToBDLTeamId[teamCode] else { return }
        let bdl = BallDontLieClient.shared
        let today = Self.dateOnly.string(from: Date())

        let todayGames = (try? await bdl.getTeamGames(date: today, teamId: bdlTeamId)) ?? []

        // Tag each eligible game with whether it's currently live.
        // BDL's status enum collapses to two states we care about:
        // STATUS_IN_PROGRESS → overlay + keep polling; STATUS_FINAL
        // → overlay once, stop polling.
        var eligible: [(gameId: Int, isLive: Bool)] = []
        var liveOnSchedule = false
        var hasFinal = false
        for g in todayGames {
            switch g.status {
            case "STATUS_IN_PROGRESS", "STATUS_DELAYED":
                liveOnSchedule = true
                eligible.append((g.id, true))
            case "STATUS_FINAL":
                hasFinal = true
                eligible.append((g.id, false))
            default:
                break
            }
        }
        // Publish the team-live signal even if there's nothing to
        // overlay yet — gates the refresh-loop continuation so a
        // bench player whose team is mid-game keeps polling until
        // they appear.
        teamHasLiveGame = liveOnSchedule

        // Post-midnight-ET fallback. When nothing landed under
        // "today ET", a player's evening West-Coast game may have
        // shipped under yesterday's ET date (10pm PT == 1am ET ==
        // yesterday's date in ET's calendar). Pull yesterday's finals
        // and keep only the ones that started at or after 18:00 ET —
        // afternoon games are already in our nightly DB, so the cutoff
        // avoids re-overlaying them.
        if eligible.isEmpty {
            var etCal = Calendar(identifier: .gregorian)
            etCal.timeZone = TimeZone(identifier: "America/New_York")
                ?? TimeZone.current
            // Latest nightly stamp across both sides. ISO-8601 UTC.
            // If the nightly ran after a candidate game's end, that
            // game's already in our DB and an overlay would double-
            // count it.
            let lastUpdated: Date? = {
                let raw = [
                    currentBatting?.stats_last_updated,
                    currentPitching?.stats_last_updated,
                ].compactMap { $0 }
                let dates = raw.compactMap {
                    try? Date($0, strategy: .iso8601)
                }
                return dates.max()
            }()
            if let yest = etCal.date(byAdding: .day, value: -1, to: Date()) {
                let yString = Self.dateOnly.string(from: yest)
                let yGames = (try? await bdl.getTeamGames(
                    date: yString, teamId: bdlTeamId,
                )) ?? []
                for g in yGames where g.status == "STATUS_FINAL" {
                    guard let start = g.startDate else { continue }
                    let hour = etCal.component(.hour, from: start)
                    guard hour >= 18 else { continue }
                    // 4-hour buffer is generous for a 9-inning regular
                    // game (~3hr median). Skip when the nightly stamp
                    // is past that buffer — we'd be overlaying a game
                    // already in the DB.
                    if let lastUpdated,
                       lastUpdated > start.addingTimeInterval(4 * 3600) {
                        continue
                    }
                    hasFinal = true
                    eligible.append((g.id, false))
                }
            }
        }

        guard !eligible.isEmpty else { return }
        // BDL player id is the join key on the stats response. If
        // we don't have one (mapping bootstrap hasn't reached this
        // player yet), we can't filter — bail out and leave the
        // overnight totals as-is.
        guard let bdlPlayerId = player.bdl_id else { return }

        // Games-played gate for Final games. Three resolutions:
        //
        // • bdl_G == db_G  → overlay finals normally (the box-score
        //   data exists but neither BDL nor our DB has absorbed
        //   it yet).
        // • bdl_G  > db_G  → BDL has absorbed a final our nightly
        //   hasn't. Fetch BDL's full season totals and use them as
        //   the displayed stats via the `usesBDLDirectStats` flag,
        //   so the user sees current numbers instead of stale DB.
        // • Otherwise → skip finals (would double-count) but keep
        //   live games. Two-way needs the rule satisfied on the
        //   relevant side(s).
        //
        // Live games are never in the nightly by definition; they
        // bypass the gate.
        var totalBat = BoxBattingLine()
        var totalPit = BoxPitchingLine()
        var sawBat = false
        var sawPit = false

        if hasFinal {
            let season = Calendar.current.component(.year, from: Date())
            let rows = (try? await bdl.getSeasonStats(
                playerIds: [bdlPlayerId], season: season,
            )) ?? []
            let bdlSeason = rows.first(where: { $0.player.id == bdlPlayerId })
            let dbBattingG   = currentBatting?.standard?.G
            let dbPitchingG  = currentPitching?.standard?.G
            let bdlBattingG  = bdlSeason?.battingGp
            let bdlPitchingG = bdlSeason?.pitchingGp
            let battingEqual: Bool = {
                if let db = dbBattingG, let b = bdlBattingG { return db == b }
                return false
            }()
            let pitchingEqual: Bool = {
                if let db = dbPitchingG, let p = bdlPitchingG { return db == p }
                return false
            }()
            let battingBDLAhead: Bool = {
                if let db = dbBattingG, let b = bdlBattingG { return b > db }
                return false
            }()
            let pitchingBDLAhead: Bool = {
                if let db = dbPitchingG, let p = bdlPitchingG { return p > db }
                return false
            }()
            let shouldOverlayFinals: Bool = {
                if hasMeaningfulBatting && hasMeaningfulPitching {
                    return battingEqual && pitchingEqual
                }
                if hasMeaningfulBatting  { return battingEqual }
                if hasMeaningfulPitching { return pitchingEqual }
                return false
            }()
            let shouldUseBDLDirect: Bool = {
                guard !shouldOverlayFinals, bdlSeason != nil else { return false }
                if hasMeaningfulBatting && battingBDLAhead { return true }
                if hasMeaningfulPitching && pitchingBDLAhead { return true }
                return false
            }()
            if shouldUseBDLDirect, let bdlSeason {
                // Seed totalBat/totalPit with BDL's full season
                // totals. Live games (if any) will accumulate on
                // top via the existing per-game add() loop —
                // BDL's snapshot is post-final, today's live game
                // is separate. usesBDLDirectStats tells the
                // renderer to REPLACE the overnight stats rather
                // than add to them.
                if hasMeaningfulBatting {
                    totalBat = bdlSeason.toBattingLine()
                    sawBat = true
                }
                if hasMeaningfulPitching {
                    totalPit = bdlSeason.toPitchingLine()
                    sawPit = true
                }
                usesBDLDirectStats = true
            }
            if !shouldOverlayFinals {
                // BDL-direct or not, finals shouldn't go through
                // the per-game add() — BDL totals already cover
                // them (BDL-direct path) OR they'd double-count
                // (non-BDL-direct path).
                eligible.removeAll { !$0.isLive }
                if eligible.isEmpty {
                    // No live games remain. Publish whatever
                    // BDL-direct totals we computed (or do nothing
                    // when neither side qualified).
                    if sawBat || sawPit {
                        recentBatting    = sawBat ? totalBat : nil
                        recentPitching   = sawPit ? totalPit : nil
                        recentStatsLoaded = true
                        hasLiveGame      = false
                    }
                    return
                }
            }
        }

        // Fan out stats fetches in parallel.
        let perGame: [(bat: BoxBattingLine?, pit: BoxPitchingLine?, isLive: Bool)]
            = await withTaskGroup(
                of: (BoxBattingLine?, BoxPitchingLine?, Bool)?.self
            ) { group in
                for entry in eligible {
                    let gid = entry.gameId
                    let live = entry.isLive
                    group.addTask {
                        guard let rows = try? await bdl.getGameStats(gameId: gid) else {
                            return nil
                        }
                        // BDL returns one row per (player, side) — a
                        // two-way player gets two rows in the same
                        // response, one batting one pitching. Sum
                        // across them for the overlay.
                        let mine = rows.filter { $0.player.id == bdlPlayerId }
                        guard !mine.isEmpty else { return nil }
                        var bat: BoxBattingLine? = nil
                        var pit: BoxPitchingLine? = nil
                        for r in mine {
                            if let line = Self.bdlBattingLine(r) {
                                if var existing = bat { existing.add(line); bat = existing }
                                else                  { bat = line }
                            }
                            if let line = Self.bdlPitchingLine(r) {
                                if var existing = pit { existing.add(line); pit = existing }
                                else                  { pit = line }
                            }
                        }
                        return (bat, pit, live)
                    }
                }
                var hits: [(BoxBattingLine?, BoxPitchingLine?, Bool)] = []
                for await maybe in group {
                    if let m = maybe { hits.append(m) }
                }
                return hits.map { (bat: $0.0, pit: $0.1, isLive: $0.2) }
            }

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

    /// BDL `BDLPlayerStat` → batting line. Returns nil when the
    /// row has no batting activity (pure pitching appearances on
    /// a two-way player's other row).
    nonisolated private static func bdlBattingLine(_ s: BDLPlayerStat) -> BoxBattingLine? {
        let ab = s.atBats ?? 0
        let bb = s.bb ?? 0
        let pa = s.plateAppearances ?? 0
        // Skip if no plate appearances. `appeared` flag elsewhere
        // gates the overlay sum, so an all-zeros row would no-op
        // anyway — but bailing here avoids a wasted `add()`.
        if ab == 0, bb == 0, pa == 0 { return nil }
        // BDL's per-game row carries season-to-date AVG / OBP /
        // SLG as of (and including) this game. OPS isn't shipped
        // directly — derive from OBP + SLG when both are present
        // (the same convention the box-score synthesizer uses).
        let opsValue: Double? = (s.obp != nil && s.slg != nil)
            ? (s.obp! + s.slg!)
            : nil
        return BoxBattingLine(
            games:   1,
            AB:      ab,
            R:       s.runs           ?? 0,
            H:       s.hits           ?? 0,
            doubles: s.doubles        ?? 0,
            triples: s.triples        ?? 0,
            HR:      s.hr             ?? 0,
            RBI:     s.rbi            ?? 0,
            BB:      bb,
            SO:      s.k              ?? 0,
            SB:      s.stolenBases    ?? 0,
            HBP:     s.hitByPitch     ?? 0,
            SF:      s.sacFlies       ?? 0,
            seasonAVG: s.avg,
            seasonOBP: s.obp,
            seasonSLG: s.slg,
            seasonOPS: opsValue
        )
    }

    /// BDL `BDLPlayerStat` → pitching line. Returns nil when the
    /// row has no pitching activity.
    nonisolated private static func bdlPitchingLine(_ s: BDLPlayerStat) -> BoxPitchingLine? {
        // BDL's per-game `ip` is a baseball-notation string ("5.2"
        // = 5⅔, "0.1" = ⅓), NOT a decimal — parse to true decimal
        // before any arithmetic. Skip the row when the pitcher
        // didn't record an out (empty string or "0.0").
        guard let ipStr = s.ip, !ipStr.isEmpty else { return nil }
        let ip = Self.parseInningsString(ipStr)
        guard ip > 0 else { return nil }
        return BoxPitchingLine(
            games: 1,
            IP:    ip,
            H:     s.pHits ?? 0,
            R:     s.pRuns ?? 0,
            ER:    s.er    ?? 0,
            BB:    s.pBb   ?? 0,
            SO:    s.pK    ?? 0,
            HR:    s.pHr   ?? 0,
            W:     s.wins  ?? 0,
            L:     s.losses ?? 0,
            SV:    s.saves ?? 0
        )
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
            // Fire one refresh immediately on task start so the LIVE
            // badge appears without waiting a full 60s window. The
            // BDL client's in-process TTL cache collapses the call
            // when this method is invoked right after the initial
            // `loadRecentGameStats()` from `loadData` — no double
            // HTTP cost. The defense matters when the method is
            // called fresh (e.g. profile re-entry after backgrounding,
            // or a game flipping to Live between initial load and
            // the timer kickoff).
            if let strong = self {
                await strong.loadRecentGameStats()
                if !strong.teamHasLiveGame { return }
            } else {
                return
            }
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

    /// `yyyy-MM-dd` in local timezone for BDL's `dates[]` filter.
    /// `yyyy-MM-dd` for today, anchored to US Eastern time. MLB
    /// schedules games off ET, and the BDL client's `getTeamGames`
    /// expects a date in the same calendar. Anchoring this formatter
    /// to `America/New_York` (instead of the device's local timezone)
    /// prevents the "device on PT at 11pm sees today as yesterday"
    /// bug — at 23:00 PT a request for `today` would resolve to
    /// PT's date string (the calendar day already over on the East
    /// Coast) and miss tonight's game which BDL files under ET's
    /// next day.
    private static let dateOnly: DateFormatter = {
        let f = DateFormatter()
        f.calendar = .init(identifier: .gregorian)
        f.timeZone = TimeZone(identifier: "America/New_York") ?? .current
        f.locale   = .init(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()

    /// MLB conventions ship innings as "5.2" → 5 and ⅔ innings, NOT
    /// 5.2 in decimal. Convert to true decimal (5.667) so it can be
    /// added to the overnight Float-stored IP without distortion.
    /// `nonisolated` so the nonisolated `bdlPitchingLine` in this
    /// `@MainActor` class can call it without an actor hop.
    nonisolated private static func parseInningsString(_ s: String?) -> Double {
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


// MARK: - BDL season stats → BoxBattingLine / BoxPitchingLine

extension BDLSeasonStat {
    /// Convert BDL's season totals to a `BoxBattingLine` carrying
    /// the FULL season counts (not a delta). Used by the
    /// `usesBDLDirectStats` path when BDL is ahead of the DB and
    /// the renderer needs to replace overnight stats. BDL doesn't
    /// ship HBP / SF; they're left at 0 (PA derivation downstream
    /// is forced to use AB + BB only).
    func toBattingLine() -> BoxBattingLine {
        var line = BoxBattingLine()
        line.games   = battingGp  ?? 0
        line.AB      = battingAb  ?? 0
        line.R       = battingR   ?? 0
        line.H       = battingH   ?? 0
        line.doubles = batting2B  ?? 0
        line.triples = batting3B  ?? 0
        line.HR      = battingHr  ?? 0
        line.RBI     = battingRbi ?? 0
        line.BB      = battingBb  ?? 0
        line.SO      = battingSo  ?? 0
        line.SB      = battingSb  ?? 0
        line.HBP     = 0
        line.SF      = 0
        line.seasonAVG = battingAvg
        line.seasonOBP = battingObp
        line.seasonSLG = battingSlg
        line.seasonOPS = battingOps
        return line
    }

    /// Pitcher counterpart to `toBattingLine`. BDL's
    /// `pitching_ip` is in baseball notation (22.2 = 22⅔) so the
    /// converter promotes it to true decimal before storing on
    /// `BoxPitchingLine.IP`. R isn't shipped on the season-stats
    /// endpoint; ER is the relevant counter for ERA anyway.
    func toPitchingLine() -> BoxPitchingLine {
        var line = BoxPitchingLine()
        line.games = pitchingGp ?? 0
        line.IP    = BDLSeasonStat.ipBaseballToDecimal(pitchingIp)
        line.H     = pitchingH  ?? 0
        line.R     = 0
        line.ER    = pitchingEr ?? 0
        line.BB    = pitchingBb ?? 0
        line.SO    = pitchingK  ?? 0
        line.HR    = pitchingHr ?? 0
        line.W     = pitchingW  ?? 0
        line.L     = pitchingL  ?? 0
        line.SV    = pitchingSv ?? 0
        line.GS    = pitchingGs ?? 0
        return line
    }

    /// Baseball notation → true decimal: 22.2 → 22.667.
    /// nil / non-positive → 0.
    private static func ipBaseballToDecimal(_ v: Double?) -> Double {
        guard let v, v > 0 else { return 0 }
        let whole = Int(v)
        let outs  = Int(((v - Double(whole)) * 10).rounded())
        return Double(whole) + Double(outs) / 3.0
    }
}
