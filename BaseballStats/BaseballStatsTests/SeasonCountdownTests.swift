//
//  SeasonCountdownTests.swift
//
//  The offseason countdown card's text (Isaac, 2026-10-31), from the server's
//  `GET /season/countdown` body.
//

import Foundation
import Testing
@testable import BaseballStats

private func decode(_ json: String) throws -> SeasonCountdown {
    try JSONDecoder().decode(SeasonCountdown.self, from: Data(json.utf8))
}

/// Noon Eastern on an Eastern date.
private func eastern(_ y: Int, _ m: Int, _ d: Int, hour: Int = 12) -> Date {
    var c = DateComponents(year: y, month: m, day: d, hour: hour)
    c.timeZone = TimeZone(identifier: "America/New_York")
    return Calendar(identifier: .gregorian).date(from: c)!
}

@Suite("Season countdown")
struct SeasonCountdownTests {
    private let report = #"{"season": 2027, "stage": "pitchers_catchers_report", "label": "Pitchers & catchers report", "date": "2027-02-09", "days": 100, "source": "manual"}"#

    @Test func pitchersAndCatchersFromTheDayAfterTheWorldSeries() throws {
        let c = try decode(report)
        #expect(c.isActive)
        #expect(c.countText(eastern(2026, 11, 1)) == "100 days")
        #expect(c.dateText == "Tuesday, February 9")
    }

    /// Counted on the device from the date, so a screen left open past midnight
    /// moves on — and by the EASTERN calendar: 11:30pm Pacific on Feb 7 is
    /// already Feb 8 in New York.
    @Test func theCountFollowsTheEasternDate() throws {
        let c = try decode(report)
        #expect(c.countText(eastern(2027, 2, 8)) == "1 day")
        #expect(c.countText(eastern(2027, 2, 9)) == "Today")
        #expect(c.countText(eastern(2027, 2, 8, hour: 2)) == "1 day")
        #expect(c.countText(eastern(2027, 2, 7, hour: 23)) == "2 days")
    }

    /// The clock runs to midnight Eastern starting the day, and stops there.
    @Test func theClockRunsToMidnightEastern() throws {
        let c = try decode(report)
        let r = try #require(c.remaining(eastern(2027, 2, 8, hour: 12)))
        #expect((r.days, r.hours, r.minutes, r.seconds) == (0, 12, 0, 0))
        let far = try #require(c.remaining(eastern(2026, 11, 1, hour: 12)))
        #expect((far.days, far.hours) == (99, 12))
        #expect(c.remaining(eastern(2027, 2, 9, hour: 0)) == nil)
    }

    @Test func anUnannouncedDateIsNamedWithoutACount() throws {
        let c = try decode(#"{"season": 2027, "stage": "spring_training_start", "label": "Spring training games begin", "date": null, "days": null, "source": null}"#)
        #expect(c.isActive)
        #expect(c.countText(eastern(2027, 2, 10)) == nil)
        #expect(c.dateText == "Date not announced yet")
    }

    @Test func noCountdownWhileASeasonIsOn() throws {
        let c = try decode(#"{"season": null, "stage": null, "label": null, "date": null, "days": null, "source": null}"#)
        #expect(!c.isActive)
    }
}
