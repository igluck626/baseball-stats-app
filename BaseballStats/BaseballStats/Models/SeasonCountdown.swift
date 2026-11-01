//
//  SeasonCountdown.swift
//
//  The offseason countdown on Home and Scores (Isaac, 2026-10-31): from the end
//  of the World Series to Opening Day, the next milestone — pitchers & catchers
//  report, spring training games begin, Opening Day — and the days until it.
//  The server picks the milestone and its date (`GET /season/countdown`,
//  `season_countdown.py`); every field is null when there is none, and the card
//  isn't drawn.
//

import Foundation

struct SeasonCountdown: Codable, Hashable {
    let season: Int?
    let stage: String?
    let label: String?
    /// Eastern date, "2027-02-09"; nil when not announced yet.
    let date: String?
    let days: Int?

    /// Whether there is a countdown to draw.
    var isActive: Bool { season != nil && label != nil }

    private static let eastern = TimeZone(identifier: "America/New_York")!

    private static let isoParser: DateFormatter = {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = eastern
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()

    /// "Tuesday, February 9".
    private static let display: DateFormatter = {
        let f = DateFormatter()
        f.timeZone = eastern
        f.setLocalizedDateFormatFromTemplate("EEEEMMMMd")
        return f
    }()

    var parsedDate: Date? { date.flatMap { Self.isoParser.date(from: $0) } }

    /// Days from `now`'s Eastern date to the milestone — counted here rather
    /// than taken from the server, so a screen left open past midnight still
    /// counts down. Falls back to the server's figure if the date won't parse.
    func daysUntil(_ now: Date = Date()) -> Int? {
        guard let target = parsedDate else { return days }
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = Self.eastern
        return cal.dateComponents([.day], from: cal.startOfDay(for: now), to: cal.startOfDay(for: target)).day
    }

    /// "100 days", "1 day", "Today"; nil when the date isn't known.
    func countText(_ now: Date = Date()) -> String? {
        guard let n = daysUntil(now) else { return nil }
        if n <= 0 { return "Today" }
        return n == 1 ? "1 day" : "\(n) days"
    }

    /// Days, hours, minutes and seconds to midnight Eastern starting the
    /// milestone's day — the clock's digits. No milestone has an official hour,
    /// so the clock runs out as the day begins in New York. Nil when the date
    /// isn't known or has arrived.
    func remaining(_ now: Date = Date()) -> (days: Int, hours: Int, minutes: Int, seconds: Int)? {
        guard let target = parsedDate else { return nil }
        let total = Int(target.timeIntervalSince(now).rounded(.up))
        guard total > 0 else { return nil }
        return (total / 86_400, total % 86_400 / 3_600, total % 3_600 / 60, total % 60)
    }

    /// "Tuesday, February 9", or "Date not announced yet".
    var dateText: String {
        parsedDate.map { Self.display.string(from: $0) } ?? "Date not announced yet"
    }
}
