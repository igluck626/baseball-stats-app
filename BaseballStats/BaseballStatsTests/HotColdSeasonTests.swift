//
//  HotColdSeasonTests.swift
//
//  `showsHotCold`: the Hot/Cold meter shows only in the regular season —
//  Opening Day through the last regular-season day, both from
//  `/season/phase` — and an unknown phase hides it. Plus decoding the
//  endpoint's payload, published and not yet published.
//

import Foundation
import Testing
@testable import BaseballStats

private let season2026 = SeasonPhase(season: 2026, openingDay: "2026-03-25", lastRegularDay: "2026-09-27")
private let season2027 = SeasonPhase(season: 2027, openingDay: "2027-03-25", lastRegularDay: "2027-09-26")

@MainActor
@Suite("Hot/Cold season")
struct HotColdSeasonTests {

    // MARK: the rule

    @Test func midSeasonShows() {
        #expect(showsHotCold(today: "2026-07-15", phase: season2026))
    }

    @Test func theLastRegularSeasonDayShows() {
        #expect(showsHotCold(today: "2026-09-27", phase: season2026))
    }

    @Test func thePostseasonHides() {
        #expect(!showsHotCold(today: "2026-09-29", phase: season2026))
    }

    @Test func decemberHides() {
        #expect(!showsHotCold(today: "2026-12-10", phase: season2026))
    }

    @Test func springTrainingHides() {
        #expect(!showsHotCold(today: "2027-03-20", phase: season2027))
    }

    @Test func openingDayShows() {
        #expect(showsHotCold(today: "2027-03-25", phase: season2027))
    }

    @Test func unknownHides() {
        // The fetch failed.
        #expect(!showsHotCold(today: "2026-07-15", phase: nil))
        // The season's schedule isn't published yet (January).
        #expect(!showsHotCold(today: "2027-01-15",
                              phase: SeasonPhase(season: 2027, openingDay: nil, lastRegularDay: nil)))
    }

    // MARK: the payload

    @Test func decodesThePublishedSpan() throws {
        let json = #"{"season": 2026, "opening_day": "2026-03-25", "last_regular_day": "2026-09-27"}"#
        #expect(try JSONDecoder().decode(SeasonPhase.self, from: Data(json.utf8)) == season2026)
    }

    @Test func decodesAnUnpublishedSeason() throws {
        let json = #"{"season": 2027, "opening_day": null, "last_regular_day": null}"#
        let p = try JSONDecoder().decode(SeasonPhase.self, from: Data(json.utf8))
        #expect(p.openingDay == nil && p.lastRegularDay == nil)
    }

    @Test func easternToday() {
        // 03:00 UTC on 09-28 is 11pm on 09-27 in New York.
        let d = ISO8601DateFormatter().date(from: "2026-09-28T03:00:00Z")!
        #expect(HotColdSeason.easternDateString(d) == "2026-09-27")
    }
}
