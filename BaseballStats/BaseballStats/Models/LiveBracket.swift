//
//  LiveBracket.swift
//  BaseballStats
//
//  Codable models for `GET /postseason/bracket?season=YYYY` — the CURRENT
//  format's bracket (2022 on) at any stage: before the Wild Card round, mid-
//  round, or finished. Not to be confused with `PostseasonBracket.swift`,
//  which assembles Lahman's FINISHED series for Playoff History and cannot
//  hold an empty slot.
//
//  The backend (`postseason_series.build_bracket`) places every slot from the
//  bracket structure and the seeds — seed 1 meets the 4/5 Wild Card winner,
//  seed 2 the 3/6 winner — so the client never re-derives who plays whom. A
//  side that isn't decided yet has no team and lists its candidates.
//
//  Also here: `standingsDefault`, the rule for when the Standings tab opens on
//  the bracket.
//

import Foundation

struct LiveBracket: Codable, Hashable {
    let season: Int
    let leagues: [String: LiveBracketLeague]
    let worldSeries: BracketSlot?

    enum CodingKeys: String, CodingKey {
        case season, leagues
        case worldSeries = "world_series"
    }

    var al: LiveBracketLeague? { leagues["AL"] }
    var nl: LiveBracketLeague? { leagues["NL"] }

    /// Both leagues' slots are known — the bracket can be drawn. When either
    /// league's seeds can't be trusted the backend sends no slots for it, and
    /// the view falls back to a plain series list.
    var isDrawable: Bool {
        (al?.trusted ?? false) && (nl?.trusted ?? false) && worldSeries != nil
    }

    /// The first pitch of this postseason: the earliest scheduled game in any
    /// series. nil when balldontlie lists none yet.
    var firstPitch: Date? {
        let series: [PostseasonSeriesState] = leagues.values.flatMap { league in
            league.slots.compactMap { $0.series } + league.series
        }
        return series.flatMap { $0.games }.compactMap { $0.startDate }.min()
    }
}

struct LiveBracketLeague: Codable, Hashable {
    /// false when a seed is missing or the first-round pairs aren't {3,6} and
    /// {4,5}: then `slots` and `seeds` are empty and `series` holds the
    /// league's series as a flat list.
    let trusted: Bool
    let seeds: [BracketSeed]
    /// WC-3v6, WC-4v5, DS-1, DS-2, CS — in that order.
    let slots: [BracketSlot]
    let series: [PostseasonSeriesState]

    func slot(_ suffix: String) -> BracketSlot? {
        slots.first { $0.id.hasSuffix(suffix) }
    }
}

struct BracketSeed: Codable, Hashable {
    let seed: Int
    let team: String
}

struct BracketSlot: Codable, Hashable, Identifiable {
    let id: String              // "AL-DS-1", "WS"
    let round: String           // WC / DS / CS / WS
    let roundName: String?      // "AL Wild Card", "ALDS", "World Series"
    let league: String?         // nil for the World Series
    let bestOf: Int?
    let sides: [BracketSide]
    let state: State
    let winner: String?
    let series: PostseasonSeriesState?

    enum State: String, Codable, Hashable {
        case tbd, scheduled
        case inProgress = "in_progress"
        case complete
    }

    enum CodingKeys: String, CodingKey {
        case id, round, league, sides, state, winner, series
        case roundName = "round_name"
        case bestOf = "best_of"
    }

    /// Series wins for a side, once the series is under way — a "0 0" before
    /// the first pitch says nothing.
    func wins(for side: BracketSide) -> Int? {
        guard let team = side.team, let series, state == .inProgress || state == .complete else { return nil }
        return series.wins[team] ?? 0
    }

    /// The one line under the box. A decided or running series says where it
    /// stands ("ATL leads 1-0", "LAD wins 4-3"); a live game says so; before a
    /// series starts, its length.
    var statusLine: String {
        if let series, state != .scheduled {
            if series.games.contains(where: \.isLive) { return "Live" }
            if let last = series.games.last(where: { $0.status == "STATUS_FINAL" }),
               let status = last.seriesStatus {
                return status
            }
        }
        if let bestOf { return "Best of \(bestOf)" }
        return ""
    }
}

struct BracketSide: Codable, Hashable {
    let team: String?
    let seed: Int?
    /// Every team that could still fill this side; just `[team]` once known.
    let candidates: [String]

    /// "NYY/BOS" for an undecided side with two possibilities; "TBD" when
    /// there are more (an LCS side before the Wild Card round).
    var tbdLabel: String {
        candidates.count == 2 ? candidates.joined(separator: "/") : "TBD"
    }
}

extension PostseasonSeriesGame {
    var isLive: Bool {
        status == "STATUS_IN_PROGRESS" || status == "STATUS_DELAYED"
    }

    var startDate: Date? {
        guard let date else { return nil }
        return LiveBracketDates.parse(date)
    }
}

enum LiveBracketDates {
    private static let withFraction: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
    private static let plain = ISO8601DateFormatter()

    static func parse(_ iso: String) -> Date? {
        withFraction.date(from: iso) ?? plain.date(from: iso)
    }
}

// MARK: - When the Standings tab opens on the bracket

/// What the Standings tab shows on open.
enum StandingsDefault: Equatable {
    case standings
    case bracket(season: Int)
}

/// ⚠️ THE BRACKET DEFAULT: from the first pitch of a postseason until OPENING
/// DAY of the next season. It is NOT the rule for the Overview "Postseason"
/// line (that one ends with the World Series) — the two are different rules
/// and must stay two functions.
///
/// Both ends are read from data, not a calendar:
///   • The postseason has started when `now` has reached the first scheduled
///     game of `currentYear`'s bracket (`LiveBracket.firstPitch`).
///   • Opening Day has come when `currentYear`'s standings show a game played.
///     So from January until the first result of the new season lands, the
///     tab opens on LAST season's bracket.
///
/// The bracket only exists from 2022 (the current format); before that, and
/// when neither condition holds, the tab opens on the standings.
///
/// - Parameters:
///   - currentYearFirstPitch: `firstPitch` of `currentYear`'s bracket, or nil
///     when there is none yet (or it failed to load).
///   - currentYearGamesPlayed: whether any team in `currentYear`'s standings
///     has a decision (W + L > 0); `false` also when the backend has no
///     standings for the year at all (its 404 before Opening Day).
///     ⚠️ nil — UNKNOWN, the fetch failed — is NOT "no games played": a
///     dropped request in July must not reopen last October's bracket.
func standingsDefault(
    now: Date,
    currentYear: Int,
    currentYearFirstPitch: Date?,
    currentYearGamesPlayed: Bool?,
) -> StandingsDefault {
    if let first = currentYearFirstPitch, now >= first, currentYear >= LiveBracketRules.firstSeason {
        return .bracket(season: currentYear)
    }
    if currentYearGamesPlayed == false, currentYear - 1 >= LiveBracketRules.firstSeason {
        return .bracket(season: currentYear - 1)
    }
    return .standings
}

enum LiveBracketRules {
    /// The 12-team, 3/6 + 4/5 format `/postseason/bracket` serves.
    static let firstSeason = 2022
}
