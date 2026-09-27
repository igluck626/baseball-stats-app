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
    /// One side's inputs — batting or pitching, never mixed.
    ///
    /// ⚠️ PER-SIDE ONLY, NO UNION. Batting rows are compared against
    /// batting G and pitching rows against pitching G. A union of dates
    /// across both sides was measured and rejected: it does close
    /// Ohtani's gap (his hitting G counts pitching-only appearances that
    /// his hitting gamelog has no row for), but across 62 sampled
    /// players it left 59% still mismatched and introduced over-counts
    /// where a pitcher's batting G is the wrong denominator — Jack
    /// Leiter read +14. Per-side keeps the arithmetic saying one thing.
    struct Side: Equatable {
        let seasonG: Int?
        let bdlG: Int?
        /// RAW gamelog rows season-to-date, not distinct dates.
        let gamelogRows: Int?
        /// ⚠️ HOW THIS IS COMPUTED, because the whole gate turns on it:
        /// the backend takes every gamelog row for the season up to and
        /// including the queried date — `finalGameDateET`, the ET
        /// calendar date of a final on today's schedule — and reports
        /// whether ANY of them carries exactly that date
        /// (`any(r.game_date == game_date)`). It is a DATE question, not
        /// a count: both halves of a doubleheader satisfy it, which is
        /// right, since it asks only whether our gamelog has reached
        /// that day. False means our per-game ingest has not written the
        /// day yet.
        let includesToday: Bool
        /// Whether the SEASON ROW already counts today's game.
        ///
        /// Derived by `seasonRowIncludes(written:firstPitch:)` from the
        /// row's own write time against the game's scheduled start. nil
        /// when either is missing, and nil does not block — the
        /// double-count this guards needs a definite yes.
        let seasonRowIncludesToday: Bool?

        /// How many games the season row is behind our gamelog, or nil
        /// when the count is unavailable.
        var behind: Int? {
            guard let gamelogRows, let seasonG else { return nil }
            return gamelogRows - seasonG
        }

        /// ⚠️ A side answers one of THREE things, not a Bool. The
        /// previous `allowsOverlay: Bool` conflated "fire" with "I have
        /// nothing to say", and the caller combined sides with
        /// `(isSilent || allows) && (isSilent || allows)` — so TWO
        /// ABSTAINING SIDES produced `true && true` and the overlay
        /// fired on the strength of nobody having an opinion. A player
        /// with no season row on either side would have had today's
        /// finals added to nothing.
        enum Vote: Equatable {
            /// A clause matched: this side wants the finals applied.
            case fire
            /// Counted, and the answer is no. Refusal outranks fire.
            case refuse
            /// Nothing to say. Never fires by itself and never blocks.
            case abstain
        }

        var vote: Vote {
            // Clause 1 — our gamelog holds more rows than the season row
            // counts, so the row is missing games. Suppressed when the
            // row already has today, or today would go on twice.
            if let behind, behind > 0, seasonRowIncludesToday != true { return .fire }
            // Clause 2 needs both counts to compare; without them this
            // side cannot speak at all.
            guard let seasonG, let bdlG else { return .abstain }
            // Today's game is in NEITHER source — nothing has absorbed it.
            if seasonG == bdlG, !includesToday { return .fire }
            // ⚠️ A negative `behind` abstains rather than refusing: our
            // gamelog holds FEWER rows than the season row counts, which
            // says nothing about today and is the BDL-direct path's
            // business. Counting rows rather than dates makes this rare
            // — the three negatives measured on 2026-09-27 were all
            // doubleheaders and now read zero.
            if let behind, behind < 0 { return .abstain }
            // Counted, level or ahead, and today is already in the
            // gamelog: adding anything would double-count.
            return .refuse
        }

        /// How many games this side is short, when it is short at all.
        var shortfall: Int? {
            guard let behind, behind > 0 else { return nil }
            return behind
        }
    }

    /// Whether a season row written at `written` already counts a game
    /// that started at `firstPitch`.
    ///
    /// ⚠️ THE ROW'S OWN WRITE TIME IS THE SIGNAL, and the intent was
    /// already recorded beside the column: a box-score line is in the
    /// row if the game started BEFORE the stamp, and missing if it
    /// started after. `stats_last_updated` is per row — observed values
    /// differ player to player by seconds within one nightly pass — so
    /// this is a real per-player reading, not a global clock.
    ///
    /// ⚠️ AND IT IS A LOWER BOUND, not proof. The stamp says when WE
    /// wrote the row; what we wrote is balldontlie's aggregate, which
    /// may itself have been behind at that moment. So a row written
    /// after first pitch MIGHT still lack the game — which is why a
    /// `true` here only ever SUPPRESSES clause 1 and never fires
    /// anything, and why clause 2 is left to catch what it misses.
    static func seasonRowIncludes(written: Date?, firstPitch: Date?) -> Bool? {
        guard let written, let firstPitch else { return nil }
        return written >= firstPitch
    }

    /// ⚠️ REFUSAL OUTRANKS FIRE, AND ABSTENTION DECIDES NOTHING. The
    /// overlay runs only when some side actually asks for it and no side
    /// refuses. Two abstaining sides therefore produce NO overlay, which
    /// the previous Bool combination got backwards.
    static func decide(batting: Side, pitching: Side) -> OverlayDecision {
        let votes = [batting.vote, pitching.vote]
        let overlay = !votes.contains(.refuse) && votes.contains(.fire)
        // The budget is the largest positive shortfall across the sides
        // that could be counted. A two-way player behind by one on the
        // mound and two at the plate needs two games applied; taking the
        // smaller would leave the batting row short.
        let shortfalls = [batting.shortfall, pitching.shortfall].compactMap { $0 }
        return OverlayDecision(
            shouldOverlayFinals: overlay,
            budget: overlay ? shortfalls.max() : nil,
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
