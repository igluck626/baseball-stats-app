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

@MainActor
final class PlayerViewModel: ObservableObject {
    /// Source of truth for the bio shown in the header. Comes from the
    /// search row that pushed this screen — no extra fetch required.
    let player: PlayerSearchResult

    @Published var currentBatting: PlayerCurrentStats?
    @Published var careerBatting: PlayerCareerStats?
    @Published var currentPitching: PitcherCurrentStats?
    @Published var careerPitching: PitcherCareerStats?

    @Published var isLoadingCurrentBatting = false
    @Published var isLoadingCareerBatting = false
    @Published var isLoadingCurrentPitching = false
    @Published var isLoadingCareerPitching = false
    @Published var error: String?

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
    func loadData() async {
        error = nil

        async let currentBattingDone:  Void = loadCurrentBatting()
        async let careerBattingDone:   Void = loadCareerBatting()
        async let currentPitchingDone: Void = loadCurrentPitching()
        async let careerPitchingDone:  Void = loadCareerPitching()

        _ = await (
            currentBattingDone, careerBattingDone,
            currentPitchingDone, careerPitchingDone
        )
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
