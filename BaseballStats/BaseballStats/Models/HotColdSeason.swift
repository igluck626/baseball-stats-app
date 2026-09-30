//
//  HotColdSeason.swift
//  BaseballStats
//
//  `showsHotCold`, the rule for when the profile's Hot/Cold meter shows, the
//  `/season/phase` model it reads, and the loader.
//
//  A third season-phase rule, kept its own function like the other two: the
//  Standings bracket default (`standingsDefault`, first pitch → next Opening
//  Day) and the Overview postseason line (`showsPostseasonOverviewLine`,
//  first game → end of the World Series). This one is the regular season.
//

import Foundation

/// `GET /season/phase?season=`: the regular season's span, Eastern
/// `yyyy-MM-dd` dates, derived by the backend from balldontlie's schedule.
/// nil until the season's schedule is published.
struct SeasonPhase: Codable, Hashable {
    let season: Int
    let openingDay: String?
    let lastRegularDay: String?

    enum CodingKeys: String, CodingKey {
        case season
        case openingDay = "opening_day"
        case lastRegularDay = "last_regular_day"
    }
}

/// ⚠️ THE HOT/COLD RULE: show the meter only during the REGULAR SEASON —
/// from Opening Day through the last regular-season day, both from
/// `/season/phase`. Hidden in the postseason, the offseason and spring
/// training: the heat scale is calibrated on regular-season play (postseason
/// games — tougher opposition, tiny samples — would skew players cold), and a
/// meter frozen after a team's season ends reads as current when it isn't.
/// The nightly keeps computing heat scores, so the meter returns by itself
/// next Opening Day.
///
/// ⚠️ UNKNOWN HIDES: a failed fetch (`phase` nil) or a season with no
/// published schedule (either date nil) hides the meter — better absent than
/// a stale or mis-scaled reading.
///
/// - Parameters:
///   - today: the Eastern date, `yyyy-MM-dd` (MLB schedules off ET).
///   - phase: `today`'s season's span.
func showsHotCold(today: String, phase: SeasonPhase?) -> Bool {
    guard let first = phase?.openingDay, let last = phase?.lastRegularDay else { return false }
    return first <= today && today <= last
}

/// What the Overview's recent-games section shows.
enum RecentGamesMode: Equatable {
    /// "Recent Games": the regular-season rolling windows.
    case regular
    /// "Recent Postseason Games": his latest games of this postseason.
    case postseason
    case hidden
}

/// ⚠️ THE RECENT GAMES RULE:
///   • the regular season — Opening Day through the last regular day, from
///     `/season/phase` (the same span as `showsHotCold`) — shows the
///     regular-season windows, as always;
///   • while the league's postseason is in progress AND he has appeared in it,
///     his latest postseason games;
///   • otherwise — postseason without an appearance, offseason, spring
///     training, an unknown phase — nothing.
func recentGamesMode(today: String, phase: SeasonPhase?, postseason: PlayerPostseason?) -> RecentGamesMode {
    if showsHotCold(today: today, phase: phase) { return .regular }
    if postseason?.current.leagueInProgress == true, postseason?.current.playerAppeared == true {
        return .postseason
    }
    return .hidden
}

/// Reads `/season/phase` for today's season and remembers it for 15 minutes,
/// so opening profile after profile doesn't refetch it. nil = unknown.
@MainActor
enum HotColdSeason {
    private static var cached: (value: SeasonPhase, at: Date)?

    static func load(now: Date = Date(), api: APIClient = .shared) async -> SeasonPhase? {
        if let cached, now.timeIntervalSince(cached.at) < 15 * 60 { return cached.value }
        let season = Int(easternDateString(now).prefix(4)) ?? Calendar.current.component(.year, from: now)
        guard let phase = try? await api.getSeasonPhase(season: season) else {
            return nil                      // unknown: callers hide, and retry next time
        }
        cached = (phase, now)
        return phase
    }

    static func easternDateString(_ date: Date) -> String {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "America/New_York") ?? .current
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: date)
    }
}
