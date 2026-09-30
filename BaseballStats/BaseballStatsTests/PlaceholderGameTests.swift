//
//  PlaceholderGameTests.swift
//
//  balldontlie pre-lists postseason games whose teams aren't known yet,
//  as team "UNK" (id -1). Listed, a bye team's Home card read "vs UNK".
//  Every game list the client builds — `getGames` (Scores, Standings),
//  `getTeamGames` (Home hero and strip, the overlay) and
//  `getTeamSeasonGames` (the schedule) — drops them.
//
//  `testdata/placeholder-games.json` is balldontlie's real `/games` response
//  for 2026-10-02..04, fetched 2026-09-30: two real Wild Card Game 3s and
//  five Division Series games with one side still "UNK".
//

import Foundation
import Testing
@testable import BaseballStats

private final class StubPlaceholderProtocol: URLProtocol {
    nonisolated(unsafe) static var body = Data()

    override class func canInit(with request: URLRequest) -> Bool {
        request.url?.path.contains("/games") ?? false
    }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let response = HTTPURLResponse(url: request.url!, statusCode: 200,
                                       httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Self.body)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

private func fixtureData() throws -> Data {
    let url = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()   // BaseballStatsTests
        .deletingLastPathComponent()   // BaseballStats
        .deletingLastPathComponent()   // repo root
        .appendingPathComponent("testdata/placeholder-games.json")
    return try Data(contentsOf: url)
}

private func stubbedClient() throws -> BallDontLieClient {
    StubPlaceholderProtocol.body = try fixtureData()
    let config = URLSessionConfiguration.ephemeral
    config.protocolClasses = [StubPlaceholderProtocol.self]
    return BallDontLieClient(session: URLSession(configuration: config))
}

private let snake: JSONDecoder = {
    let d = JSONDecoder()
    d.keyDecodingStrategy = .convertFromSnakeCase
    return d
}()

private struct Envelope: Decodable { let data: [BDLGame] }

@MainActor
@Suite("Placeholder postseason games", .serialized)
struct PlaceholderGameTests {

    @Test func theFixtureHoldsBothKinds() throws {
        let games = try snake.decode(Envelope.self, from: fixtureData()).data
        let placeholders = games.filter { $0.hasPlaceholderTeam }.count
        let real = games.filter { !$0.hasPlaceholderTeam }.map { $0.awayTeam.abbreviation }.sorted()
        #expect(games.count == 7)
        #expect(placeholders == 5)
        #expect(real == ["BOS", "CHC"])
    }

    @Test func theRule() throws {
        let games = try snake.decode(Envelope.self, from: fixtureData()).data
        let half = try #require(games.first { $0.homeTeam.abbreviation == "TB" })
        #expect(half.awayTeam.id == -1 && half.hasPlaceholderTeam, "one side unknown is enough")
        let real = try #require(games.first { $0.homeTeam.abbreviation == "NYY" })
        #expect(!real.hasPlaceholderTeam)
    }

    @Test func getGamesNeverListsAPlaceholder() async throws {
        let client = try stubbedClient()
        var listed: [BDLGame] = []
        for day in ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"] {
            listed += try await client.getGames(date: day, bypassCache: true)
        }
        let anyPlaceholder = listed.contains { $0.hasPlaceholderTeam }
        let ids = Set(listed.map { $0.id })
        #expect(!anyPlaceholder)
        #expect(ids.count == 2, "both real Game 3s still listed")
    }

    @Test func aByeTeamsHomeListHasNoUNK() async throws {
        // TB has the AL bye; its only listed game is "UNK @ TB".
        let client = try stubbedClient()
        var listed: [BDLGame] = []
        for day in ["2026-10-02", "2026-10-03", "2026-10-04"] {
            listed += try await client.getTeamGames(date: day, teamId: 27, bypassCache: true)
        }
        let anyPlaceholder = listed.contains { $0.hasPlaceholderTeam }
        #expect(!anyPlaceholder)
    }
}
