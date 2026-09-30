//
//  LiveBracketTests.swift
//
//  The live bracket: decoding `/postseason/bracket`, what each slot state
//  shows, the bracket's placement, the tap-through's game resolution, and the
//  rule for when the Standings tab opens on the bracket.
//
//  `testdata/postseason-bracket-2026.json` and `-2025.json` are the backend's
//  OWN output (`postseason_series.build_bracket`): the real 2026 pre-Wild-Card
//  payload with ATL winning NL Wild Card Game 1, and the finished 2025
//  bracket — so a field renamed on one side fails here.
//

import Foundation
import Testing
@testable import BaseballStats

@MainActor
private func bracket(_ season: Int) throws -> LiveBracket {
    let url = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()   // BaseballStatsTests
        .deletingLastPathComponent()   // BaseballStats
        .deletingLastPathComponent()   // repo root
        .appendingPathComponent("testdata/postseason-bracket-\(season).json")
    return try JSONDecoder().decode(LiveBracket.self, from: Data(contentsOf: url))
}

private func iso(_ s: String) -> Date { LiveBracketDates.parse(s)! }

@MainActor
@Suite("Live bracket")
struct LiveBracketTests {

    // MARK: decoding and slot states

    @Test func decodesTheBackendPayload() throws {
        let b = try bracket(2026)
        #expect(b.season == 2026)
        #expect(b.isDrawable)
        let al = try #require(b.al)
        #expect(al.seeds.map(\.team) == ["TB", "CLE", "HOU", "NYY", "BOS", "CHW"])
        #expect(al.slots.map(\.id) == ["AL-WC-3v6", "AL-WC-4v5", "AL-DS-1", "AL-DS-2", "AL-CS"])
    }

    @Test func anUndecidedSideListsItsCandidates() throws {
        let ds1 = try #require(try bracket(2026).al?.slot("DS-1"))
        #expect(ds1.state == .tbd)
        #expect(ds1.sides[0].team == "TB" && ds1.sides[0].seed == 1)
        #expect(ds1.sides[1].team == nil)
        #expect(ds1.sides[1].tbdLabel == "NYY/BOS")
        #expect(ds1.series == nil)
        #expect(ds1.statusLine == "Best of 5")
    }

    @Test func anLCSSideBeforeTheWildCardRoundIsTBD() throws {
        let cs = try #require(try bracket(2026).nl?.slot("CS"))
        #expect(cs.sides.allSatisfy { $0.team == nil && $0.tbdLabel == "TBD" })
        #expect(cs.statusLine == "Best of 7")
    }

    @Test func aRunningSeriesShowsWhereItStands() throws {
        let wc = try #require(try bracket(2026).nl?.slot("WC-3v6"))
        #expect(wc.state == .inProgress)
        #expect(wc.statusLine == "ATL leads 1-0")
        #expect(wc.wins(for: wc.sides[0]) == 1)
        #expect(wc.wins(for: wc.sides[1]) == 0)
        #expect(wc.winner == nil)
    }

    @Test func aScheduledSeriesShowsItsLength() throws {
        let wc = try #require(try bracket(2026).nl?.slot("WC-4v5"))
        #expect(wc.state == .scheduled)
        #expect(wc.statusLine == "Best of 3")
        #expect(wc.wins(for: wc.sides[0]) == nil)     // no "0 0" before the first pitch
    }

    @Test func aFinishedBracketHasItsChampion() throws {
        let ws = try #require(try bracket(2025).worldSeries)
        #expect(ws.state == .complete)
        #expect(ws.winner == "LAD")
        #expect(ws.statusLine == "LAD wins 4-3")
        #expect(ws.series?.games.count == 7)
    }

    // MARK: placement

    @Test func eachWildCardSitsBesideTheDivisionSeriesItFeeds() throws {
        let layout = try #require(LiveBracketView.layout(try bracket(2026), metrics: .live))
        #expect(layout.placed.count == 11)
        #expect(layout.lines.count == 10)
        let y = Dictionary(uniqueKeysWithValues: layout.placed.map { ($0.id, $0.center.y) })
        // Seed 1 meets the 4/5 winner, seed 2 the 3/6 winner.
        #expect(y["AL-WC-4v5"] == y["AL-DS-1"])
        #expect(y["AL-WC-3v6"] == y["AL-DS-2"])
        #expect(y["NL-WC-4v5"] == y["NL-DS-1"])
        #expect(layout.placed.first { $0.isFinal }?.id == "WS")
    }

    @Test func untrustedSeedsDrawNoBracket() throws {
        let b = try bracket(2026)
        let al = try #require(b.al)
        let broken = LiveBracket(
            season: b.season,
            leagues: ["AL": LiveBracketLeague(trusted: false, seeds: [], slots: [], series: al.slots.compactMap(\.series)),
                      "NL": try #require(b.nl)],
            worldSeries: nil)
        #expect(!broken.isDrawable)
        #expect(LiveBracketView.layout(broken, metrics: .live) == nil)
    }

    @Test func layoutScalesWithDynamicType() throws {
        let b = try bracket(2026)
        let base = try #require(LiveBracketView.layout(b, metrics: .live))
        let big = try #require(LiveBracketView.layout(b, metrics: BracketMetrics.live.scaled(2)))
        #expect(big.size.width == base.size.width * 2)
        #expect(big.size.height == base.size.height * 2)
    }

    @Test func historyMetricsAreUnscaled() {
        // Playoff History's geometry must not move: its metrics are the Phase
        // 2a constants and every derived offset keys on topInset / 92 == 1.
        let m = BracketMetrics.history
        #expect(m.boxW == 132 && m.boxH == 58 && m.colGap == 50 && m.dsGap == 30)
        #expect(m.leagueGap == 84 && m.topInset == 92 && m.sideInset == 22)
    }

    // MARK: tap-through

    @Test func aGameResolvesOnItsEasternDay() throws {
        let wc = try #require(try bracket(2026).nl?.slot("WC-3v6"))
        let g1 = try #require(wc.series?.games.first)
        #expect(g1.easternDate == "2026-09-29")
        #expect(g1.hasBoxScore)
        #expect(!(wc.series?.games[1].hasBoxScore ?? true))   // Game 2 not started
    }

    @Test func aLateGameFilesUnderTheEasternDayNotUTC() throws {
        let late = try JSONDecoder().decode(PostseasonSeriesGame.self, from: Data("""
        {"game_id": 1, "game_number": 1, "date": "2026-09-30T01:38:00.000Z", "status": "STATUS_FINAL",
         "label": "NL Wild Card · Game 1", "if_necessary": false, "series_status": "SD leads 1-0",
         "line": "SD leads 1-0", "can_clinch": false, "elimination_game": false}
        """.utf8))
        #expect(late.easternDate == "2026-09-29")
    }

    @Test func balldontlieAbbreviationsReachTheColourTable() {
        #expect(lahmanCode(forBDLAbbreviation: "LAD") == "LAN")
        #expect(lahmanCode(forBDLAbbreviation: "CHW") == "CHA")
        #expect(lahmanCode(forBDLAbbreviation: "WSH") == "WAS")   // not MON
        #expect(lahmanCode(forBDLAbbreviation: "MIA") == "MIA")   // not FLO
        #expect(lahmanCode(forBDLAbbreviation: "LAA") == "LAA")   // not ANA
        #expect(lahmanCode(forBDLAbbreviation: "ATL") == "ATL")
    }

    // MARK: when the Standings tab opens on the bracket

    @Test func fromTheFirstPitchTheBracketIsTheDefault() {
        let first = iso("2026-09-29T18:00:00Z")
        #expect(standingsDefault(now: iso("2026-09-29T17:59:00Z"), currentYear: 2026,
                                 currentYearFirstPitch: first, currentYearGamesPlayed: true) == .standings)
        #expect(standingsDefault(now: iso("2026-09-29T18:00:00Z"), currentYear: 2026,
                                 currentYearFirstPitch: first, currentYearGamesPlayed: true) == .bracket(season: 2026))
    }

    @Test func itStaysThroughTheWinterAfterTheWorldSeries() {
        // December: still the current year's bracket.
        #expect(standingsDefault(now: iso("2026-12-15T12:00:00Z"), currentYear: 2026,
                                 currentYearFirstPitch: iso("2026-09-29T18:00:00Z"),
                                 currentYearGamesPlayed: true) == .bracket(season: 2026))
        // January to Opening Day: the new year has no games and no bracket —
        // last season's bracket.
        #expect(standingsDefault(now: iso("2027-02-01T12:00:00Z"), currentYear: 2027,
                                 currentYearFirstPitch: nil,
                                 currentYearGamesPlayed: false) == .bracket(season: 2026))
    }

    @Test func openingDayEndsIt() {
        #expect(standingsDefault(now: iso("2027-03-27T12:00:00Z"), currentYear: 2027,
                                 currentYearFirstPitch: nil, currentYearGamesPlayed: true) == .standings)
    }

    @Test func anUnknownGamesPlayedIsNotNoGames() {
        // A failed standings fetch in July must not reopen last October's bracket.
        #expect(standingsDefault(now: iso("2027-07-04T12:00:00Z"), currentYear: 2027,
                                 currentYearFirstPitch: nil, currentYearGamesPlayed: nil) == .standings)
    }

    @Test func thereIsNoBracketBefore2022() {
        #expect(standingsDefault(now: iso("2022-02-01T12:00:00Z"), currentYear: 2022,
                                 currentYearFirstPitch: nil, currentYearGamesPlayed: false) == .standings)
        #expect(standingsDefault(now: iso("2021-10-10T12:00:00Z"), currentYear: 2021,
                                 currentYearFirstPitch: iso("2021-10-05T00:00:00Z"),
                                 currentYearGamesPlayed: true) == .standings)
    }

    @Test func firstPitchIsTheEarliestGame() throws {
        #expect(try bracket(2026).firstPitch == iso("2026-09-29T18:00:00.000Z"))
    }
}

@MainActor
@Suite("Live bracket default loading")
struct LiveBracketDefaultTests {
    private struct Offline: Error {}

    @Test func duringThePostseasonItShowsThisYearsBracketWithOneFetch() async throws {
        let b26 = try bracket(2026)
        var fetched: [Int] = []
        let vm = LiveBracketViewModel(fetch: { season in fetched.append(season); return b26 })
        await vm.resolveDefault(now: iso("2026-09-30T02:00:00Z"), currentYear: 2026, currentYearGamesPlayed: true)
        #expect(vm.defaultView == .bracket(season: 2026))
        #expect(vm.state == .loaded(b26))
        #expect(fetched == [2026])
    }

    @Test func inTheWinterItLoadsLastSeasons() async throws {
        let b25 = try bracket(2025)
        var fetched: [Int] = []
        let vm = LiveBracketViewModel(fetch: { season in
            fetched.append(season)
            if season == 2025 { return b25 }
            throw Offline()     // 2026 before its postseason: no bracket yet
        })
        await vm.resolveDefault(now: iso("2026-02-01T12:00:00Z"), currentYear: 2026, currentYearGamesPlayed: false)
        #expect(vm.defaultView == .bracket(season: 2025))
        #expect(vm.state == .loaded(b25))
        #expect(fetched == [2026, 2025])
    }

    @Test func inSeasonItStaysOnTheStandings() async throws {
        let vm = LiveBracketViewModel(fetch: { _ in throw Offline() })
        await vm.resolveDefault(now: iso("2026-07-04T12:00:00Z"), currentYear: 2026, currentYearGamesPlayed: true)
        #expect(vm.defaultView == .standings)
        #expect(vm.season == nil)
        #expect(vm.state == .idle)
    }

    @Test func aQuietRefreshThatFailsKeepsTheBracket() async throws {
        let b26 = try bracket(2026)
        var fail = false
        let vm = LiveBracketViewModel(fetch: { _ in if fail { throw Offline() }; return b26 })
        await vm.resolveDefault(now: iso("2026-09-30T02:00:00Z"), currentYear: 2026, currentYearGamesPlayed: true)
        fail = true
        await vm.load(quiet: true)
        #expect(vm.state == .loaded(b26))
    }
}
