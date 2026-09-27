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
        /// other.
        ///
        /// ⚠️ A NEGATIVE `behind` is deliberately NOT silence here, even
        /// though an earlier note prescribed exactly that as the fix for
        /// the phantom-pitching-row veto. It was the right fix for a
        /// version of `allowsOverlay` that had only the count clause;
        /// with the second clause below it is both unnecessary and too
        /// loose. Unnecessary because a phantom row — `G = 4`, zero
        /// gamelog rows, `behind = -4` — now passes the second clause on
        /// its own and vetoes nothing. Too loose because a silent side
        /// defers entirely, so a pure pitcher whose gamelog is short by
        /// one would overlay today's finals even when the season row
        /// already counted them, and double-count. Let the clauses speak
        /// instead of muting the side.
        ///
        /// `behind == 0` remains a real "no": the season row and the
        /// gamelog agree, so adding a game would double-count.
        var isSilent: Bool {
            behind == nil && (seasonG == nil || bdlG == nil)
        }

        /// ⚠️ TWO CLAUSES, because neither signal is sufficient alone and
        /// each covers the other's blind spot.
        ///
        /// 1. `behind > 0` — our gamelog holds more games than the season
        ///    row counts, so the row is behind and must be topped up.
        ///    Catches the case balldontlie is late on: Bryce Miller,
        ///    2026-09-09, season row G=18 beside 19 gamelog rows.
        ///
        /// 2. `seasonG == bdlG && !includesToday` — today's game is in
        ///    NEITHER source. Our row matches balldontlie's, and our
        ///    gamelog has no row for the date, so nothing has absorbed
        ///    it yet. Catches a game that finished after the nightly ran:
        ///    NYM @ WSH started 17:05Z on 2026-09-27, two hours past the
        ///    15:04Z nightly.
        ///
        /// ⚠️ THE FIRST CLAUSE ALONE REGRESSED EVERY SAME-DAY FINAL. The
        /// count is `gamelog − seasonG`, and our gamelog carries holes of
        /// its own — measured at 0 to 3 games per player across a sample
        /// of seven, three of them negative. So a same-day final that no
        /// source has absorbed still reads `behind <= 0`, because the
        /// hole cancels it. Juan Soto read -1, Bo Bichette and Carson
        /// Benge -3, and all three rendered a pre-game average.
        ///
        /// ⚠️ AND A HOLE REMAINS, deliberately named rather than papered
        /// over: a Miller-shaped player — balldontlie late, our gamelog
        /// HAS the game — whose gamelog also carries a structural hole
        /// reads `behind == 0` (+1 and -1 cancelling) AND
        /// `includesToday == true`, so neither clause fires and the
        /// stale line stands. That is Hoby Milner, Corey Seager, Justin
        /// Foscue and Lazaro Montes on the TEX @ SEA night exactly. This
        /// pair of clauses fixes the same-day regression and does NOT
        /// fix that; see the note in memory for the measurement that
        /// would settle whether a same-source count removes it.
        var allowsOverlay: Bool {
            if let behind, behind > 0 { return true }
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
