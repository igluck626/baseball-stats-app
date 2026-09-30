//
//  PostseasonTotalsTests.swift
//
//  Every season figure the app writes beside a player — "2B: Rice (24)",
//  a Scores final's HR line, a pitcher's "(W 15-6)" — is a REGULAR-season
//  number. On a postseason game it read as the playoff figure, so there it
//  is left off and the name (and, for a pitcher, the decision) stands alone.
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

@Suite("Postseason season totals")
struct PostseasonTotalsTests {

    @Test func aPostseasonGameShowsNoRegularSeasonFigure() {
        let wc = game(postseason: true)
        #expect(SeasonType.regularSeasonFigure("(24)", in: wc) == nil, "2B: Rice, not Rice (24)")
        #expect(SeasonType.regularSeasonFigure("(15-6)", in: wc) == nil)
        #expect(SeasonType.regularSeasonFigure("(—)", in: wc) == nil, "no loading dash either")
    }

    @Test func aRegularSeasonGameIsUnchanged() {
        let reg = game(postseason: false)
        #expect(SeasonType.regularSeasonFigure("(24)", in: reg) == "(24)")
        #expect(SeasonType.regularSeasonFigure("(—)", in: reg) == "(—)")
        #expect(SeasonType.regularSeasonFigure(nil, in: reg) == nil)
    }

    @Test func aPostseasonDecisionKeepsTheLetterAndDropsTheRecord() {
        #expect(SeasonType.decisionOnly(wins: 1, losses: 0, saves: 0) == "(W)")
        #expect(SeasonType.decisionOnly(wins: 0, losses: 1, saves: 0) == "(L)")
        #expect(SeasonType.decisionOnly(wins: 0, losses: 0, saves: 1) == "(SV)")
        #expect(SeasonType.decisionOnly(wins: 0, losses: 0, saves: 0) == nil)
        #expect(SeasonType.decisionOnly(wins: nil, losses: nil, saves: nil) == nil)
    }
}
