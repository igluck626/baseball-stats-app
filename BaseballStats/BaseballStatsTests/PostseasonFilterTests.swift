//
//  PostseasonFilterTests.swift
//
//  BDL's `/games` feed lists the postseason alongside the regular season,
//  and every place the app adds today's game to a season figure was written
//  when only regular-season games were on it. These pin the one rule
//  (`SeasonType`) and each place it is applied: the profile overlay's game
//  list, its season-stats row, the Scores / box-score bumps, and the standings
//  fold. A missing season type (older payloads, our own historical games)
//  counts as regular.
//

import Foundation
import Testing
@testable import BaseballStats

private func team(_ id: Int, _ abbr: String) -> BDLTeam {
    BDLTeam(id: id, slug: abbr.lowercased(), abbreviation: abbr,
            displayName: abbr, shortDisplayName: abbr, name: abbr,
            location: abbr, league: "National", division: "East")
}

private func bdlGame(id: Int = 1, status: String = "STATUS_FINAL",
                     seasonType: String?, postseason: Bool?,
                     iso: String = "2026-09-29T18:00:00.000Z",
                     homeRuns: Int = 5, awayRuns: Int = 2) -> BDLGame {
    BDLGame(
        id: id,
        homeTeam: team(10, "ATL"), awayTeam: team(20, "PHI"),
        homeTeamData: BDLTeamData(hits: nil, runs: homeRuns, errors: nil, inningScores: nil),
        awayTeamData: BDLTeamData(hits: nil, runs: awayRuns, errors: nil, inningScores: nil),
        date: iso, status: status, venue: nil, period: 9, displayClock: nil,
        scoringSummary: nil, season: 2026, seasonType: seasonType, postseason: postseason,
        homeTeamName: "Atlanta Braves", awayTeamName: "Philadelphia Phillies",
    )
}

private let wildCard = bdlGame(seasonType: "postseason", postseason: true)
private let regular  = bdlGame(seasonType: "regular", postseason: false,
                               iso: "2026-09-27T17:35:00.000Z")

private let snakeDecoder: JSONDecoder = {
    let d = JSONDecoder()
    d.keyDecodingStrategy = .convertFromSnakeCase
    return d
}()

@Suite("Postseason filter")
struct PostseasonFilterTests {

    // ── the rule ────────────────────────────────────────────────────────

    @Test func theRule() {
        #expect(SeasonType.isRegular("regular", postseason: false))
        #expect(SeasonType.isRegular("postseason", postseason: true) == false)
        #expect(SeasonType.isRegular("spring_training", postseason: false) == false)
        #expect(SeasonType.isRegular(nil, postseason: nil), "missing type counts as regular")
        #expect(SeasonType.isRegular(nil, postseason: true) == false,
                "an explicit postseason flag wins over a missing type")
    }

    /// Older payloads carry no `season_type` at all — they must still decode,
    /// and count as regular.
    @Test func aPayloadWithoutSeasonTypeDecodesAsRegular() throws {
        let json = """
        {"id": 7, "home_team": {"id": 10, "abbreviation": "ATL", "display_name": "Atlanta Braves",
          "name": "Braves", "location": "Atlanta"},
         "away_team": {"id": 20, "abbreviation": "PHI", "display_name": "Philadelphia Phillies",
          "name": "Phillies", "location": "Philadelphia"},
         "date": "2026-05-01T23:05:00.000Z", "status": "STATUS_FINAL", "season": 2026}
        """
        let g = try snakeDecoder.decode(BDLGame.self, from: Data(json.utf8))
        #expect(g.seasonType == nil)
        #expect(g.isRegularSeason)
        #expect(g.toGame().isRegularSeason)
    }

    @Test func toGameCarriesTheSeasonType() {
        #expect(wildCard.toGame().isRegularSeason == false)
        #expect(regular.toGame().isRegularSeason)
        // Flag set, type missing: still postseason once converted.
        #expect(bdlGame(seasonType: nil, postseason: true).toGame().isRegularSeason == false)
    }

    // ── the profile overlay ─────────────────────────────────────────────

    @Test func aPostseasonFinalDoesNotOverlay() {
        let live = bdlGame(id: 2, status: "STATUS_IN_PROGRESS", seasonType: "postseason", postseason: true)
        #expect(PlayerViewModel.overlayCandidates([wildCard, live]).isEmpty)
    }

    @Test func theRegularSeasonPathIsUnchanged() {
        let ids = PlayerViewModel.overlayCandidates([regular, wildCard]).map(\.id)
        #expect(ids == [regular.id])
        let legacy = bdlGame(id: 3, seasonType: nil, postseason: nil)
        #expect(PlayerViewModel.overlayCandidates([legacy]).map(\.id) == [3])
    }

    /// Cade Smith 2024 as BDL still ships it: the postseason row FIRST.
    @Test func theRegularSeasonStatsRowIsPicked() throws {
        let json = """
        [{"player": {"id": 841, "full_name": "Cade Smith"}, "postseason": true,  "pitching_gp": 9},
         {"player": {"id": 841, "full_name": "Cade Smith"}, "postseason": false, "pitching_gp": 74},
         {"player": {"id": 99,  "full_name": "Someone Else"}, "postseason": false, "pitching_gp": 5}]
        """
        let rows = try snakeDecoder.decode([BDLSeasonStat].self, from: Data(json.utf8))
        #expect(BDLSeasonStat.regularRow(in: rows, for: 841)?.pitchingGp == 74)
        #expect(BDLSeasonStat.regularRow(in: [rows[0]], for: 841) == nil,
                "a postseason-only player gets no row, never the playoff line")
        let legacy = try snakeDecoder.decode([BDLSeasonStat].self, from: Data("""
        [{"player": {"id": 5, "full_name": "Old Shape"}, "pitching_gp": 30}]
        """.utf8))
        #expect(BDLSeasonStat.regularRow(in: legacy, for: 5)?.pitchingGp == 30)
    }

    // ── the Scores / box-score bumps ────────────────────────────────────

    @Test func aPostseasonWinDoesNotBumpTheRegularSeasonRecord() {
        #expect(SeasonType.regularSeasonBump(1, includesToday: false, game: wildCard.toGame()) == 0)
        #expect(SeasonType.regularSeasonBump(2, includesToday: false, game: wildCard.toGame()) == 0,
                "nor a regular-season HR total")
    }

    @Test func theRegularSeasonBumpIsUnchanged() {
        #expect(SeasonType.regularSeasonBump(1, includesToday: false, game: regular.toGame()) == 1)
        #expect(SeasonType.regularSeasonBump(2, includesToday: false, game: regular.toGame()) == 2)
        #expect(SeasonType.regularSeasonBump(1, includesToday: true, game: regular.toGame()) == 0)
    }

    // ── the standings fold ──────────────────────────────────────────────

    @Test func aPostseasonFinalIsNotFoldedIntoTheStandings() {
        let now = ISO8601DateFormatter().date(from: "2026-09-29T21:30:00Z")!
        let results = TodayRecordAdjustments.qualifyingResults(
            from: [wildCard.toGame()], lastUpdated: "2026-09-29T15:05:00Z", now: now)
        #expect(results.isEmpty)
        let reg = bdlGame(id: 4, seasonType: "regular", postseason: false).toGame()
        #expect(TodayRecordAdjustments.qualifyingResults(
            from: [reg], lastUpdated: "2026-09-29T15:05:00Z", now: now).count == 1,
                "same game as regular season still qualifies")
    }

    // ── the backend stopgap, read by builds WITHOUT this filter ─────────

    /// Builds before this change still see the Wild Card final. The backend
    /// now answers `includes_today = true` for any date after the regular
    /// season, and for a player whose regular season is in sync that must
    /// make the overlay refuse — the whole point of the stopgap. The new
    /// build never asks about that game at all (`aPostseasonFinalDoesNotOverlay`).
    @Test func theBackendStopgapMakesOldBuildsRefuse() {
        let synced = OverlayDecision.Side(seasonG: 156, bdlG: 156, gamelogRows: 156,
                                          includesToday: true, seasonRowIncludesToday: nil)
        let none = OverlayDecision.Side(seasonG: nil, bdlG: nil, gamelogRows: nil,
                                        includesToday: false, seasonRowIncludesToday: nil)
        #expect(OverlayDecision.decide(batting: synced, pitching: none).shouldOverlayFinals == false)
        // Without the stopgap (includes_today false) the same player overlays —
        // which is exactly the bug old builds have.
        let unguarded = OverlayDecision.Side(seasonG: 156, bdlG: 156, gamelogRows: 156,
                                             includesToday: false, seasonRowIncludesToday: nil)
        #expect(OverlayDecision.decide(batting: unguarded, pitching: none).shouldOverlayFinals)
    }
}
