//
//  Scores.swift
//  BaseballStats
//
//  Codable models for MLB Stats API responses consumed by the
//  Scores tab. Two payload shapes are decoded:
//
//    1. `ScheduleResponse` — list of games for one date, with team
//       info, current score, linescore, and decisions hydrated in.
//    2. `BoxScoreResponse` — per-team batting + pitching lineups
//       for one game.
//
//  Field names mirror the API exactly so default Codable decoding
//  works without a CodingKeys map. Anything we don't need is
//  omitted; the decoder ignores unknown keys by default.
//

import Foundation

// MARK: - Schedule

struct ScheduleResponse: Codable {
    let dates: [ScheduleDate]
}

struct ScheduleDate: Codable {
    let date: String
    let games: [Game]
}

struct Game: Codable, Identifiable, Hashable {
    let gamePk: Int
    let gameDate: String              // ISO-8601 UTC, e.g. "2026-05-15T19:05:00Z"
    let status: GameStatus
    let teams: GameTeams
    let venue: Venue?
    let linescore: Linescore?
    let decisions: Decisions?
    // Authoritative BDL team ids straight off `BDLGame.{away,home}_team.id`.
    // `teams.{away,home}.team.id` carries the MLBAM id (good for logos);
    // these carry the BDL id (good for joining against BDL lineup /
    // stat rows). Optional so the struct remains decodable from older
    // payloads that didn't include them.
    let bdlAwayTeamId: Int?
    let bdlHomeTeamId: Int?
    /// The provider's id for this game, when one is known.
    ///
    /// Present for a BDL-served game (where it equals `gamePk`) and, for a
    /// Retrosheet-served game, attached at slate load by matching against
    /// BDL's list for the same day — see `BDLRetroMatch`. It exists for ONE
    /// reason: play-by-play is BDL's alone, and a Retrosheet game's negative
    /// `gamePk` cannot address it. nil means the match found nothing, which
    /// costs the plays list and nothing else.
    let bdlGameId: Int?

    var id: Int { gamePk }

    /// The SEASON this game belongs to, from its own date.
    ///
    /// Every source-routing question below is a question about the season, and
    /// the date is the one field every payload carries whatever served it —
    /// which is why these are derived here rather than from the id.
    var seasonYear: Int? {
        guard let d = startDate else { return Int(gameDate.prefix(4)) }
        return Calendar(identifier: .gregorian).component(.year, from: d)
    }

    /// Whether this game's BOX SCORE should be built from our Retrosheet
    /// tables rather than fetched from BDL.
    ///
    /// 2025 and earlier. Not a statement about age but about quality: BDL's
    /// `/lineups` returns ZERO rows for every season through 2025 and is
    /// populated only for 2026, so before then its box score has no batting
    /// order and falls back to each player's CAREER position, spelled out
    /// ("Third Baseman") and unrelated to where he actually played that day.
    /// Our tables carry the real lineup slot, the game's own position, the
    /// decisions, the pre-game season line and the pitch count.
    ///
    /// The boundary is the SEASON, not "is it finished": Retrosheet publishes
    /// complete through 2025 and our rows stop dead at 2025-09-28, so there is
    /// no lag window and no in-progress edge to handle.
    var usesRetrosheetBoxScore: Bool { (seasonYear ?? 0) <= RetrosheetCoverage.lastSeason }

    /// Whether the PROVIDER has play-by-play for this game. Independent of
    /// where the box score comes from: BDL's plays reach back to 2002, so a
    /// 2005 game is boxed from our tables and still gets its plays.
    var providerHasPlays: Bool {
        guard let y = seasonYear else { return false }
        return y >= Self.bdlFirstPlaysSeason && bdlGameId != nil
    }

    /// Last season Retrosheet publishes, and the last our tables hold.
    ///
    /// ⚠️ NO LONGER THE SOURCE OF TRUTH — see `RetrosheetCoverage`, which reads
    /// it from the service so it moves on its own when a season is published
    /// and ingested. This remains only as the value that ships in the binary
    /// and the value a failed request falls back to, and the two must stay in
    /// step, so it forwards rather than holding its own copy.
    static var retrosheetLastSeason: Int { RetrosheetCoverage.fallbackLastSeason }
    /// First season BDL serves play-by-play. Measured, not assumed: 2000 and
    /// 2001 return no games at all, 2002 onward return a full play list.
    static let bdlFirstPlaysSeason = 2002

    /// True for a game served by `/games/by-date` rather than by BDL.
    ///
    /// ⚠️ NARROWED. This used to be THE source-routing signal, and it answered
    /// four different questions at once: which box score to build, whether
    /// plays existed, whether a linescore existed, and what the card should
    /// offer. That worked only while "served by us" and "pre-2000" and "has no
    /// linescore" were the same fact. They are not: we now hold better data
    /// than BDL for 2000-2025, and those games have linescores and plays.
    ///
    /// It now means EXACTLY what it says — this `Game` came from our endpoint,
    /// so its `gamePk` is synthetic and negative. Use `usesRetrosheetBoxScore`
    /// for the box score, `providerHasPlays` for plays, and `linescore != nil`
    /// for the grid.
    var isHistorical: Bool { gamePk < 0 }

    /// Bucketed game phase so the UI can branch on intent rather
    /// than the raw API state strings.
    enum Phase {
        case preview        // scheduled / not yet started
        case live           // in progress
        case final          // ended
        case postponed      // postponed — won't be played today
        case other          // canceled / suspended
    }

    var phase: Phase {
        switch status.abstractGameState {
        case "Preview":   return .preview
        case "Live":      return .live
        case "Final":     return .final
        case "Postponed": return .postponed
        default:          return .other
        }
    }

    /// `gameDate` parsed via `Date.ISO8601FormatStyle`. nil if the
    /// API ever ships an unexpected shape — caller falls back to
    /// the raw string.
    var startDate: Date? {
        try? Date(gameDate, strategy: .iso8601)
    }
}

struct GameStatus: Codable, Hashable {
    let abstractGameState: String     // "Preview", "Live", "Final"
    let detailedState: String         // "Scheduled", "In Progress", "Final", "Postponed", …
    let statusCode: String?
    let codedGameState: String?
}

struct GameTeams: Codable, Hashable {
    let away: GameTeam
    let home: GameTeam
}

struct GameTeam: Codable, Hashable {
    let team: TeamInfo
    let score: Int?
    let leagueRecord: TeamRecord?
    let isWinner: Bool?
    let probablePitcher: PlayerInfo?
}

struct TeamInfo: Codable, Hashable {
    let id: Int
    let name: String
    let abbreviation: String?

    // Carried a `logoURL` off `midfield.mlbstatic.com` until 2026-08-15. Its
    // only consumer was TeamLogoCache, which went at the same time.
}

struct TeamRecord: Codable, Hashable {
    let wins: Int?
    let losses: Int?
    let pct: String?
}

// MARK: - Today's-results record overlay

/// Shared logic for folding today's already-final games — which the
/// standings haven't absorbed yet — into a team's displayed W-L. Used
/// by the Standings tab (which surfaces the deltas with a "†"), and by
/// the Scores / Home record displays (which silently apply them).
///
/// A game counts when it's `STATUS_FINAL`, its **ET calendar date** is
/// today, and it started after the standings' `lastUpdated` cutoff
/// (start is strictly before end, so `start > cutoff` ⟹ the result
/// post-dates the standings — a conservative guard that never
/// double-counts an already-absorbed game). When `lastUpdated` is nil
/// (no timestamp available, e.g. BDL standings) every today-ET final is
/// applied.
enum TodayRecordAdjustments {

    /// One today-ET final game's decided outcome, normalized to BDL
    /// team ids + scores. `start` is kept so a doubleheader's two games
    /// can be applied to a streak in chronological order.
    struct FinalResult {
        let homeId: Int
        let awayId: Int
        let homeScore: Int
        let awayScore: Int
        let start: Date
        var homeWon: Bool { homeScore > awayScore }
    }

    /// The today-ET, post-cutoff, non-tie final games — the single
    /// qualifying set that both `deltas` and `recalculateGB` work from,
    /// sorted chronologically. Filtering lives here so the W/L deltas
    /// and the STRK / split / run-diff adjustments always agree on
    /// which games count.
    static func qualifyingResults(
        from games: [Game],
        lastUpdated: String?,
        now: Date = Date(),
        alwaysAdmitPriorDay: Bool = false,
    ) -> [FinalResult] {
        let etFormatter = DateFormatter()
        etFormatter.timeZone = TimeZone(identifier: "America/New_York")
        etFormatter.dateFormat = "yyyy-MM-dd"

        let etHourFormatter = DateFormatter()
        etHourFormatter.timeZone = TimeZone(identifier: "America/New_York")
        etHourFormatter.dateFormat = "HH"

        let todayET = etFormatter.string(from: now)
        let currentETHour = Int(etHourFormatter.string(from: now)) ?? 12

        // Yesterday's ET date is admitted too — always under
        // `alwaysAdmitPriorDay`, and otherwise only before 6 AM ET.
        //
        // THE NARROW FORM EXISTS TO STOP DOUBLE-COUNTING, and it was the only
        // protection there was: with no way to tell an absorbed game from an
        // unabsorbed one, dropping yesterday at 6 AM was a proxy for "by now
        // the base surely has it". The proxy is wrong in both directions, and
        // the direction that hurt was early — BDL trails a final by ~9.4h, so
        // a game ending at 22:00 ET is absorbed around 07:24 ET, and between
        // 06:00 and then the adjustment had already stopped counting it while
        // the base still lacked it. That hour-and-a-half to four-and-a-half
        // hour hole is the morning reversion, and it is why an 08:00 ET check
        // saw last night's game vanish from the record and the streak.
        //
        // ⚠️ `.counted` MAKES THE PROXY REDUNDANT, WHICH IS WHY IT MAY WIDEN.
        // It retires a game by MEASURING that the base contains it, so a game
        // the base has absorbed is dropped whatever its date. Keeping the
        // narrow window under `.counted` means protecting against
        // double-counting twice, with the cruder guard firing first and
        // deciding the outcome.
        //
        // ⚠️ AND IT WIDENS PER-CASE, NOT GLOBALLY. `.unanchored` and
        // `.anchored` still get the 6 AM form, because they still need it:
        // `.unanchored` has no absorption signal at all, so an always-on prior
        // day would re-add every one of yesterday's games to a base that
        // already contains them. The caller passes
        // `Absorption.admitsPriorDayAllDay`, so the window can never be wider
        // than the absorption that has to police it.
        var validETDates: Set<String> = [todayET]
        if alwaysAdmitPriorDay || currentETHour < 6,
           let yesterday = Calendar.current.date(byAdding: .day, value: -1, to: now) {
            validETDates.insert(etFormatter.string(from: yesterday))
        }

        let cutoff = parseLastUpdated(lastUpdated)

        var out: [FinalResult] = []
        for game in games {
            let result: FinalResult? = {
                guard game.phase == .final,
                      let start = game.startDate,
                      validETDates.contains(etFormatter.string(from: start)) else { return nil }
                if let cutoff, start <= cutoff { return nil }
                guard let homeId = game.bdlHomeTeamId,
                      let awayId = game.bdlAwayTeamId,
                      let homeScore = game.teams.home.score,
                      let awayScore = game.teams.away.score,
                      homeScore != awayScore else { return nil }
                return FinalResult(
                    homeId: homeId, awayId: awayId,
                    homeScore: homeScore, awayScore: awayScore, start: start,
                )
            }()
            if let result { out.append(result) }
        }
        return out.sorted { $0.start < $1.start }
    }

    /// How much of today the BASE RECORD has already absorbed.
    ///
    /// This exists because the base and the cutoff used to come from different
    /// services. The base is BDL's standings, which carry no timestamp; the
    /// cutoff was our backend's `last_updated`. So the adjustment asked "has
    /// OUR BACKEND absorbed this game?" and applied the answer to BDL's
    /// numbers. Once BDL absorbed a final, its base already contained the win
    /// and the delta added it a second time — the favourite's record climbed by
    /// one every time Home was re-entered.
    ///
    /// The fix is to stop asking a clock and start counting. A standings row's
    /// games played is `W + L` — verified against all 30 teams, exactly, with
    /// no ties anywhere — so it is not a second quantity that can drift from
    /// the record; it IS the record. Given a games-played count anchored to a
    /// known time, "how many of today's finals does this base already contain?"
    /// is arithmetic rather than inference.
    enum Absorption {
        /// No anchor available: apply every post-cutoff final. Correct only
        /// where the cutoff itself is trustworthy — i.e. where the base and the
        /// `lastUpdated` come from the SAME response.
        case unanchored

        /// `gamesPlayed` is each team's `W + L` at a known instant (our
        /// backend's standings row, whose `last_updated` is the `lastUpdated`
        /// passed alongside). `current` is what is displayed NOW — BDL's base
        /// on a good tick, or the previously-adjusted values if the fetch
        /// failed. Both keyed by BDL team id.
        case anchored(gamesPlayed: [Int: Int], current: [Int: TeamRecord])

        /// `feedGamesPlayed` is what BDL's GAME FEED says each club has
        /// actually played, right now (`recent_form.games_played`), and
        /// `current` is the record being displayed. Both keyed by BDL team id.
        ///
        /// ⚠️ THIS IS THE ONE THAT NEEDS NO CLOCK. `.anchored` measures how far
        /// the displayed record has moved past a snapshot we took, which is
        /// honest but bounded by how fresh that snapshot is. This measures how
        /// far the displayed record is BEHIND the games that have actually been
        /// played — an independent live count against the base, so "does the
        /// base contain this game?" stops being an inference and becomes
        /// subtraction.
        ///
        /// ⚠️ AND IT MAKES THE `lastUpdated` CUTOFF UNNECESSARY, WHICH IS THE
        /// POINT. That cutoff drops any game starting before our last fetch, on
        /// the reasoning that we must therefore already have it. The reasoning
        /// is false while the upstream lags: BDL trails a final by ~9.4h, so a
        /// game can start before our fetch and still be absent from the base.
        /// That gap is what made the record correct all evening and wrong the
        /// next morning. Under this case the cutoff is not applied at all.
        case counted(feedGamesPlayed: [Int: Int], current: [Int: TeamRecord])

        /// Whether the `lastUpdated` cutoff should filter the candidate set.
        ///
        /// It must NOT under `.counted`: the cutoff would remove games from the
        /// set before absorption is measured, and absorption is exactly what
        /// this case computes properly. Removing them first would re-introduce
        /// the fault by the back door.
        var appliesLastUpdatedCutoff: Bool {
            if case .counted = self { return false }
            return true
        }

        /// Whether yesterday's ET date stays in scope ALL DAY rather than only
        /// before 6 AM.
        ///
        /// True only for `.counted`, and for the same reason as the cutoff: a
        /// game is retired here by measuring that the base contains it, so the
        /// date window no longer has to guess. Under the other two cases the
        /// narrow window is the only double-count protection there is and must
        /// stay — see the note in `qualifyingResults`.
        var admitsPriorDayAllDay: Bool {
            if case .counted = self { return true }
            return false
        }
    }

    /// Games of `team`'s post-cutoff finals that the base already reflects.
    ///
    /// `current - anchor` is how far the displayed record has already moved
    /// past the anchored count, whether that movement came from BDL catching up
    /// or from an earlier run of this very adjustment. That is what makes the
    /// whole thing idempotent: applying it twice cannot add twice, because the
    /// first application is visible to the second.
    static func absorbedCount(
        _ absorption: Absorption,
        teamId: Int,
        resultCount: Int,
    ) -> Int? {
        switch absorption {
        case .unanchored:
            return 0
        case let .counted(feedGamesPlayed, current):
            // How many games the base is BEHIND the feed. Everything the feed
            // has that the base does not is unabsorbed; the rest of today's
            // results are already in it.
            guard let played = feedGamesPlayed[teamId],
                  let rec = current[teamId],
                  let w = rec.wins, let l = rec.losses else { return nil }
            let missing = max(0, played - (w + l))
            // Clamped at both ends. A base somehow AHEAD of the feed absorbs
            // everything rather than yielding a negative count.
            //
            // ⚠️ AND THE LOWER CLAMP MARKS A REAL LIMIT, NOT AN ARBITRARY ONE.
            // When `missing` exceeds today's slate — the nightly skipped a day,
            // say, so the base is short two games while only one was played
            // today — absorbed floors at 0 and every one of today's games is
            // applied. That is right, and it is also all this can do: a
            // TODAY-adjustment can only add games it can see, and yesterday's
            // missing game is not in today's set to add. The record stays a
            // game light until the standings themselves catch up.
            //
            // So do not reach for a bigger window here to "recover" it. The
            // fix for a base that has fallen multiple days behind is refreshing
            // the standings more often; this overlay exists to cover the hours
            // between a game ending and the next refresh, not to substitute for
            // one that never ran.
            return min(resultCount, max(0, resultCount - missing))
        case let .anchored(gamesPlayed, current):
            // No anchor for this team means we cannot tell absorbed from
            // unabsorbed. Adding blind is what produced an unbounded climb, so
            // the answer is nil and the caller adds nothing.
            guard let anchor = gamesPlayed[teamId],
                  let rec = current[teamId],
                  let w = rec.wins, let l = rec.losses else { return nil }
            return max(0, (w + l) - anchor)
        }
    }

    /// Today's post-cutoff finals per BDL team id, chronological, with the ones
    /// the base ALREADY reflects dropped.
    ///
    /// THE single filtered set. `deltas` and `recalculateGB` both work from it
    /// and must never disagree about which games count — a record corrected for
    /// a game while the streak beside it rolls that same game forward again is
    /// the same class of fault as the record double-counting in the first
    /// place, just one field over.
    ///
    /// Chronological because a doubleheader's two games are absorbed in the
    /// order they were played, and the streak walks them in that order.
    static func unabsorbedResultsByTeam(
        from games: [Game],
        lastUpdated: String?,
        absorption: Absorption,
        now: Date = Date(),
    ) -> [Int: [FinalResult]] {
        let results = qualifyingResults(
            from: games,
            // See `Absorption.appliesLastUpdatedCutoff`: under `.counted` the
            // cutoff is dropped, because absorption is measured rather than
            // inferred and filtering first would discard the very games the
            // measurement exists to catch.
            lastUpdated: absorption.appliesLastUpdatedCutoff ? lastUpdated : nil,
            now: now,
            alwaysAdmitPriorDay: absorption.admitsPriorDayAllDay,
        )
        guard !results.isEmpty else { return [:] }

        var byTeam: [Int: [FinalResult]] = [:]
        for r in results {
            byTeam[r.homeId, default: []].append(r)
            byTeam[r.awayId, default: []].append(r)
        }

        var out: [Int: [FinalResult]] = [:]
        for (teamId, teamResults) in byTeam {
            guard let absorbed = absorbedCount(absorption, teamId: teamId,
                                               resultCount: teamResults.count),
                  absorbed < teamResults.count else { continue }
            out[teamId] = Array(teamResults.dropFirst(absorbed))
        }
        return out
    }

    /// Per-team `(wDelta, lDelta)` keyed by **BDL team id**.
    static func deltas(
        from games: [Game],
        lastUpdated: String?,
        absorption: Absorption,
        now: Date = Date(),
    ) -> [Int: (wDelta: Int, lDelta: Int)] {
        var out: [Int: (wDelta: Int, lDelta: Int)] = [:]
        for (teamId, teamResults) in unabsorbedResultsByTeam(
            from: games, lastUpdated: lastUpdated, absorption: absorption, now: now,
        ) {
            var delta = (wDelta: 0, lDelta: 0)
            for r in teamResults {
                let won = (r.homeId == teamId) ? r.homeWon : !r.homeWon
                if won { delta.wDelta += 1 } else { delta.lDelta += 1 }
            }
            if delta.wDelta != 0 || delta.lDelta != 0 { out[teamId] = delta }
        }
        return out
    }

    /// Apply BDL-id-keyed deltas onto a `TeamRecord` dict, returning a
    /// new dict with today's results folded in. Teams without a base
    /// record are left untouched (a bump with no base would render a
    /// misleading partial line).
    static func apply(
        _ deltas: [Int: (wDelta: Int, lDelta: Int)],
        to records: [Int: TeamRecord],
    ) -> [Int: TeamRecord] {
        guard !deltas.isEmpty else { return records }
        var out = records
        for (bdlId, delta) in deltas {
            guard let base = out[bdlId] else { continue }
            out[bdlId] = TeamRecord(
                wins:   base.wins.map   { $0 + delta.wDelta },
                losses: base.losses.map { $0 + delta.lDelta },
                pct:    base.pct,
            )
        }
        return out
    }

    /// Tolerant ISO-8601 parse for the backend's `last_updated` stamp.
    /// Python's `datetime.isoformat()` emits 6-digit microseconds
    /// (e.g. "2026-06-11T15:03:45.395494Z"), which `ISO8601DateFormatter`
    /// (millisecond-only) won't parse — so we truncate to milliseconds,
    /// then strip fractional seconds entirely as a last resort.
    static func parseLastUpdated(_ iso: String?) -> Date? {
        guard let iso else { return nil }

        // Standard ISO-8601 with fractional seconds (3-digit ms).
        let msFormatter = ISO8601DateFormatter()
        msFormatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = msFormatter.date(from: iso) { return date }

        // Python's 6-digit microseconds → truncate to 3-digit ms.
        // "…45.395494Z" → "…45.395Z".
        let truncated = iso.replacingOccurrences(
            of: #"(\.\d{3})\d+(Z|[+-]\d{2}:?\d{2})$"#,
            with: "$1$2",
            options: .regularExpression,
        )
        if let date = msFormatter.date(from: truncated) { return date }

        // Last resort: drop fractional seconds altogether.
        let stripped = iso.replacingOccurrences(
            of: #"\.\d+(Z|[+-]\d{2}:?\d{2})$"#,
            with: "$1",
            options: .regularExpression,
        )
        return ISO8601DateFormatter().date(from: stripped)
    }

    /// Modern wild-card spots per league (current season only — this
    /// overlay never applies to historical years).
    private static let wildCardSpots = 3

    /// Recompute Games-Back (vs the division leader) and Wild-Card
    /// Games-Back (vs the last wild-card spot) from the today-adjusted
    /// records, so the GB / WCGB columns stay consistent with the
    /// bumped W-L. Returns a dict keyed by Lahman `team_id` →
    /// `(gb, wcgb)` display strings; empty when there are no
    /// adjustments (callers then keep the backend / leader-derived
    /// values). GB / WCGB are recomputed for EVERY team — one win
    /// shifts the whole division's / league's gaps, not just its row.
    /// `absorption` MUST be the same value passed to `deltas` for the same
    /// tick. The two derive their game set from `unabsorbedResultsByTeam`
    /// together; hand them different absorptions and the record and the streak
    /// will describe different evenings.
    static func recalculateGB(
        standings: [TeamStanding],
        games: [Game],
        adjustments: [Int: (wDelta: Int, lDelta: Int)],
        lastUpdated: String?,
        absorption: Absorption,
        now: Date = Date(),
    ) -> [String: (gb: String, wcgb: String, pct: Double?, strk: String?, homeW: Int?, homeL: Int?, awayW: Int?, awayL: Int?, runsScored: Int?, runsAllowed: Int?)] {
        guard !adjustments.isEmpty else { return [:] }

        struct Rec {
            let teamId: String
            let league: String
            let division: String
            let w: Int
            let l: Int
            var pct: Double {
                let g = w + l
                return g > 0 ? Double(w) / Double(g) : 0
            }
        }

        // Apply the W/L deltas (bridging Lahman team_id → BDL id).
        var recs: [Rec] = []
        var pctByTeam: [String: Double] = [:]
        for s in standings {
            guard let teamId = s.team_id, let league = s.league,
                  let division = s.division, let w0 = s.W, let l0 = s.L else { continue }
            let delta = lahmanToBDLTeamId[teamId].flatMap { adjustments[$0] }
            let rec = Rec(
                teamId:   teamId,
                league:   league,
                division: division,
                w:        w0 + (delta?.wDelta ?? 0),
                l:        l0 + (delta?.lDelta ?? 0),
            )
            recs.append(rec)
            pctByTeam[teamId] = rec.pct
        }

        // Today's decided games per BDL team id (chronological), so we
        // can roll the streak forward and tally home/away splits + runs
        // off the same qualifying set the W/L deltas came from.
        // Same filtered set the W/L deltas came from — see
        // `unabsorbedResultsByTeam`. Under `.unanchored` nothing is dropped, so
        // this is exactly the previous behaviour.
        let byTeam = unabsorbedResultsByTeam(
            from: games, lastUpdated: lastUpdated, absorption: absorption, now: now,
        )
        var todayByBDL: [Int: [(won: Bool, isHome: Bool, scored: Int, allowed: Int)]] = [:]
        for (teamId, teamResults) in byTeam {
            for r in teamResults {
                let isHome = (r.homeId == teamId)
                todayByBDL[teamId, default: []].append((
                    won:     isHome ? r.homeWon : !r.homeWon,
                    isHome:  isHome,
                    scored:  isHome ? r.homeScore : r.awayScore,
                    allowed: isHome ? r.awayScore : r.homeScore,
                ))
            }
        }
        // Base rows keyed by Lahman team_id, for the STRK / split / run
        // baselines the deltas fold into.
        let standingByTeamId = Dictionary(
            standings.compactMap { s in s.team_id.map { ($0, s) } },
            uniquingKeysWith: { first, _ in first },
        )

        var gbByTeam: [String: String] = [:]
        var wcgbByTeam: [String: String] = [:]

        // GB — per (league, division), measured off the best record.
        var divisionLeaders: Set<String> = []
        for (_, group) in Dictionary(grouping: recs, by: { "\($0.league)|\($0.division)" }) {
            guard let leader = group.max(by: { ($0.pct, $0.w) < ($1.pct, $1.w) }) else { continue }
            divisionLeaders.insert(leader.teamId)
            for team in group {
                let gb = Double((leader.w - team.w) + (team.l - leader.l)) / 2.0
                gbByTeam[team.teamId] = formatGB(gb)
            }
        }

        // WCGB — per league. Division leaders are in via their division
        // ("—"). Among non-division-leaders sorted best→worst, the top
        // `wildCardSpots` hold a spot: each spot-holder above the last
        // shows how far it leads the next spot ("+X.X"); the last in-
        // spot shows "—". Everyone below trails that last in-spot.
        for (_, group) in Dictionary(grouping: recs, by: { $0.league }) {
            for team in group where divisionLeaders.contains(team.teamId) {
                wcgbByTeam[team.teamId] = "—"
            }
            let contenders = group
                .filter { !divisionLeaders.contains($0.teamId) }
                .sorted { ($0.pct, $0.w) > ($1.pct, $1.w) }
            guard contenders.count > wildCardSpots else {
                for team in contenders { wcgbByTeam[team.teamId] = "—" }
                continue
            }
            // Every team's WCGB is measured against the same anchor —
            // the last in-spot (3rd) team. Teams above it show how far
            // they lead it ("+X.X"); the 3rd itself shows "—"; teams
            // below show games back from it.
            let lastIn = contenders[wildCardSpots - 1]
            for (i, team) in contenders.enumerated() {
                if i < wildCardSpots - 1 {
                    let lead = Double((team.w - lastIn.w) + (lastIn.l - team.l)) / 2.0
                    wcgbByTeam[team.teamId] = lead > 0 ? "+" + formatGB(lead) : "—"
                } else if i == wildCardSpots - 1 {
                    wcgbByTeam[team.teamId] = "—"
                } else {
                    let back = Double((lastIn.w - team.w) + (team.l - lastIn.l)) / 2.0
                    wcgbByTeam[team.teamId] = formatGB(back)
                }
            }
        }

        var out: [String: (gb: String, wcgb: String, pct: Double?, strk: String?, homeW: Int?, homeL: Int?, awayW: Int?, awayL: Int?, runsScored: Int?, runsAllowed: Int?)] = [:]
        for id in Set(gbByTeam.keys).union(wcgbByTeam.keys) {
            let base = standingByTeamId[id]
            let today = lahmanToBDLTeamId[id].flatMap { todayByBDL[$0] } ?? []

            // STRK — roll the base streak forward across today's games.
            var strk = base?.streak_code
            for game in today { strk = evolveStreak(strk, won: game.won) }

            // Home / away splits — base + today's home/away outcomes.
            let homeWonToday  = today.filter { $0.isHome &&  $0.won }.count
            let homeLostToday = today.filter { $0.isHome && !$0.won }.count
            let awayWonToday  = today.filter { !$0.isHome &&  $0.won }.count
            let awayLostToday = today.filter { !$0.isHome && !$0.won }.count

            // Run diff — base runs scored/allowed + today's.
            let scoredToday  = today.reduce(0) { $0 + $1.scored }
            let allowedToday = today.reduce(0) { $0 + $1.allowed }

            out[id] = (
                gb:          gbByTeam[id] ?? "-",
                wcgb:        wcgbByTeam[id] ?? "-",
                pct:         pctByTeam[id],
                strk:        strk,
                homeW:       base?.home_w.map { $0 + homeWonToday },
                homeL:       base?.home_l.map { $0 + homeLostToday },
                awayW:       base?.away_w.map { $0 + awayWonToday },
                awayL:       base?.away_l.map { $0 + awayLostToday },
                runsScored:  base?.runs_scored.map { $0 + scoredToday },
                runsAllowed: base?.runs_allowed.map { $0 + allowedToday },
            )
        }
        return out
    }

    /// Roll a streak code ("W3" / "L1" / "-") forward by one game.
    /// Extends the run when the result matches the current direction,
    /// otherwise flips to "W1" / "L1".
    private static func evolveStreak(_ current: String?, won: Bool) -> String {
        let prefix = won ? "W" : "L"
        if let cur = current, cur.hasPrefix(prefix), let n = Int(cur.dropFirst()) {
            return "\(prefix)\(n + 1)"
        }
        return "\(prefix)1"
    }

    /// "—" for 0 (leader / level), otherwise one decimal place rounded
    /// to the nearest half game ("2.0", "0.5", "6.5").
    private static func formatGB(_ value: Double) -> String {
        if value == 0 { return "—" }
        return String(format: "%.1f", (value * 2).rounded() / 2)
    }
}

struct Venue: Codable, Hashable {
    let id: Int?
    let name: String?
}

// MARK: - Linescore

struct Linescore: Codable, Hashable {
    let currentInning: Int?
    let currentInningOrdinal: String?  // "9th", "Top 7th", …
    let inningState: String?           // "Top", "Bottom", "Middle", "End"
    let innings: [Inning]?
    let teams: LinescoreTeamsTotals?
    let scheduledInnings: Int?
    let isTopInning: Bool?
    let balls: Int?
    let strikes: Int?
    let outs: Int?
}

struct Inning: Codable, Hashable {
    let num: Int
    let home: InningTotals?
    let away: InningTotals?
}

struct InningTotals: Codable, Hashable {
    let runs: Int?
    let hits: Int?
    let errors: Int?
    let leftOnBase: Int?
}

struct LinescoreTeamsTotals: Codable, Hashable {
    let home: InningTotals?
    let away: InningTotals?
}

// MARK: - Decisions

struct Decisions: Codable, Hashable {
    let winner: PlayerInfo?
    let loser: PlayerInfo?
    let save: PlayerInfo?
}

struct PlayerInfo: Codable, Hashable {
    let id: Int
    let fullName: String
}

// MARK: - Box score

struct BoxScoreResponse: Codable {
    let teams: BoxScoreTeams
}

struct BoxScoreTeams: Codable {
    let away: BoxScoreTeam
    let home: BoxScoreTeam
}

struct BoxScoreTeam: Codable {
    let team: TeamInfo
    /// Keyed by "ID{playerId}" — the box-score endpoint serializes
    /// players as a dict keyed by id-prefixed string, not an array.
    let players: [String: BoxPlayer]
    /// Batting order — list of player ids in lineup order. Pinch
    /// hitters / DH appear as additional ids past the starting nine.
    let batters: [Int]
    /// Pitching order — first id is the starter; later ids are the
    /// reliever appearances in order.
    let pitchers: [Int]
}

struct BoxPlayer: Codable {
    let person: PlayerInfo
    let position: BoxPosition?
    /// This-game stats. Both sides may be nil for a player who
    /// didn't appear (e.g. position player listed as a pitcher).
    let stats: BoxStats?
    /// Season-to-date stats — used to surface AVG / ERA next to
    /// the box-score line.
    let seasonStats: BoxStats?
    /// "Williams, P" style — same string the MLB.com box score
    /// surfaces, nil if the API didn't ship it.
    let stats_battingOrder: String?

    enum CodingKeys: String, CodingKey {
        case person, position, stats, seasonStats
        case stats_battingOrder = "battingOrder"
    }
}

struct BoxPosition: Codable {
    let abbreviation: String?
}

struct BoxStats: Codable {
    let batting: BoxBatting?
    let pitching: BoxPitching?
}

struct BoxBatting: Codable, Hashable {
    let atBats: Int?
    let runs: Int?
    let hits: Int?
    let doubles: Int?
    let triples: Int?
    let homeRuns: Int?
    let rbi: Int?
    let baseOnBalls: Int?
    let strikeOuts: Int?
    /// Per-game fields the MLB Stats API box score does ship —
    /// needed by the player-profile overlay to recompute OBP
    /// accurately (HBP + SF are in the formula's denominator;
    /// SB drives the season-SB increment).
    let stolenBases: Int?
    let caughtStealing: Int?
    let hitByPitch: Int?
    let sacFlies: Int?
    let sacBunts: Int?
    let groundIntoDoublePlay: Int?
    /// "AVG" — comes through as a String like ".301" / ".000" /
    /// "---" (the latter for players with 0 PA), so we keep it raw.
    let avg: String?
    /// "OPS" — same MLB convention as AVG. Strings like ".812",
    /// ".000", "---". Game-stats OPS rarely makes sense (it's
    /// just-the-game-so-far); the seasonStats version is what the
    /// box-score table actually surfaces.
    let ops: String?
}

struct BoxPitching: Codable, Hashable {
    let inningsPitched: String?   // "5.2" — string per MLB convention
    let hits: Int?
    let runs: Int?
    let earnedRuns: Int?
    let baseOnBalls: Int?
    let strikeOuts: Int?
    let homeRuns: Int?
    let era: String?              // "2.41" — string from the API
    /// Career-side W/L/SV used by `FinalGameCard` to render the
    /// decision pitchers' updated record next to their name in the
    /// expanded view ("W: Cole (8-2)"). Only meaningful on the
    /// `seasonStats.pitching` payload — the game-stats version is
    /// per-appearance and noisy.
    let wins: Int?
    let losses: Int?
    let saves: Int?
    /// Pitch count for this appearance (only meaningful on the
    /// game-stats payload, not season). Surfaced as the "PC"
    /// column in the box-score pitching table; nil → "—".
    let pitchCount: Int?
}

/// A box score assembled from our own game logs, for a game older than the
/// provider's coverage. `BoxScoreResponse` plus what only this path knows.
struct HistoricalBoxScore: Codable {
    let gameId: String
    /// How `BoxScoreTeam.batters` is sorted, PER GAME — "lineup" once that
    /// game's Retrosheet slots are loaded, "alphabetical" until then. The
    /// distinction is the point: an alphabetical list that looked like a
    /// lineup would be wrong through the middle of the order, so the view
    /// shows a note for one value and nothing for the other.
    let batterOrdering: String?
    let teams: BoxScoreTeams

    var asBoxScore: BoxScoreResponse { BoxScoreResponse(teams: teams) }
}

// MARK: - Live feed (/api/v1.1/game/{pk}/feed/live)

/// Full-game live snapshot — `LiveGameCard` polls this every 30s to
/// drive the in-progress card and the live BoxScoreView. Decodes
/// only the `liveData` subtree; the response also contains
/// `gameData` (static metadata) which we skip.
struct LiveFeedResponse: Codable {
    let liveData: LiveData
}

struct LiveData: Codable {
    let linescore: LiveLinescore?
    let plays: LivePlays?
    /// Same shape as the standalone /boxscore endpoint nested one
    /// level deeper. Live mode uses this so the batting + pitching
    /// tables on BoxScoreView refresh in sync with the rest of the
    /// live state without a second round trip.
    let boxscore: LiveBoxscoreEnvelope?
}

struct LiveBoxscoreEnvelope: Codable {
    let teams: BoxScoreTeams
}

struct LiveLinescore: Codable {
    let currentInning: Int?
    let currentInningOrdinal: String?       // "9th", etc.
    let inningHalf: String?                  // "Top" / "Bottom"
    let inningState: String?                 // "Top" / "Bottom" / "Middle" / "End"
    let isTopInning: Bool?
    let balls: Int?
    let strikes: Int?
    let outs: Int?
    /// Offense block — current batter + base runners. The bases
    /// themselves are `PlayerInfo?` per base; non-nil means a
    /// runner is on that base.
    let offense: LiveOffense?
    /// Defense block — current pitcher.
    let defense: LiveDefense?
    let innings: [Inning]?
    let teams: LinescoreTeamsTotals?
    let scheduledInnings: Int?
}

struct LiveOffense: Codable {
    let batter: PlayerInfo?
    let onDeck: PlayerInfo?
    let inHole: PlayerInfo?
    let first: PlayerInfo?
    let second: PlayerInfo?
    let third: PlayerInfo?
}

struct LiveDefense: Codable {
    let pitcher: PlayerInfo?
    let catcher: PlayerInfo?
}

struct LivePlays: Codable {
    let currentPlay: LivePlay?
}

struct LivePlay: Codable {
    let result: LivePlayResult?
    let about: LivePlayAbout?
    let matchup: LivePlayMatchup?
    let count: LivePlayCount?
    /// Per-pitch / per-event log for this PA. We surface the last
    /// item's description as a one-line "last play" string when
    /// `result.description` is empty (mid-PA states like a single
    /// pitch before the plate appearance ends).
    let playEvents: [LivePlayEvent]?
}

struct LivePlayResult: Codable {
    let description: String?
    let event: String?
}

struct LivePlayAbout: Codable {
    let halfInning: String?
    let inning: Int?
}

struct LivePlayMatchup: Codable {
    let batter: PlayerInfo?
    let pitcher: PlayerInfo?
}

struct LivePlayCount: Codable {
    let balls: Int?
    let strikes: Int?
    let outs: Int?
}

struct LivePlayEvent: Codable {
    let details: LivePlayEventDetails?
    let isPitch: Bool?
}

struct LivePlayEventDetails: Codable {
    let description: String?
    let event: String?
}

// MARK: - BallDontLie conversion layer
//
// During the MLB-Stats-API → BallDontLie migration, the Scores tab
// still drives off the existing `Game` / `ScheduleResponse` models
// — too many call sites to swap in one pass. These conversions let
// a `BDLGame` flow into the existing model space without forcing
// downstream views to know about the new shape.
//
// Mappings:
//   • status string  → GameStatus.abstractGameState ("Live"/"Final"/"Preview")
//   • BDL team id    → TeamInfo.id (MLBAM id, looked up via bdlToLahmanTeamId)
//   • innings array  → Linescore.innings (per-inning runs only —
//                       BDL doesn't ship hits/errors at the inning
//                       granularity, just team totals)
//   • scoring_summary → not surfaced (no equivalent on legacy Game;
//                       BoxScoreView's expand pane will consume the
//                       BDL game directly in Phase 3 instead)

/// Maps BDL status string → the abstractGameState enum-like string
/// the existing `Game.phase` accessor branches on. Anything we
/// don't recognize falls through to "Preview" so the UI defaults
/// to scheduled-game chrome.
private func bdlStatusToAbstract(_ status: String) -> String {
    switch status {
    case "STATUS_FINAL":            return "Final"
    case "STATUS_IN_PROGRESS":      return "Live"
    case "STATUS_SCHEDULED":        return "Preview"
    // Postponed games won't be played today — they get their own
    // phase so the UI can flag them ("PPD") and sink them below the
    // genuinely-scheduled games rather than showing a stale start time.
    case "STATUS_POSTPONED":        return "Postponed"
    // Delays aren't truly "live" (the LIVE badge / current-inning
    // ordinal don't apply) but the game may still be played today, so
    // keep them as scheduled. The card reads `detailedState` to show a
    // "DELAYED" hint in place of the start time.
    case "STATUS_DELAYED":          return "Preview"
    case "STATUS_RAIN_DELAY":       return "Preview"
    default:                        return "Preview"
    }
}

extension BDLTeam {
    /// MLB Stats API numeric team id — what `TeamInfo.id` and the
    /// logo CDN want. Hops through Lahman so the bridge stays one
    /// place. Falls back to the BDL id if we can't resolve, which
    /// means logos won't load for that team but everything else
    /// still renders.
    var mlbStatsApiTeamId: Int {
        mlbTeamId(forBDLId: id) ?? id
    }

    /// Project to the legacy `TeamInfo` shape.
    func toTeamInfo() -> TeamInfo {
        TeamInfo(
            id:           mlbStatsApiTeamId,
            name:         displayName,
            abbreviation: abbreviation,
        )
    }
}

// MARK: - BDL box-score synthesis
//
// BDL's `/stats?game_ids[]={id}` ships a flat array of per-player
// lines. The existing `BoxScoreView` consumes a team-nested shape:
// `BoxScoreResponse.teams.{away,home}.players: [String: BoxPlayer]`
// keyed by `"ID{id}"`, plus `batters: [Int]` / `pitchers: [Int]`
// for ordering. The synthesizer below reshapes BDL's response into
// that legacy form, using BDL player ids in place of MLBAM ids
// throughout (the player-resolve path on tap routes through
// `BallDontLieClient.resolveBDLPlayerId` to hop back to MLBAM).
//
// Notes on what doesn't come through:
//   • `seasonStats` — BDL's per-game `/stats` endpoint doesn't
//     carry season-to-date AVG / OPS / ERA. Left nil; the view
//     degrades to "—" / "(0)" placeholders for those cells.
//   • Inningss-pitched format — BDL ships true decimal (5.667);
//     the view expects baseball notation ("5.2"). Converted at
//     synth time so visuals match the legacy path.

/// "5.667" → "5.2" (5 ⅔ innings in baseball notation). Round to
/// whole + outs/3. nil passes through.
func ipToBaseballNotation(_ ip: Double?) -> String? {
    guard let ip else { return nil }
    let whole = Int(ip)
    let frac  = ip - Double(whole)
    let outs  = Int((frac * 3).rounded())
    if outs == 3 { return "\(whole + 1).0" }
    return "\(whole).\(outs)"
}

/// `0.226` → `".226"` — MLB convention for AVG / OBP / SLG / OPS
/// is to drop the leading zero. nil passes through; values >= 1
/// (impossible for AVG/OBP, possible for OPS) keep the leading
/// digit ("1.034").
func formatMLBRate(_ v: Double?) -> String? {
    guard let v else { return nil }
    let s = String(format: "%.3f", v)
    return s.hasPrefix("0.") ? String(s.dropFirst()) : s
}

/// `2.41` → `"2.41"` — ERA keeps its leading digit. nil passes
/// through.
func formatMLBEra(_ v: Double?) -> String? {
    guard let v else { return nil }
    return String(format: "%.2f", v)
}

extension Array where Element == BDLPlayerStat {
    /// Project a BDL `/stats` response into the legacy
    /// `BoxScoreResponse` shape. `game.{away,home}Team` tells us
    /// which side each per-player row belongs to (we compare
    /// `BDLPlayerStat.teamName` against BDL's team names). `lineup`
    /// is optional — when supplied, batting + pitching order is
    /// driven off `BDLGameLineup.battingOrder`; without it, rows
    /// are ordered by their position in the BDL response (a usable
    /// fallback since BDL ships starters first).
    func toBoxScoreResponse(
        awayTeam: BDLTeam,
        homeTeam: BDLTeam,
        awayBDLTeamId: Int? = nil,
        homeBDLTeamId: Int? = nil,
        lineup: [BDLGameLineup] = [],
        seasonStatsByPid: [Int: BDLSeasonStat] = [:],
        plateAppearances: [BDLPlateAppearance] = [],
        isFinal: Bool = false,
    ) -> BoxScoreResponse {
        // Bucket the per-player lines by team. BDL's `team_name`
        // is the short franchise name ("Yankees" / "Mariners");
        // BDLTeam.name carries the same value, so a direct equality
        // check works.
        var awayStats: [BDLPlayerStat] = []
        var homeStats: [BDLPlayerStat] = []
        for s in self {
            switch s.teamName {
            case awayTeam.name: awayStats.append(s)
            case homeTeam.name: homeStats.append(s)
            default:
                // BDL is occasionally inconsistent (display name vs.
                // short name). Fall back to a substring check —
                // close enough for the box-score split.
                if let tn = s.teamName, awayTeam.displayName.contains(tn) {
                    awayStats.append(s)
                } else if let tn = s.teamName, homeTeam.displayName.contains(tn) {
                    homeStats.append(s)
                }
            }
        }

        // Prefer the explicit BDL team ids handed in by the caller
        // (sourced from `BDLGame.{away,home}_team.id` via `Game`'s
        // `bdlAwayTeamId` / `bdlHomeTeamId` fields). Those are the
        // authoritative join keys for the lineup payload. Fall back
        // to the BDLTeam param's `.id` only when the caller didn't
        // supply them — older paths might not.
        let awayJoinId = awayBDLTeamId ?? awayTeam.id
        let homeJoinId = homeBDLTeamId ?? homeTeam.id

        let lineupAway = lineup.filter { $0.team.id == awayJoinId }
        let lineupHome = lineup.filter { $0.team.id == homeJoinId }

        return BoxScoreResponse(teams: BoxScoreTeams(
            // The away side bats the top half, the home side the
            // bottom — that pairing is what splits the shared PA
            // sequence between the two box-score tables.
            away: buildBoxScoreTeam(
                team:             awayTeam,
                stats:            awayStats,
                lineup:           lineupAway,
                seasonStatsByPid: seasonStatsByPid,
                plateAppearances: plateAppearances,
                half:             "top",
                isFinal:          isFinal,
            ),
            home: buildBoxScoreTeam(
                team:             homeTeam,
                stats:            homeStats,
                lineup:           lineupHome,
                seasonStatsByPid: seasonStatsByPid,
                plateAppearances: plateAppearances,
                half:             "bottom",
                isFinal:          isFinal,
            ),
        ))
    }
}

/// Heuristic for whether a `BDLPlayerStat` row is a pitching line.
/// True when the pitcher fields are populated and the batter ones
/// are not (or are zero) — pure pitchers. Two-way players who
/// batted AND pitched (Ohtani) get two rows in BDL's response, one
/// flagged each way, so this gate doesn't accidentally hide them.
private func bdlStatIsPitcher(_ s: BDLPlayerStat) -> Bool {
    s.ip != nil
}

private func buildBoxScoreTeam(
    team: BDLTeam,
    stats: [BDLPlayerStat],
    lineup: [BDLGameLineup],
    seasonStatsByPid: [Int: BDLSeasonStat] = [:],
    plateAppearances: [BDLPlateAppearance] = [],
    half: String = "",
    isFinal: Bool = false,
) -> BoxScoreTeam {
    // The merge: seed BoxPlayers for every starting batter AND the
    // probable starting pitcher from the lineup (blank stats, season
    // rates pulled from `seasonStatsByPid` when available). Then walk
    // the stats rows and merge each row's batting / pitching blocks
    // onto the matching player — upgrading the placeholder to real
    // numbers, OR creating a new BoxPlayer for substitutes /
    // relievers who weren't in the lineup. The merge prefers a row's
    // non-nil block but keeps the placeholder's season rates when the
    // row doesn't ship them (Ohtani's pitching row carries no batting
    // AVG, etc.).
    var players: [String: BoxPlayer] = [:]

    // Index lineup entries by BDL player id so each merge step can
    // pull THIS-GAME's position (and the universal-DH override).
    let lineupByPid: [Int: BDLGameLineup] = Dictionary(
        lineup.map { ($0.player.id, $0) },
        uniquingKeysWith: { a, _ in a },
    )

    // --- Step 1: seed placeholders for every starting batter. ---
    for entry in lineup where (entry.battingOrder ?? 0) > 0 {
        let key = "ID\(entry.player.id)"
        players[key] = placeholderBatterBoxPlayer(
            entry:      entry,
            seasonStat: seasonStatsByPid[entry.player.id],
        )
    }

    // --- Step 1b: seed a placeholder for the probable starting
    // pitcher. BDL's lineup endpoint flags this entry via
    // `is_probable_pitcher == true` with `batting_order == null`. ---
    for entry in lineup where lineupEntryIsPitcher(entry) {
        let key = "ID\(entry.player.id)"
        players[key] = placeholderPitcherBoxPlayer(
            entry:      entry,
            seasonStat: seasonStatsByPid[entry.player.id],
        )
    }

    // --- Step 2: merge stats rows. Each row contributes either a
    // batting block (anyone with PA / AB / walks counts as a batter
    // row) or a pitching block (ip != nil). Two-way players (Ohtani)
    // get two stat rows, one of each shape; the merger preserves
    // both. ---
    for s in stats {
        let key = "ID\(s.player.id)"
        let increment = boxPlayerFromStatRow(
            s,
            lineupRow:  lineupByPid[s.player.id],
            seasonStat: seasonStatsByPid[s.player.id],
        )
        players[key] = mergeBoxPlayer(players[key], increment)
    }

    // --- Step 3: ordering arrays. ---
    // Batters: lineup order first; then any stats-only batters
    // (substitutes / pinch hitters), appended in their stats-payload
    // order. When the lineup ships empty (BDL outage), the lineup
    // slice is empty and we degrade to pure stats order — same
    // behavior the previous implementation had as a fallback.
    let lineupBatterIds: [Int] = lineup
        .filter { ($0.battingOrder ?? 0) > 0 }
        .sorted { ($0.battingOrder ?? 0) < ($1.battingOrder ?? 0) }
        .map(\.player.id)
    let lineupBatterSet = Set(lineupBatterIds)

    var seenExtras = Set<Int>()
    let extraBatterIds: [Int] = stats.compactMap { s in
        let pid = s.player.id
        guard !lineupBatterSet.contains(pid) else { return nil }
        guard statHadBattingActivity(s) else { return nil }
        guard seenExtras.insert(pid).inserted else { return nil }
        return pid
    }
    let appendOrder = lineupBatterIds + extraBatterIds

    // Substitute placement. When the PA sequence yields a trustworthy
    // set of slot codes, stamp them onto the players (which is what
    // drives the traditional indent in BoxScoreView) and sort the
    // batting order by them, so a pinch hitter sits under the man he
    // replaced. Otherwise leave every code nil and keep the old
    // append-at-the-bottom order.
    let orderCodes = substituteBattingOrders(
        plateAppearances: plateAppearances,
        half:             half,
        lineup:           lineup,
        stats:            stats,
        isFinal:          isFinal,
    )
    let battingOrder: [Int]
    if let codes = orderCodes {
        for (pid, code) in codes {
            let key = "ID\(pid)"
            guard let p = players[key] else { continue }
            players[key] = BoxPlayer(
                person:             p.person,
                position:           p.position,
                stats:              p.stats,
                seasonStats:        p.seasonStats,
                stats_battingOrder: String(code),
            )
        }
        // Anything the sequence didn't cover keeps its old relative
        // position at the end rather than being dropped.
        battingOrder = appendOrder
            .enumerated()
            .sorted { a, b in
                let ca = codes[a.element] ?? Int.max
                let cb = codes[b.element] ?? Int.max
                return ca == cb ? a.offset < b.offset : ca < cb
            }
            .map(\.element)
    } else {
        battingOrder = appendOrder
    }

    // Pitchers: probable starter(s) from the lineup first, then any
    // stats-bearing pitchers not already covered (relievers; or the
    // starter once they've recorded an out). Dedupe across both lists.
    let lineupPitcherIds: [Int] = lineup
        .filter { lineupEntryIsPitcher($0) }
        .map(\.player.id)
    let lineupPitcherSet = Set(lineupPitcherIds)

    var seenPitchers = lineupPitcherSet
    let extraPitcherIds: [Int] = stats.compactMap { s in
        guard s.ip != nil else { return nil }
        let pid = s.player.id
        guard seenPitchers.insert(pid).inserted else { return nil }
        return pid
    }
    let pitchingOrder = lineupPitcherIds + extraPitcherIds

    return BoxScoreTeam(
        team:     team.toTeamInfo(),
        players:  players,
        batters:  battingOrder,
        pitchers: pitchingOrder,
    )
}

/// MLB-style `battingOrder` codes for one side, derived from the
/// plate-appearance sequence: slot * 100 + depth, so "600" is the
/// number-six starter and "601" the first man to bat in his slot.
/// That is the same encoding the historical box score ships, which
/// is why `BoxScoreView.substitutionDepth` can read both.
///
/// Why this is needed at all: `/lineups` carries only the nine
/// starters, `/stats` carries no batting order, and `/plays` carries
/// no substitution records — measured, not assumed. The PA sequence
/// is the only current-season signal that says which slot a pinch
/// hitter batted in.
///
/// Returns nil when the sequence can't be trusted. Nil is not a
/// failure: the caller falls back to appending substitutes at the
/// bottom, which is what the box score did before this existed. A
/// wrong slot is worse than an append, because an append is visibly
/// an append and a wrong slot reads as fact.
///
/// About one side in five falls back. That rate is even between home
/// and away (12 of 58 each) and shows no team pattern, but it
/// clusters hard on extra innings: 58% of extra-inning sides against
/// 16% of nine-inning ones.
private func substituteBattingOrders(
    plateAppearances: [BDLPlateAppearance],
    half: String,
    lineup: [BDLGameLineup],
    stats: [BDLPlayerStat],
    isFinal: Bool,
) -> [Int: Int]? {
    // The nine declared slots are the anchors every derived slot is
    // measured against. Anything other than a full nine (BDL outage,
    // a lineup that hasn't posted) and we don't start.
    var declaredSlot: [Int: Int] = [:]
    for e in lineup {
        guard let slot = e.battingOrder, slot > 0 else { continue }
        declaredSlot[e.player.id] = slot
    }
    guard Set(declaredSlot.values) == Set(1...9) else { return nil }

    // Finals only, and not for the reason it looks like. A live game's
    // box score is not built here at all: `BoxScoreView` subscribes to
    // `LiveGameStore` and overwrites `boxScore` with
    // `LiveGameDetail.toBoxScoreResponse()` on every snapshot, which
    // has no batting order to carry (`LiveBatterRow` has no such
    // field) and keys players in a different id space. Deriving slots
    // for a live game would therefore show the indent on first load
    // and lose it one tick later. Substitute placement is a
    // finals-only feature until that path can carry an order.
    guard isFinal else { return nil }

    let seq = plateAppearances
        .filter { $0.halfInning?.lowercased() == half }
        .sorted { ($0.inning, $0.paNumber) < ($1.inning, $1.paNumber) }
    guard !seq.isEmpty else { return nil }

    // Gate 1: the PA rows must reconcile exactly with the summed
    // plate appearances on this side's stat lines.
    //
    // Equality, not a surplus check. Measured over 116 sides
    // (2026-09-06) the two disagree on about a fifth of them, and in
    // BOTH directions: the feed DROPS rows slightly more often than
    // it duplicates them — 12 sides short, 9 long, plus 3 that
    // reconcile here and still fail gate 2. A deficit shifts the
    // slots after it exactly as a surplus does, so neither direction
    // is the safe one.
    let statsPA = stats.reduce(0) { $0 + ($1.plateAppearances ?? 0) }
    guard seq.count == statsPA else { return nil }

    // Gate 2: rotation consistency, and the load-bearing one. Walk
    // the sequence against a 1...9 rotation; every batter whose slot
    // we already know must land where the rotation says. A starter
    // out of position, or a substitute coming round in a different
    // slot than his first PA, means the sequence has drifted and
    // every slot after that point would be silently wrong.
    //
    // The largest single cause is an inning-ending caught stealing,
    // which writes a PA row for a batter who then leads off the next
    // inning — 8 of 24 observed failures. It is NOT the majority:
    // ordinary results (groundout, strikeout, single, sac bunt) make
    // up the rest, so this has to stay a general consistency check
    // and can't be narrowed to that one shape.
    var codes: [Int: Int] = declaredSlot.mapValues { $0 * 100 }
    var depthInSlot: [Int: Int] = [:]
    var expected = 1
    for pa in seq {
        guard let bid = pa.batterId else { return nil }
        if let declared = declaredSlot[bid] {
            guard declared == expected else { return nil }
        } else if let already = codes[bid] {
            guard already / 100 == expected else { return nil }
        } else {
            let depth = (depthInSlot[expected] ?? 0) + 1
            // Depth is the low two digits of the code, so a slot that
            // somehow burned 99 men would collide with the next slot.
            guard depth < 100 else { return nil }
            depthInSlot[expected] = depth
            codes[bid] = expected * 100 + depth
        }
        expected = expected % 9 + 1
    }
    return codes
}

/// True iff this lineup row represents the probable starting
/// pitcher — `batting_order == null` plus either an explicit
/// `is_probable_pitcher: true` flag or a pitcher-type position.
private func lineupEntryIsPitcher(_ e: BDLGameLineup) -> Bool {
    if (e.battingOrder ?? 0) > 0 { return false }
    if e.isProbablePitcher == true { return true }
    if let p = e.position?.uppercased() {
        return p == "P" || p == "SP" || p == "RP"
    }
    return false
}

/// Stats-row → BoxPlayer projection. Same shape the synthesizer
/// previously built inline; pulled out so step 2 of the merge can
/// produce a single per-row increment that's combined with the
/// running aggregate via `mergeBoxPlayer`.
private func boxPlayerFromStatRow(
    _ s: BDLPlayerStat,
    lineupRow:  BDLGameLineup?,
    seasonStat: BDLSeasonStat? = nil,
) -> BoxPlayer {
    let pid = s.player.id
    let isPitcher = bdlStatIsPitcher(s)
    let batting: BoxBatting? = isPitcher && (s.atBats ?? 0) == 0 ? nil : BoxBatting(
        atBats:               s.atBats,
        runs:                 s.runs,
        hits:                 s.hits,
        doubles:              s.doubles,
        triples:              s.triples,
        homeRuns:             s.hr,
        rbi:                  s.rbi,
        baseOnBalls:          s.bb,
        strikeOuts:           s.k,
        stolenBases:          s.stolenBases,
        caughtStealing:       s.caughtStealing,
        hitByPitch:           s.hitByPitch,
        sacFlies:             s.sacFlies,
        sacBunts:             nil,
        groundIntoDoublePlay: nil,
        avg:                  nil,  // → seasonStats.batting.avg
        ops:                  nil,
    )
    let pitching: BoxPitching? = !isPitcher ? nil : BoxPitching(
        // BDL already ships `ip` in baseball notation ("5.2" = 5⅔),
        // which is the same shape `BoxPitching.inningsPitched`
        // wants — pass through directly.
        inningsPitched: s.ip,
        hits:           s.pHits,
        runs:           s.pRuns,
        earnedRuns:     s.er,
        baseOnBalls:    s.pBb,
        strikeOuts:     s.pK,
        homeRuns:       s.pHr,
        era:            nil,  // → seasonStats.pitching.era
        wins:           s.wins,
        losses:         s.losses,
        saves:          s.saves,
        pitchCount:     s.pitchCount,
    )
    // BDL doesn't ship OPS — derive from OBP + SLG.
    let opsValue: Double? = (s.obp != nil && s.slg != nil) ? (s.obp! + s.slg!) : nil
    // Only build the season block when the row actually carries
    // season rates. A pitcher-side stats row for a two-way player
    // (Ohtani) ships avg/obp as nil even though he has a real
    // season AVG — nil-ing the whole block here lets the merge fall
    // back to the placeholder's pre-populated value.
    let seasonBatting: BoxBatting? = (s.avg != nil || s.obp != nil || s.slg != nil) ? BoxBatting(
        atBats:               nil,
        runs:                 nil,
        hits:                 nil,
        doubles:              nil,
        triples:              nil,
        homeRuns:             nil,
        rbi:                  nil,
        baseOnBalls:          nil,
        strikeOuts:           nil,
        stolenBases:          nil,
        caughtStealing:       nil,
        hitByPitch:           nil,
        sacFlies:             nil,
        sacBunts:             nil,
        groundIntoDoublePlay: nil,
        avg:                  formatMLBRate(s.avg),
        ops:                  formatMLBRate(opsValue),
    ) : nil
    // Season W/L/SV come from the season-stats endpoint (BDL's
    // per-game `/stats` rows don't ship them), so they're only
    // available when the caller hands us a `seasonStat` row.
    // Without one, the field-aware merge in `mergeSeasonPitching`
    // preserves whatever the pitcher placeholder already set.
    let seasonPitching: BoxPitching? = (s.era != nil || seasonStat != nil) ? BoxPitching(
        inningsPitched: nil,
        hits:           nil,
        runs:           nil,
        earnedRuns:     nil,
        baseOnBalls:    nil,
        strikeOuts:     nil,
        homeRuns:       nil,
        era:            formatMLBEra(s.era),
        wins:           seasonStat?.pitchingW,
        losses:         seasonStat?.pitchingL,
        saves:          seasonStat?.pitchingSv,
        pitchCount:     nil,
    ) : nil
    // Position: lineup row wins (carries the game-specific DH
    // override for two-way starters); fall back to the stat row's
    // career-default `player.position`.
    let lineupPos = lineupRow?.position
    let inBattingOrder = (lineupRow?.battingOrder ?? 0) > 0
    let resolvedPosition: String? = {
        let raw = lineupPos ?? s.player.position
        if inBattingOrder, let r = raw?.uppercased(),
           r == "P" || r == "SP" || r == "RP" {
            return "DH"
        }
        return raw
    }()
    return BoxPlayer(
        person:             PlayerInfo(id: pid, fullName: s.player.fullName),
        position:           BoxPosition(abbreviation: resolvedPosition),
        stats:              BoxStats(batting: batting,       pitching: pitching),
        seasonStats:        BoxStats(batting: seasonBatting, pitching: seasonPitching),
        stats_battingOrder: nil,
    )
}

/// "Hasn't-batted-yet" starter row — blank counts, season AVG/OPS
/// pulled from BDL's `/season_stats` endpoint when available.
/// Falls back to "—" for both rates when the season-stats fetch
/// returned no row for the player (early-season cold-start, or
/// the fetch errored / cached miss). Replaced (via merge) the
/// moment the player records a PA.
private func placeholderBatterBoxPlayer(
    entry: BDLGameLineup,
    seasonStat: BDLSeasonStat?,
) -> BoxPlayer {
    let resolvedPosition: String? = {
        let raw = entry.position
        // Universal-DH override: a "P" / "SP" / "RP" with a real
        // batting_order is the team's DH for this game.
        if (entry.battingOrder ?? 0) > 0, let r = raw?.uppercased(),
           r == "P" || r == "SP" || r == "RP" {
            return "DH"
        }
        return raw
    }()
    let blankBatting = BoxBatting(
        atBats:               0,
        runs:                 0,
        hits:                 0,
        doubles:              0,
        triples:              0,
        homeRuns:             0,
        rbi:                  0,
        baseOnBalls:          0,
        strikeOuts:           0,
        stolenBases:          0,
        caughtStealing:       0,
        hitByPitch:           0,
        sacFlies:             0,
        sacBunts:             0,
        groundIntoDoublePlay: 0,
        avg:                  nil,  // → seasonStats.batting.avg
        ops:                  nil,
    )
    let avgString: String = formatMLBRate(seasonStat?.battingAvg) ?? "—"
    let opsString: String = formatMLBRate(seasonStat?.battingOps) ?? "—"
    let blankSeasonBatting = BoxBatting(
        atBats:               nil,
        runs:                 nil,
        hits:                 nil,
        // Season HR / 2B / 3B pulled from BDL's `/season_stats` row
        // so the box-score notable-plays section and the score-card
        // HR summary read "(N)" with the actual season total instead
        // of "(0)". The field-aware merge below preserves these
        // values when the per-game stats row lands (BDL doesn't ship
        // season counting stats on the per-game payload).
        doubles:              seasonStat?.batting2B,
        triples:              seasonStat?.batting3B,
        homeRuns:             seasonStat?.battingHr,
        rbi:                  nil,
        baseOnBalls:          nil,
        strikeOuts:           nil,
        stolenBases:          nil,
        caughtStealing:       nil,
        hitByPitch:           nil,
        sacFlies:             nil,
        sacBunts:             nil,
        groundIntoDoublePlay: nil,
        avg:                  avgString,
        ops:                  opsString,
    )
    return BoxPlayer(
        person:             PlayerInfo(id: entry.player.id, fullName: entry.player.fullName),
        position:           BoxPosition(abbreviation: resolvedPosition),
        stats:              BoxStats(batting: blankBatting,       pitching: nil),
        seasonStats:        BoxStats(batting: blankSeasonBatting, pitching: nil),
        stats_battingOrder: nil,
    )
}

/// "Hasn't-pitched-yet" probable-starter row. Counts at 0,
/// inningsPitched at "0.0", season ERA pulled from BDL when the
/// fetch had a row for the player. Replaced via merge the moment
/// the pitcher records their first out.
private func placeholderPitcherBoxPlayer(
    entry: BDLGameLineup,
    seasonStat: BDLSeasonStat?,
) -> BoxPlayer {
    let position = entry.position ?? "SP"
    let blankPitching = BoxPitching(
        inningsPitched: "0.0",
        hits:           0,
        runs:           0,
        earnedRuns:     0,
        baseOnBalls:    0,
        strikeOuts:     0,
        homeRuns:       0,
        era:            nil,  // → seasonStats.pitching.era
        wins:           0,
        losses:         0,
        saves:          0,
        pitchCount:     0,
    )
    let eraString: String = formatMLBEra(seasonStat?.pitchingEra) ?? "—"
    let blankSeasonPitching = BoxPitching(
        inningsPitched: nil,
        hits:           nil,
        runs:           nil,
        earnedRuns:     nil,
        baseOnBalls:    nil,
        strikeOuts:     nil,
        homeRuns:       nil,
        era:            eraString,
        wins:           seasonStat?.pitchingW,
        losses:         seasonStat?.pitchingL,
        saves:          seasonStat?.pitchingSv,
        pitchCount:     nil,
    )
    return BoxPlayer(
        person:             PlayerInfo(id: entry.player.id, fullName: entry.player.fullName),
        position:           BoxPosition(abbreviation: position),
        stats:              BoxStats(batting: nil, pitching: blankPitching),
        seasonStats:        BoxStats(batting: nil, pitching: blankSeasonPitching),
        stats_battingOrder: nil,
    )
}

/// Merge an incoming BoxPlayer (from one stats row) onto an
/// existing aggregate. The merge keeps any non-nil block on the
/// new row, falling back to the existing aggregate's. Used for two
/// patterns:
///   • upgrading a placeholder (blank batting) to real game stats
///   • combining Ohtani's batter row + pitcher row into one entry
///     that has both `stats.batting` and `stats.pitching` populated
private func mergeBoxPlayer(_ existing: BoxPlayer?, _ incoming: BoxPlayer) -> BoxPlayer {
    guard let existing else { return incoming }
    return BoxPlayer(
        person:   incoming.person,
        position: incoming.position?.abbreviation != nil ? incoming.position : existing.position,
        stats: BoxStats(
            batting:  incoming.stats?.batting  ?? existing.stats?.batting,
            pitching: incoming.stats?.pitching ?? existing.stats?.pitching,
        ),
        seasonStats: BoxStats(
            batting:  mergeSeasonBatting(
                existing: existing.seasonStats?.batting,
                incoming: incoming.seasonStats?.batting,
            ),
            pitching: mergeSeasonPitching(
                existing: existing.seasonStats?.pitching,
                incoming: incoming.seasonStats?.pitching,
            ),
        ),
        stats_battingOrder: incoming.stats_battingOrder ?? existing.stats_battingOrder,
    )
}

/// Field-aware season-batting merge. When both sides exist, prefer
/// the incoming row's non-nil values per field; otherwise fall back
/// to the existing aggregate's. Keeps placeholder-loaded values
/// (e.g. season AVG/OPS pulled from `/season_stats` before the
/// player batted) from being clobbered by an incoming per-game row
/// that lacks those rates.
private func mergeSeasonBatting(
    existing: BoxBatting?, incoming: BoxBatting?,
) -> BoxBatting? {
    guard let e = existing else { return incoming }
    guard let i = incoming else { return e }
    // doubles / triples / homeRuns invert the precedence: BDL's
    // per-game `/stats` row carries those counts as "this game's
    // 2B / 3B / HR" — they're zero or the per-game delta, never
    // season totals. The placeholder pre-fetched the season totals
    // from `/season_stats`, so when both sides exist we want the
    // existing value to survive. Other fields keep the
    // "incoming wins on non-nil" semantics because their per-game
    // values ARE the season-to-date stamp BDL ships on each row.
    return BoxBatting(
        atBats:               i.atBats               ?? e.atBats,
        runs:                 i.runs                 ?? e.runs,
        hits:                 i.hits                 ?? e.hits,
        doubles:              e.doubles              ?? i.doubles,
        triples:              e.triples              ?? i.triples,
        homeRuns:             e.homeRuns             ?? i.homeRuns,
        rbi:                  i.rbi                  ?? e.rbi,
        baseOnBalls:          i.baseOnBalls          ?? e.baseOnBalls,
        strikeOuts:           i.strikeOuts           ?? e.strikeOuts,
        stolenBases:          i.stolenBases          ?? e.stolenBases,
        caughtStealing:       i.caughtStealing       ?? e.caughtStealing,
        hitByPitch:           i.hitByPitch           ?? e.hitByPitch,
        sacFlies:             i.sacFlies             ?? e.sacFlies,
        sacBunts:             i.sacBunts             ?? e.sacBunts,
        groundIntoDoublePlay: i.groundIntoDoublePlay ?? e.groundIntoDoublePlay,
        avg:                  i.avg                  ?? e.avg,
        ops:                  i.ops                  ?? e.ops,
    )
}

/// Field-aware season-pitching merge. Same intent as
/// `mergeSeasonBatting` — the placeholder's season ERA / W / L / SV
/// (pulled from `/season_stats` for the lineup's probable starter)
/// must survive a subsequent per-game stats row that only carries
/// the in-game counts and clobbers everything else.
private func mergeSeasonPitching(
    existing: BoxPitching?, incoming: BoxPitching?,
) -> BoxPitching? {
    guard let e = existing else { return incoming }
    guard let i = incoming else { return e }
    return BoxPitching(
        inningsPitched: i.inningsPitched ?? e.inningsPitched,
        hits:           i.hits           ?? e.hits,
        runs:           i.runs           ?? e.runs,
        earnedRuns:     i.earnedRuns     ?? e.earnedRuns,
        baseOnBalls:    i.baseOnBalls    ?? e.baseOnBalls,
        strikeOuts:     i.strikeOuts     ?? e.strikeOuts,
        homeRuns:       i.homeRuns       ?? e.homeRuns,
        era:            i.era            ?? e.era,
        wins:           i.wins           ?? e.wins,
        losses:         i.losses         ?? e.losses,
        saves:          i.saves          ?? e.saves,
        pitchCount:     i.pitchCount     ?? e.pitchCount,
    )
}

/// Did this stats row record any batting activity? Used to decide
/// whether a non-lineup player (substitute / pinch hitter) gets
/// appended to `battingOrder`.
private func statHadBattingActivity(_ s: BDLPlayerStat) -> Bool {
    (s.atBats ?? 0) > 0 || (s.bb ?? 0) > 0 || (s.plateAppearances ?? 0) > 0
}

extension BDLGame {
    /// Project to the legacy `Game` shape so existing views can
    /// consume BDL results without code changes. `gamePk` is set
    /// to the BDL id (it's an int in both worlds, just a different
    /// number space — the player-resolve / box-score paths know
    /// the difference and route accordingly).
    func toGame() -> Game {
        let abstract = bdlStatusToAbstract(status)
        let status = GameStatus(
            abstractGameState: abstract,
            detailedState:     detailedStateFromBDL(),
            statusCode:        nil,
            codedGameState:    nil,
        )
        let awayInfo = GameTeam(
            team:           awayTeam.toTeamInfo(),
            score:          awayTeamData?.runs,
            leagueRecord:   nil,
            isWinner:       nil,
            probablePitcher: nil,
        )
        let homeInfo = GameTeam(
            team:           homeTeam.toTeamInfo(),
            score:          homeTeamData?.runs,
            leagueRecord:   nil,
            isWinner:       nil,
            probablePitcher: nil,
        )
        return Game(
            gamePk:        id,
            gameDate:      date,
            status:        status,
            teams:         GameTeams(away: awayInfo, home: homeInfo),
            venue:         venue.map { Venue(id: nil, name: $0) },
            linescore:     toLinescore(),
            decisions:     nil,
            bdlAwayTeamId: awayTeam.id,
            bdlHomeTeamId: homeTeam.id,
            // A BDL game IS its own provider id, so plays address it directly.
            bdlGameId:     id,
        )
    }

    /// Approximate `detailedState` from BDL's status enum. We can't
    /// surface every Stats-API nuance (scheduled vs. warmup vs.
    /// pre-game) — collapse to the buckets that drive UI branches.
    private func detailedStateFromBDL() -> String {
        switch status {
        case "STATUS_FINAL":       return "Final"
        case "STATUS_IN_PROGRESS": return "In Progress"
        case "STATUS_SCHEDULED":   return "Scheduled"
        case "STATUS_POSTPONED":   return "Postponed"
        case "STATUS_DELAYED":     return "Delayed"
        default:                   return status
        }
    }

    /// Build a legacy `Linescore` from BDL's `inning_scores` arrays.
    /// BDL only ships per-team per-inning runs (no hits / errors /
    /// LOB at that granularity), so we leave those fields nil and
    /// surface the totals row from `home_team_data` / `away_team_data`.
    private func toLinescore() -> Linescore? {
        let awayInns = awayTeamData?.inningScores ?? []
        let homeInns = homeTeamData?.inningScores ?? []
        let inningCount = max(awayInns.count, homeInns.count)

        var innings: [Inning] = []
        for i in 0..<inningCount {
            let awayR = i < awayInns.count ? awayInns[i] : nil
            let homeR = i < homeInns.count ? homeInns[i] : nil
            innings.append(Inning(
                num:  i + 1,
                home: InningTotals(runs: homeR, hits: nil, errors: nil, leftOnBase: nil),
                away: InningTotals(runs: awayR, hits: nil, errors: nil, leftOnBase: nil),
            ))
        }

        let teamsTotals = LinescoreTeamsTotals(
            home: InningTotals(
                runs:       homeTeamData?.runs,
                hits:       homeTeamData?.hits,
                errors:     homeTeamData?.errors,
                leftOnBase: nil,
            ),
            away: InningTotals(
                runs:       awayTeamData?.runs,
                hits:       awayTeamData?.hits,
                errors:     awayTeamData?.errors,
                leftOnBase: nil,
            ),
        )

        // `currentInning` — for live games, BDL's `period` is the
        // current inning number. For finals, BDL doesn't ship a
        // value here; leave nil and let the existing UI fall back
        // to inning-array length.
        let currentInning = (status == "STATUS_IN_PROGRESS") ? period : nil

        return Linescore(
            currentInning:        currentInning,
            currentInningOrdinal: nil,
            inningState:          nil,
            innings:              innings.isEmpty ? nil : innings,
            teams:                teamsTotals,
            scheduledInnings:     9,
            isTopInning:          nil,
            balls:                nil,
            strikes:              nil,
            outs:                 nil,
        )
    }
}
