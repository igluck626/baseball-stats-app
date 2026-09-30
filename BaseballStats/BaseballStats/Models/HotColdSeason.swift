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

/// Reads `showsHotCold`'s input and remembers the answer for 15 minutes, so
/// opening profile after profile doesn't refetch it.
@MainActor
enum HotColdSeason {
    private static var cached: (value: Bool, at: Date)?

    static func load(now: Date = Date(), api: APIClient = .shared) async -> Bool {
        if let cached, now.timeIntervalSince(cached.at) < 15 * 60 { return cached.value }
        let today = easternDateString(now)
        let season = Int(today.prefix(4)) ?? Calendar.current.component(.year, from: now)
        guard let phase = try? await api.getSeasonPhase(season: season) else {
            return false                    // unknown: hide, and retry next time
        }
        let value = showsHotCold(today: today, phase: phase)
        cached = (value, now)
        return value
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
