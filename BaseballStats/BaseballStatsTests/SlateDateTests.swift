//
//  SlateDateTests.swift
//  BaseballStatsTests
//
//  What the Scores slate actually contains for a date.
//
//  BDL buckets /games by UTC, so a 8:05pm ET game lands in the NEXT
//  day's bucket — asking it for 2026-09-05 returns 19 games, 7 of them
//  played on the 4th, and two Tampa Bay games at Texas. None of that is
//  what the app renders: `getGames(date:)` over-fetches a ±1-day
//  envelope and filters to EASTERN-local start date before returning.
//
//  ⚠️ These hit the live API on purpose. A probe against the raw
//  endpoint is not evidence about the slate, which is exactly the
//  mistake this file exists to stop being repeated.
//

import Foundation
import Testing
@testable import BaseballStats

@Test func slateForADateMatchesTheDayItWasPlayed() async throws {
    let games = try await BallDontLieClient.shared.getGames(
        date: "2026-09-05", bypassCache: true)

    // MLB's own schedule for 2026-09-05 lists fifteen.
    #expect(games.count == 15, "got \(games.count)")

    // Every one of them starts on the 5th, Eastern.
    var cal = Calendar(identifier: .gregorian)
    cal.timeZone = TimeZone(identifier: "America/New_York")!
    let fmt = DateFormatter()
    fmt.calendar = cal
    fmt.timeZone = cal.timeZone
    fmt.locale = Locale(identifier: "en_US_POSIX")
    fmt.dateFormat = "yyyy-MM-dd"
    for g in games {
        let d = try #require(g.startDate)
        #expect(fmt.string(from: d) == "2026-09-05",
                "\(g.awayTeam.abbreviation)@\(g.homeTeam.abbreviation) starts \(g.date)")
    }
}

@Test func onlyOneOfTheTwoTampaBayGamesBelongsToThatDate() async throws {
    // BDL's raw bucket for 2026-09-05 holds both 5059890 (8:05pm ET on
    // the 4th) and 5059902 (7:05pm ET on the 5th). The slate must carry
    // the second alone — the first belongs to the 4th, and it was
    // mistaking one for the other that sent an earlier diagnosis to the
    // wrong game entirely.
    let fifth  = try await BallDontLieClient.shared.getGames(
        date: "2026-09-05", bypassCache: true)
    let fourth = try await BallDontLieClient.shared.getGames(
        date: "2026-09-04", bypassCache: true)

    let tbAtTexOnFifth  = fifth.filter  { $0.awayTeam.abbreviation == "TB" && $0.homeTeam.abbreviation == "TEX" }
    let tbAtTexOnFourth = fourth.filter { $0.awayTeam.abbreviation == "TB" && $0.homeTeam.abbreviation == "TEX" }

    #expect(tbAtTexOnFifth.count == 1)
    #expect(tbAtTexOnFifth.first?.id == 5059902)
    #expect(tbAtTexOnFourth.count == 1)
    #expect(tbAtTexOnFourth.first?.id == 5059890)
}
