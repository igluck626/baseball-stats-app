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

    /// Where a row from `season` gets its slate: our own (`/games/by-date`,
    /// Retrosheet) through the boundary the server reports, the provider after
    /// it, or nowhere.
    enum Source: Equatable { case retrosheet, provider, none }

    /// The provider's first season (the same floor as `ScoresView.bdlFirstSeason`,
    /// kept here so this model doesn't reach into a view).
    static let providerFirstSeason = 2000

    /// ⚠️ THE BOUNDARY IS THE SERVER'S, NEVER THE CALENDAR'S. The provider
    /// branch used to be "the current calendar year", so on 2027-01-01 every
    /// 2026 row went untappable until Retrosheet published 2026 and we loaded
    /// it — the server still reports 2025 all that time, and 2026 sits past it,
    /// on the provider, exactly where `resolve` already looked for it.
    /// `lastSeason` is `RetrosheetCoverage.lastSeason` (GET /meta/coverage).
    ///
    /// `.none` is the guard against a future coverage gap, not an exclusion of
    /// any era: a season past the boundary but before the provider's first
    /// season, or a season that hasn't happened yet on `today`. The row then
    /// renders untappable instead of spinning and failing.
    static func source(season: Int,
                       lastSeason: Int = RetrosheetCoverage.lastSeason,
                       today: Date = Date()) -> Source {
        if season <= lastSeason { return .retrosheet }
        let thisYear = Calendar(identifier: .gregorian).component(.year, from: today)
        if season >= providerFirstSeason && season <= thisYear { return .provider }
        return .none
    }

    /// Whether a row from `season` can be resolved at all (see `source`).
    static func canResolve(season: Int,
                           lastSeason: Int = RetrosheetCoverage.lastSeason,
                           today: Date = Date()) -> Bool {
        source(season: season, lastSeason: lastSeason, today: today) != .none
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
        dateString: String,
        season: Int,
        api: APIClient = .shared,
        bdl: BallDontLieClient = .shared,
    ) async throws -> Game {
        guard canResolve(season: season) else { throw Failure.unsupportedSeason }

        // ⚠️ THE DATE IS PARSED IN THE DEVICE'S TIMEZONE, NOT ET, AND THAT IS
        // NOT A STYLE CHOICE. A game-log row carries `game_date` as a plain
        // "yyyy-MM-dd" with no zone; the consumers below disagree about what a
        // `Date` means. `APIClient.getHistoricalGames` formats it back with a
        // `.current` formatter, and `BDLRetroMatch.baseballDay` wants the local
        // day the Scores tab passes it. Parsing in ET and handing the result to
        // a `.current` formatter shifts the request a day on any device west of
        // Eastern — which asked for the wrong slate and made every historical
        // era fail `notInSlate` while 2026, which never round-trips a Date,
        // passed. Same string in, same string out.
        guard let date = Self.deviceYMD.date(from: dateString) else {
            throw Failure.notInSlate
        }

        // PAST THE BOUNDARY: the row's `game_id` IS BDL's game id, so the slate
        // already carries everything and there is nothing to pair.
        if source(season: season) == .provider {
            guard let id = Int(gameId) else { throw Failure.notInSlate }
            let games = (try? await bdl.getGames(date: dateString)) ?? []
            guard let match = games.first(where: { $0.id == id }) else {
                throw Failure.notInSlate
            }
            return match.toGame()
        }

        // HISTORICAL: our slate is authoritative for the box score; BDL only
        // adds the colour bar and the plays.
        let next = Calendar.current.date(byAdding: .day, value: 1, to: date) ?? date
        async let oursTask = try? api.getHistoricalGames(date: date)
        async let todayTask = try? bdl.getGames(date: dateString)
        async let nextTask = try? bdl.getGames(date: Self.deviceYMD.string(from: next))

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

    /// ⚠️ `.current`, TO MATCH `APIClient.ymd`. See the note in `resolve`: the
    /// point is that a date string parsed here and re-formatted there must come
    /// back identical, which only holds if both use the same zone.
    private static let deviceYMD: DateFormatter = {
        let f = DateFormatter()
        f.calendar = .init(identifier: .gregorian)
        f.locale = .init(identifier: "en_US_POSIX")
        f.timeZone = .current
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()
}
