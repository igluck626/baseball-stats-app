//
//  SeriesStoreTests.swift
//
//  The postseason series line: decoding `/postseason/series`, the text shown
//  for each series state, and the line staying hidden when it should — a
//  failed fetch, a regular-season game, a game the endpoint doesn't know.
//
//  `testdata/postseason-series.json` is the backend's OWN output
//  (`postseason_series.build_series`) for the real 2026 pre-Wild-Card payload
//  with CHW winning Game 1, plus 2025's LAD-CIN sweep — so a field renamed on
//  one side fails here.
//

import Foundation
import Testing
@testable import BaseballStats

private func payload() throws -> PostseasonSeriesResponse {
    let url = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()   // BaseballStatsTests
        .deletingLastPathComponent()   // BaseballStats
        .deletingLastPathComponent()   // repo root
        .appendingPathComponent("testdata/postseason-series.json")
    return try JSONDecoder().decode(PostseasonSeriesResponse.self, from: Data(contentsOf: url))
}

private func team(_ id: Int, _ abbr: String) -> BDLTeam {
    BDLTeam(id: id, slug: abbr.lowercased(), abbreviation: abbr, displayName: abbr,
            shortDisplayName: abbr, name: abbr, location: abbr, league: nil, division: nil)
}

/// A card's `Game` for balldontlie game `id`.
private func game(_ id: Int, postseason: Bool = true, date: String = "2026-09-29T18:00:00.000Z") -> Game {
    BDLGame(
        id: id, homeTeam: team(1, "HOU"), awayTeam: team(2, "CHW"),
        homeTeamData: nil, awayTeamData: nil, date: date, status: "STATUS_SCHEDULED",
        venue: nil, period: nil, displayClock: nil, scoringSummary: nil, season: 2026,
        seasonType: postseason ? "postseason" : "regular", postseason: postseason,
        homeTeamName: nil, awayTeamName: nil,
    ).toGame()
}

private struct Offline: Error {}

@MainActor
@Suite("Postseason series line")
struct SeriesStoreTests {

    private func loadedStore() async throws -> SeriesStore {
        let resp = try payload()
        let store = SeriesStore(fetch: { _ in resp })
        await store.load(season: 2026)
        return store
    }

    @Test func decodesTheBackendPayload() throws {
        let resp = try payload()
        #expect(resp.season == 2026)
        #expect(resp.series.count == 3)
        let chw = try #require(resp.series.first { Set($0.teams) == ["CHW", "HOU"] })
        #expect(chw.roundName == "AL Wild Card")
        #expect(chw.bestOf == 3)
        #expect(chw.wins == ["CHW": 1, "HOU": 0])
        #expect(chw.isOver == false && chw.winner == nil)
        #expect(chw.games.map(\.gameNumber) == [1, 2, 3])
        #expect(chw.games[2].ifNecessary)
        #expect(chw.games[1].canClinch && chw.games[1].eliminationGame)
        let lad = try #require(resp.series.first { Set($0.teams) == ["CIN", "LAD"] })
        #expect(lad.isOver && lad.winner == "LAD")
    }

    // ── the text for each state ─────────────────────────────────────────

    @Test func preGameFirstGame() async throws {
        let store = try await loadedStore()
        #expect(store.line(for: game(15457180)) == "NL Wild Card · Game 1")
        #expect(store.compactLine(for: game(15457180)) == "Game 1")
    }

    @Test func afterAFinal() async throws {
        let store = try await loadedStore()
        #expect(store.line(for: game(15457178)) == "CHW leads 1-0")
    }

    @Test func preGameWithASeriesState() async throws {
        let store = try await loadedStore()
        #expect(store.line(for: game(15467362)) == "AL Wild Card · Game 2 · CHW leads 1-0")
        #expect(store.compactLine(for: game(15467362)) == "CHW leads 1-0")
    }

    /// A Game 3 is only played at 1-1, so it shows no series state — never
    /// tonight's "CHW leads 1-0" — and carries no clinch flag.
    @Test func ifNecessaryGame() async throws {
        let store = try await loadedStore()
        #expect(store.line(for: game(15467366)) == "AL Wild Card · Game 3 (if necessary)")
        #expect(store.compactLine(for: game(15467366)) == "Game 3")
        let resp = try payload()
        let g3 = try #require(resp.series.flatMap(\.games).first { $0.gameId == 15467366 })
        #expect(g3.ifNecessary && g3.seriesStatus == nil && !g3.canClinch && !g3.eliminationGame)
    }

    @Test func seriesOver() async throws {
        let store = try await loadedStore()
        #expect(store.line(for: game(4509088, date: "2025-10-02T00:00:00.000Z")) == "LAD wins 2-0")
    }

    // ── hidden ──────────────────────────────────────────────────────────

    @Test func hiddenWhenTheEndpointFails() async {
        let store = SeriesStore(fetch: { _ in throw Offline() })
        await store.load(season: 2026)
        #expect(store.line(for: game(15457180)) == nil)
        #expect(store.compactLine(for: game(15457180)) == nil)
    }

    @Test func aLaterFailureKeepsTheLastGoodLines() async throws {
        let resp = try payload()
        var fail = false
        let store = SeriesStore(fetch: { _ in if fail { throw Offline() }; return resp })
        await store.load(season: 2026)
        fail = true
        await store.load(season: 2026, force: true)
        #expect(store.line(for: game(15457178)) == "CHW leads 1-0")
    }

    @Test func hiddenForARegularSeasonGame() async throws {
        let store = try await loadedStore()
        // Same id as a postseason game, but a regular-season Game: never a line.
        #expect(store.line(for: game(15457180, postseason: false)) == nil)
        #expect(store.compactLine(for: game(15457180, postseason: false)) == nil)
    }

    @Test func hiddenForAGameTheEndpointDoesNotKnow() async throws {
        let store = try await loadedStore()
        #expect(store.line(for: game(999)) == nil)
    }

    // ── refetch on a final ──────────────────────────────────────────────

    @Test func refetchesWhenAKnownGameGoesFinal() async throws {
        let resp = try payload()
        var calls = 0
        let store = SeriesStore(fetch: { _ in calls += 1; return resp })
        await store.load(season: 2026)
        #expect(calls == 1)
        store.noteLiveList([15467362: "in_progress"])
        store.noteLiveList([15467362: "final"])
        try await Task.sleep(for: .milliseconds(200))
        #expect(calls == 2)
        // An unknown game going final, or a game first seen already final,
        // does not refetch.
        store.noteLiveList([999: "in_progress"]); store.noteLiveList([999: "final"])
        store.noteLiveList([15467366: "final"])
        try await Task.sleep(for: .milliseconds(200))
        #expect(calls == 2)
    }

    @Test func onlyLoadsInPostseasonMonths() async throws {
        let resp = try payload()
        var calls = 0
        let store = SeriesStore(fetch: { _ in calls += 1; return resp })
        let june = ISO8601DateFormatter().date(from: "2026-06-15T12:00:00Z")!
        await store.loadCurrentIfPostseason(now: june)
        #expect(calls == 0)
        let october = ISO8601DateFormatter().date(from: "2026-10-02T12:00:00Z")!
        await store.loadCurrentIfPostseason(now: october)
        #expect(calls == 1)
    }
}
