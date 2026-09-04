//
//  GameLogResolver.swift
//  BaseballStats
//
//  Turns a game-log row into the populated `Game` a box score needs.
//

import Foundation

/// A game-log row carries an id and a date; `BoxScoreView` needs a whole
/// `Game`. This rebuilds one from the slate for that row's date.
///
/// ⚠️ IT DOES NOT SYNTHESISE A `Game` FROM THE ROW, and that is the point. A
/// row has the ids, the date and the scores, so a plausible `Game` could be
/// assembled locally — but it would arrive without the linescore, without full
/// club names, and (for 2000+) without the BDL ids that carry the colour bar
/// and the play-by-play. The same game would then look poorer opened from a
/// profile than from the Scores tab, for no reason a reader could see. Fetching
/// the slate costs one round trip and produces the identical object.
enum GameLogResolver {

    enum Failure: Error { case notInSlate, unsupportedSeason }

    /// Whether a row from `season` can be resolved at all.
    ///
    /// ⚠️ THIS IS A GUARD AGAINST A FUTURE COVERAGE GAP, NOT AN EXCLUSION OF
    /// ANY ERA — AND IT IS NOT DEAD CODE. Today it is true for every row that
    /// can exist, because a game-log row only exists for a game we hold, and
    /// both branches below cover everything we hold: `/games/by-date` reaches
    /// back to 1898, and BDL serves the current season. It returns false only
    /// if those two ranges ever stop meeting — a season we ingest logs for but
    /// serve no slate for. The row would then render untappable instead of
    /// spinning and failing, which is the whole reason the check is here rather
    /// than discovered after a fetch.
    static func canResolve(season: Int) -> Bool {
        season <= RetrosheetCoverage.lastSeason
            || season == Calendar.current.component(.year, from: Date())
    }

    /// The `Game` for `gameId` on `date`, or a `Failure`.
    ///
    /// ⚠️ THE FETCHES RUN CONCURRENTLY, SO ENRICHMENT IS FREE. The historical
    /// branch needs three calls — our slate, plus BDL's for the date and the
    /// day after, because BDL buckets by UTC start and a 10pm ET game lands on
    /// tomorrow's date. Run with `async let` the wall-clock cost is the slowest
    /// one (~0.2s), not their sum.
    ///
    /// ⚠️ AND THE ENRICHMENT CANNOT BE APPLIED AFTER THE PUSH, which is why it
    /// is not deferred. A `Game` goes onto a `NavigationPath` BY VALUE and
    /// `BoxScoreView` copies it into a `@StateObject`; there is no handle from
    /// the pushing side to update it later. "Push now, enrich when it lands"
    /// would need a shared store the app does not have.
    static func resolve(
        gameId: String,
        date: Date,
        season: Int,
        api: APIClient = .shared,
        bdl: BallDontLieClient = .shared,
    ) async throws -> Game {
        guard canResolve(season: season) else { throw Failure.unsupportedSeason }

        // CURRENT SEASON: the row's `game_id` IS BDL's game id, so the slate
        // already carries everything and there is nothing to pair.
        if season > RetrosheetCoverage.lastSeason {
            guard let id = Int(gameId) else { throw Failure.notInSlate }
            let games = (try? await bdl.getGames(date: Self.ymd.string(from: date))) ?? []
            guard let match = games.first(where: { $0.id == id }) else {
                throw Failure.notInSlate
            }
            return match.toGame()
        }

        // HISTORICAL: our slate is authoritative for the box score; BDL only
        // adds the colour bar and the plays.
        let next = Calendar.current.date(byAdding: .day, value: 1, to: date) ?? date
        async let oursTask = try? api.getHistoricalGames(date: date)
        async let todayTask = try? bdl.getGames(date: Self.ymd.string(from: date))
        async let nextTask = try? bdl.getGames(date: Self.ymd.string(from: next))

        let ours = (await oursTask) ?? []
        guard !ours.isEmpty else { throw Failure.notInSlate }

        // ⚠️ ENRICHMENT IS OPTIONAL AND ITS ABSENCE IS NOT AN ERROR. A game
        // before 2002 has no BDL counterpart at all, and a handful after it
        // went unmatched (a 39-hour hole in BDL's 2002 coverage). Both open
        // correctly — `providerHasPlays` requires a non-nil `bdlGameId`, so the
        // plays section is ABSENT rather than empty, and the colour swatch
        // declines to draw rather than rendering grey. Verified on screen for
        // 2002-07-06 TB@LAA, which is one of the unmatched ones.
        var slate = ours
        if season >= Game.bdlFirstPlaysSeason {
            let theirs = BDLRetroMatch.baseballDay(
                ((await todayTask) ?? []) + ((await nextTask) ?? []),
                localDate: date)
            if !theirs.isEmpty {
                slate = BDLRetroMatch.match(retroGames: ours, bdlGames: theirs,
                                            onDiscrepancy: { _ in })
            }
        }

        // The row's id is the Retrosheet key; the slate's `gamePk` is the
        // synthetic negative encoding of it. Matching on the key the BACKEND
        // produced avoids re-implementing that encoding here — the trap
        // `_RETRO_DISPLAY_ABBR` vs `retroToBDL` already documents.
        let key = gameId.hasPrefix("retro-")
            ? String(gameId.dropFirst("retro-".count)) : gameId
        guard let match = slate.first(where: { Self.retroKey(forPk: $0.gamePk) == key })
        else { throw Failure.notInSlate }
        return match
    }

    /// The Retrosheet key a synthetic negative pk encodes — the inverse of the
    /// backend's `_synthetic_game_pk`, used only to pick a game out of a slate
    /// we already hold.
    private static func retroKey(forPk pk: Int) -> String? {
        guard pk < 0 else { return nil }
        let v = -pk
        let dateInt = v / 1_000_000
        let rest = v % 1_000_000
        var code36 = rest / 10
        let number = rest % 10
        let alphabet = Array("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        var chars = ""
        for _ in 0..<3 {
            chars = String(alphabet[code36 % 36]) + chars
            code36 /= 36
        }
        return "\(chars)\(dateInt)\(number)"
    }

    private static let ymd: DateFormatter = {
        let f = DateFormatter()
        f.calendar = .init(identifier: .gregorian)
        f.locale = .init(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "America/New_York") ?? .current
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()
}
