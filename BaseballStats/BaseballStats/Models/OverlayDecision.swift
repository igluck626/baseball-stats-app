//
//  OverlayDecision.swift
//  BaseballStats
//
//  Whether to overlay recent finals onto the stored season row, and how
//  many of them.
//
//  Extracted from `PlayerViewModel.loadRecentGameStats` so the rule can
//  be reasoned about and tested apart from the fetching around it — the
//  same reason `slot_codes` was pulled out of the live service.
//
//  ⚠️ COUNTING, NOT COMPARING. The gate this replaces asked "is today's
//  game in our gamelog?" and read a yes as "our season totals already
//  cover it". Those are different claims, because the two tables have
//  different provenance: the gamelog is written from per-game data,
//  `player_seasons` from balldontlie's season aggregate. When
//  balldontlie is late on a game, the gamelog gains a row the season row
//  does not reflect — so the one signal that should trigger the overlay
//  was the thing suppressing it.
//
//  Measured 2026-09-09, on a game balldontlie had not absorbed 11 hours
//  after it ended (TEX @ SEA, 2026-09-08):
//    • Bryce Miller  — season row G=18, ERA 4.01; gamelog 19 rows.
//      Correct after overlay: 4.22.
//    • Dominic Canzone — season row G=128, AVG .254; gamelog 129 rows.
//      Correct after overlay: .256.
//  Both rendered the PRE-GAME number as fact.
//
//  ⚠️ And this is the absorption-vintage rule in a fourth disguise: what
//  you measure against must be as current as the thing you are about to
//  change. The anchor here (our gamelog) is FRESHER than the value being
//  judged (the season row, at balldontlie's vintage), and reading them
//  as one clock is what produced the wrong answer.
//

import Foundation

struct OverlayDecision: Equatable {
    /// Apply recent finals on top of the stored season row.
    let shouldOverlayFinals: Bool
    /// At most this many finals may be applied. nil → unbounded, which
    /// is the behaviour when no count is available.
    let budget: Int?

    /// One side's inputs. `gamelogGames` is the DEDUPED count of distinct
    /// gamelog dates season-to-date; `seasonG` is the stored row's G,
    /// which is balldontlie's number verbatim.
    struct Side: Equatable {
        let seasonG: Int?
        let bdlG: Int?
        let gamelogGames: Int?
        let includesToday: Bool

        /// How many games the season row is behind our gamelog, or nil
        /// when the count is unavailable.
        var behind: Int? {
            guard let gamelogGames, let seasonG else { return nil }
            return gamelogGames - seasonG
        }

        /// True when this side has nothing to say and must not veto the
        /// other — a pitcher's batting row, say. ⚠️ A side that IS
        /// counted and reads `behind == 0` is a real "no" and does
        /// block: that is the double-count guard.
        var isSilent: Bool {
            behind == nil && (seasonG == nil || bdlG == nil)
        }

        /// ⚠️ Three branches:
        ///   • `behind > 0`  → the row is missing games; overlay.
        ///   • `behind == 0` → current; adding anything double-counts.
        ///   • `behind < 0`  → the aggregate is AHEAD of our gamelog,
        ///     which the BDL-direct path handles, not this one.
        /// Only when the count is absent does the old boolean decide.
        var allowsOverlay: Bool {
            if let behind { return behind > 0 }
            guard let seasonG, let bdlG else { return true }
            return seasonG == bdlG && !includesToday
        }
    }

    static func decide(batting: Side, pitching: Side) -> OverlayDecision {
        let overlay = (batting.isSilent  || batting.allowsOverlay)
                   && (pitching.isSilent || pitching.allowsOverlay)
        // The budget is the largest positive shortfall across the sides
        // that could be counted. A two-way player behind by one on the
        // mound and two at the plate needs two games applied; taking the
        // smaller would leave the batting row short.
        let shortfalls = [batting.behind, pitching.behind]
            .compactMap { $0 }
            .filter { $0 > 0 }
        return OverlayDecision(
            shouldOverlayFinals: overlay,
            budget: shortfalls.max(),
        )
    }

    /// The finals to actually apply, bounded and deduplicated.
    ///
    /// ⚠️ The schedule can offer more finals than the row is behind — a
    /// doubleheader, or a yesterday-retry enqueuing a game already
    /// absorbed. One game too many reads as a plausible stat line and is
    /// wrong; one too few leaves the row briefly stale and self-corrects
    /// on the next aggregate. Prefer short over inflated.
    ///
    /// Live games are never bounded away: the count describes FINALS the
    /// aggregate has not absorbed, and a game in progress is in neither.
    static func boundedGames<T>(
        _ games: [T], budget: Int?, isLive: (T) -> Bool, gameId: (T) -> Int,
    ) -> [T] {
        guard let budget else { return games }
        var seen = Set<Int>()
        var kept = 0
        return games.filter { g in
            if isLive(g) { return true }
            guard seen.insert(gameId(g)).inserted else { return false }
            guard kept < budget else { return false }
            kept += 1
            return true
        }
    }
}
