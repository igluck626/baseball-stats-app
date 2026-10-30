//
//  PostseasonTotalsTests.swift
//
//  On a postseason game the figure beside a player — "2B: Rice (3)", a Scores
//  final's HR line, a pitcher's "(W 5-1)" — is that POSTSEASON's (Isaac,
//  2026-10-29). It used to be left off, because every figure the app held was
//  the regular season's. A playoff game now asks the record-at-date endpoints
//  for `postseason=true`; a past one's Retrosheet box score already ships the
//  postseason line.
//

import Foundation
import Testing
@testable import BaseballStats

private func team(_ id: Int, _ abbr: String) -> BDLTeam {
    BDLTeam(id: id, slug: abbr.lowercased(), abbreviation: abbr, displayName: abbr,
            shortDisplayName: abbr, name: abbr, location: abbr, league: nil, division: nil)
}

private func game(postseason: Bool) -> Game {
    BDLGame(
        id: 15457179, homeTeam: team(19, "NYY"), awayTeam: team(2, "BOS"),
        homeTeamData: nil, awayTeamData: nil, date: "2026-09-30T00:00:00.000Z",
        status: "STATUS_FINAL", venue: nil, period: 9, displayClock: nil, scoringSummary: nil,
        season: 2026, seasonType: postseason ? "postseason" : "regular", postseason: postseason,
        homeTeamName: nil, awayTeamName: nil,
    ).toGame()
}

private func query(_ items: [URLQueryItem]) -> [String: String] {
    Dictionary(uniqueKeysWithValues: items.map { ($0.name, $0.value ?? "") })
}

@Suite("Postseason season totals")
struct PostseasonTotalsTests {

    @Test func aPlayoffGameAsksForThePostseasonCount() {
        let wc = game(postseason: true)
        let items = APIClient.atDateQuery(gameDate: "2026-09-30", postseason: !wc.isRegularSeason)
        #expect(query(items) == ["game_date": "2026-09-30", "postseason": "true"])
    }

    @Test func aRegularSeasonGameAsksAsBefore() {
        // No `postseason` item at all — the request an older server knows.
        let reg = game(postseason: false)
        let items = APIClient.atDateQuery(gameDate: "2026-09-20", postseason: !reg.isRegularSeason)
        #expect(query(items) == ["game_date": "2026-09-20"])
    }

    /// The postseason count decodes like the regular one: Yamamoto through
    /// 2025 World Series Game 7, the server's real answer (2026-10-29).
    @Test func thePostseasonRecordReadsAsAFigure() throws {
        let json = #"{"player_id": 808967, "game_date": "2025-11-01", "wins": 5, "losses": 1, "saves": 0, "wins_unknown": false, "losses_unknown": false, "saves_unknown": false, "includes_today": true, "gamelog_games": 6, "gamelog_rows": 6}"#
        let rec = try APIClient.pitcherRecord(from: Data(json.utf8))
        #expect(rec.figure(for: "W", bump: SeasonType.bump(1, includesToday: rec.includesToday)) == "5-1")
    }
}
