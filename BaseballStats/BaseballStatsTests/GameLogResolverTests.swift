//
//  GameLogResolverTests.swift
//
//  Which slate a game-log row resolves through, from the boundary the server
//  reports (GET /meta/coverage → RetrosheetCoverage.lastSeason), never from the
//  calendar year. The case that broke: on 2027-01-01, before Retrosheet's 2026
//  release, the server still reports 2025 — and every 2026 row went untappable
//  because the provider branch was "the current calendar year".
//

import Foundation
import Testing
@testable import BaseballStats

/// Noon Eastern on `s`, so the day is the same in every US time zone the
/// simulator might run in (midnight ET on 2027-01-01 is still 2026 in Pacific).
private func day(_ s: String) -> Date {
    let f = DateFormatter()
    f.calendar = .init(identifier: .gregorian)
    f.locale = .init(identifier: "en_US_POSIX")
    f.timeZone = TimeZone(identifier: "America/New_York")
    f.dateFormat = "yyyy-MM-dd HH:mm"
    return f.date(from: s + " 12:00")!
}

struct GameLogResolverTests {
    @Test func a2026RowStaysTappableOnNewYearsDay2027() {
        let jan1 = day("2027-01-01")
        #expect(GameLogResolver.source(season: 2026, lastSeason: 2025, today: jan1) == .provider)
        #expect(GameLogResolver.canResolve(season: 2026, lastSeason: 2025, today: jan1))
        // The rule it replaces: provider only for the calendar year.
        let oldRule = 2026 <= 2025 || 2026 == Calendar(identifier: .gregorian).component(.year, from: jan1)
        #expect(oldRule == false)
    }

    @Test func onceTheServerReports2026ItIsOurs() {
        #expect(GameLogResolver.source(season: 2026, lastSeason: 2026, today: day("2027-03-01")) == .retrosheet)
    }

    @Test func everyEraKeepsItsSlate() {
        let jan1 = day("2027-01-01")
        #expect(GameLogResolver.source(season: 1898, lastSeason: 2025, today: jan1) == .retrosheet)
        #expect(GameLogResolver.source(season: 2025, lastSeason: 2025, today: jan1) == .retrosheet)
        #expect(GameLogResolver.source(season: 2027, lastSeason: 2025, today: jan1) == .provider)
    }

    @Test func noSlateForASeasonThatHasNotHappened() {
        #expect(GameLogResolver.source(season: 2028, lastSeason: 2025, today: day("2027-01-01")) == .none)
        #expect(!GameLogResolver.canResolve(season: 2028, lastSeason: 2025, today: day("2027-01-01")))
    }

    @Test func midSeasonTodayIsUnchanged() {
        let oct = day("2026-10-19")
        #expect(GameLogResolver.source(season: 2026, lastSeason: 2025, today: oct) == .provider)
        #expect(GameLogResolver.source(season: 2025, lastSeason: 2025, today: oct) == .retrosheet)
    }
}
