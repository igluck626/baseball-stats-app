//
//  TeamPageTests.swift
//  BaseballStatsTests
//
//  The team page for every club: the two server reads it is built on, and
//  the link that opens a team's page.
//
//  Fixtures are the server's own responses, from the backend run locally
//  against balldontlie and the database on 2026-10-15:
//  `testdata/team-schedule-col-2026.json` (the Rockies — eliminated, 195 games),
//  `team-schedule-lad-2026.json` (the Dodgers, mid-NLCS) and
//  `team-roster-lad.json`.
//

import Foundation
import SwiftUI
import Testing
@testable import BaseballStats

private func fixture(_ name: String) throws -> Data {
    let url = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()   // BaseballStatsTests
        .deletingLastPathComponent()   // BaseballStats
        .deletingLastPathComponent()   // repo root
        .appendingPathComponent("testdata/\(name)")
    return try Data(contentsOf: url)
}

private func schedule(_ name: String) throws -> TeamScheduleResponse {
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase      // as APIClient decodes it
    return try decoder.decode(TeamScheduleResponse.self, from: fixture(name))
}

private func roster() throws -> TeamRosterResponse {
    try JSONDecoder().decode(TeamRosterResponse.self, from: fixture("team-roster-lad.json"))
}

@MainActor
struct TeamScheduleTests {

    @Test func theServersScheduleDecodes() throws {
        let col = try schedule("team-schedule-col-2026.json")
        #expect(col.teamId == 9 && col.abbreviation == "COL")
        #expect(col.games.count == 195)
        #expect(col.seasonResult.state == .missedPostseason && col.seasonResult.isOver)
        let lad = try schedule("team-schedule-lad-2026.json")
        #expect(lad.seasonResult.state == .alive && !lad.seasonResult.isOver)
        #expect(lad.games.contains { $0.seasonType == "postseason" })
    }

    @Test func anUnknownStateDecodesRatherThanFailingTheSchedule() throws {
        let json = #"{"state": "something_new"}"#
        let r = try JSONDecoder().decode(SeasonResult.self, from: Data(json.utf8))
        #expect(r.state == .unknown && !r.isOver)
    }
}

@MainActor
struct TeamRosterTests {

    @Test func theServersRosterDecodes() throws {
        let r = try roster()
        #expect(r.team_id == 14 && r.team_code == "LAN")
        #expect(r.players.count == 45)
        #expect(r.players.contains { $0.scope == "team" } && r.players.contains { $0.scope == "all_teams" })
        #expect(r.injuries.count == 5 && r.injuries.allSatisfy { $0.player != nil })
    }

    @Test func rosterRowsKeepTheLineAndItsScope() throws {
        let rows = HomeViewModel.rosterPlayers(try roster().players)
        #expect(rows.count == 45)
        let traded = rows.filter { $0.scope == "team" && $0.currentStats != nil && $0.currentStats?.war == nil }
        #expect(!traded.isEmpty, "a traded player's line for this team, without WAR")
        #expect(rows.filter(\.isAllTeams).allSatisfy { $0.currentStats != nil })
        #expect(rows.allSatisfy { $0.player_id == ($0.resolved?.player_id ?? 0) })
    }

    @Test func injuriesAreSortedAndResolved() throws {
        let (players, resolved) = HomeViewModel.injuries(try roster().injuries)
        #expect(players.count == 5)
        let order = ["Day-To-Day", "10-Day-IL", "15-Day-IL", "60-Day-IL"]
        let ranks = players.map { order.firstIndex(of: $0.status) ?? 4 }
        #expect(ranks == ranks.sorted(), "least severe first")
        #expect(resolved.count == 5)
    }

    @Test func theSecondaryLineMarksAWholeSeason() {
        func player(_ jersey: String?, _ scope: String?) -> RosterPlayer {
            RosterPlayer(player_id: 1, name: "X", bdl_id: 1, position: "CF", jersey: jersey,
                         resolved: nil, currentStats: nil, scope: scope)
        }
        #expect(RosterSheet.secondaryLine(player("7", "all_teams")) == "#7 · All teams")
        #expect(RosterSheet.secondaryLine(player(nil, "all_teams")) == "All teams")
        #expect(RosterSheet.secondaryLine(player("7", "team")) == "#7")
        #expect(RosterSheet.secondaryLine(player(nil, "team")) == nil)
    }
}

@MainActor
struct TeamLinkTests {

    @Test func aTeamLinkPushesItsPage() {
        var path = NavigationPath()
        let binding = Binding(get: { path }, set: { path = $0 })
        TeamLink.open(14, path: binding, rootTeamId: nil)
        #expect(path.count == 1)
    }

    /// On Home the root is the favourite's page; a link to that team goes back
    /// to it rather than stacking a second copy on top.
    @Test func aLinkToTheRootsOwnTeamGoesBackToIt() {
        var path = NavigationPath()
        let binding = Binding(get: { path }, set: { path = $0 })
        TeamLink.open(14, path: binding, rootTeamId: 19)       // the Dodgers, from the Yankees' Home
        path.append(TeamDestination(bdlTeamId: 26))
        TeamLink.open(19, path: binding, rootTeamId: 19)       // back to the Yankees
        #expect(path.isEmpty)
    }

    @Test func aStackWithATeamPageRoundTrips() throws {
        var path = NavigationPath()
        path.append(TeamDestination(bdlTeamId: 14))
        path.append(TeamNewsDestination(scope: .league, lahmanCode: nil, teamName: nil, tintCode: "LAN"))
        let data = try #require(SceneRestoration.encode(path))
        let back = try #require(SceneRestoration.path(stored: data, storedLaunch: "run-1", currentLaunch: "run-1"))
        #expect(back.count == 2)
        #expect(SceneRestoration.encode(back) == data)
        let items = try #require(try JSONSerialization.jsonObject(with: data) as? [String])
        #expect(items.contains("BaseballStats.TeamDestination"))
        #expect(items.contains { $0.contains("\"bdlTeamId\":14") })
        #expect(items.contains { $0.contains("\"tintCode\":\"LAN\"") })
    }
}
