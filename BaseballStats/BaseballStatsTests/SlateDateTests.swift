//
//  SlateDateTests.swift
//  BaseballStatsTests
//
//  What the Scores slate contains for a date, and why the raw endpoint
//  can't tell you.
//
//  BDL buckets /games by UTC, so an 8:05pm ET game lands in the NEXT
//  day's bucket. Reading that response directly makes the slate look a
//  day out — asking for 2026-09-05 returns games played on the 4th, and
//  two Tampa Bay games at Texas. An earlier diagnosis did exactly that
//  and reported a user-facing bug that did not exist.
//
//  `getGames(date:)` never passes that response on: it asks for the day
//  either side as well, then keeps only what started on the requested
//  date in Eastern time. THAT BEHAVIOUR is what's under test here, so
//  these drive the real method over stubbed bytes rather than asserting
//  against a captured result — a captured result is evidence about BDL,
//  not about the client.
//
//  The fixture holds the RAW three-bucket unions, exactly as BDL
//  returns them, so the filtering still has real work to do.
//

import Foundation
import Testing
@testable import BaseballStats

private final class SlateAnchor {}

/// Serves `SlateFixtures.json` for any `/mlb/v1/games` request, keyed by
/// the `dates[]` the client asked for. An unexpected request fails the
/// test rather than silently returning nothing.
private final class StubSlateProtocol: URLProtocol {
    nonisolated(unsafe) static var buckets: [String: Data] = [:]
    nonisolated(unsafe) static var requestedDates: [[String]] = []

    override class func canInit(with request: URLRequest) -> Bool {
        request.url?.path.contains("/games") ?? false
    }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let url = request.url,
              let comps = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            client?.urlProtocol(self, didFailWithError: URLError(.badURL)); return
        }
        let dates = (comps.queryItems ?? [])
            .filter { $0.name == "dates[]" }
            .compactMap(\.value)
        Self.requestedDates.append(dates)
        guard let data = Self.buckets[dates.joined(separator: "+")] else {
            client?.urlProtocol(self, didFailWithError: URLError(.resourceUnavailable)); return
        }
        let response = HTTPURLResponse(url: url, statusCode: 200,
                                       httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

private func stubbedClient() throws -> BallDontLieClient {
    let bundle = Bundle(for: SlateAnchor.self)
    let url = try #require(bundle.url(forResource: "SlateFixtures", withExtension: "json"))
    let raw = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any]
    StubSlateProtocol.buckets = (raw ?? [:]).reduce(into: [:]) { out, entry in
        out[entry.key] = try? JSONSerialization.data(withJSONObject: entry.value)
    }
    StubSlateProtocol.requestedDates = []
    let config = URLSessionConfiguration.ephemeral
    config.protocolClasses = [StubSlateProtocol.self]
    return BallDontLieClient(session: URLSession(configuration: config))
}

private func easternDay(_ game: BDLGame) throws -> String {
    var cal = Calendar(identifier: .gregorian)
    cal.timeZone = TimeZone(identifier: "America/New_York")!
    let fmt = DateFormatter()
    fmt.calendar = cal
    fmt.timeZone = cal.timeZone
    fmt.locale = Locale(identifier: "en_US_POSIX")
    fmt.dateFormat = "yyyy-MM-dd"
    return fmt.string(from: try #require(game.startDate))
}

/// Serialized: the stub records what it served in static state, and
/// Swift Testing runs cases concurrently by default, so parallel cases
/// would interleave into each other's recording.
@Suite(.serialized)
struct SlateDateTests {
    @Test func theSlateAsksForTheDayEitherSide() async throws {
        let client = try stubbedClient()
        _ = try await client.getGames(date: "2026-09-05", bypassCache: true)
        #expect(StubSlateProtocol.requestedDates
                == [["2026-09-04", "2026-09-05", "2026-09-06"]],
                "one request carrying the ±1-day envelope")
    }

    @Test func theSlateKeepsOnlyTheDayTheGamesWerePlayed() async throws {
        let client = try stubbedClient()
        let games = try await client.getGames(date: "2026-09-05", bypassCache: true)

        // The raw union the client was handed holds 48 games across three
        // UTC buckets. MLB's own schedule for 2026-09-05 lists fifteen.
        #expect(games.count == 15, "got \(games.count) of 48 raw")
        for g in games {
            #expect(try easternDay(g) == "2026-09-05",
                    "\(g.awayTeam.abbreviation)@\(g.homeTeam.abbreviation) starts \(g.date)")
        }
        #expect(Set(games.map(\.id)).count == games.count, "deduped across buckets")
    }

    @Test func onlyOneOfTheTwoTampaBayGamesBelongsToEachDate() async throws {
        // BDL's bucket for 2026-09-05 holds BOTH 5059890 (8:05pm ET on the
        // 4th) and 5059902 (7:05pm ET on the 5th). Mistaking one for the
        // other is what sent an earlier box-score diagnosis to the wrong
        // game entirely, so the split is pinned from both directions.
        let client = try stubbedClient()
        func tbAtTex(_ date: String) async throws -> [BDLGame] {
            try await client.getGames(date: date, bypassCache: true)
                .filter { $0.awayTeam.abbreviation == "TB" && $0.homeTeam.abbreviation == "TEX" }
        }
        let fifth  = try await tbAtTex("2026-09-05")
        let fourth = try await tbAtTex("2026-09-04")

        #expect(fifth.map(\.id)  == [5059902])
        #expect(fourth.map(\.id) == [5059890])
    }
}
