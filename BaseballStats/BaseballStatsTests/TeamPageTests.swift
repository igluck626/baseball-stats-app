//
//  TeamPageTests.swift
//  BaseballStatsTests
//
//  The team page for every club: the two server reads it is built on, the
//  hero's answer once a season is over, and the link that opens a team's page.
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

/// Serves schedules in order, one per call, the last repeating — and records
/// which seasons were asked for.
private final class FakeSchedule: TeamScheduleProviding, @unchecked Sendable {
    private let responses: [TeamScheduleResponse]
    private(set) var seasons: [Int] = []
    private let lock = NSLock()

    init(_ responses: [TeamScheduleResponse]) { self.responses = responses }

    func getTeamSchedule(bdlTeamId: Int, season: Int, fresh: Bool) async throws -> TeamScheduleResponse {
        lock.withLock {
            seasons.append(season)
            return responses[min(seasons.count - 1, responses.count - 1)]
        }
    }
}

private func noGames(season: Int) -> TeamScheduleResponse {
    TeamScheduleResponse(teamId: 9, season: season, abbreviation: "COL", games: [],
                         seasonResult: SeasonResult(state: .inProgress, roundName: nil, wins: nil,
                                                    losses: nil, opponent: nil),
                         nextSeasonFirstGame: nil)
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

    /// The Rockies' season ended on 09-27, far outside the five-day window the
    /// hero reads: the card used to say "No recent games / Season schedule TBA"
    /// for them, and would have for every club from November.
    @Test func anEliminatedTeamsHeroShowsItsLastGameAndHowTheSeasonEnded() async throws {
        let col = try schedule("team-schedule-col-2026.json")
        let vm = HomeViewModel(schedule: FakeSchedule([col]), finalizeDelays: [0])
        await vm.load(bdlTeamId: 9)
        let last = try #require(col.games.filter { $0.status == "STATUS_FINAL" }
            .max { ($0.startDate ?? .distantPast) < ($1.startDate ?? .distantPast) })
        #expect(vm.lastGame?.gamePk == last.id, "the season's last game, not nothing")
        #expect(vm.seasonResult?.state == .missedPostseason)
        #expect(vm.nextGame == nil, "nothing to play and no 2027 game listed yet")
    }

    @Test func onceTheSeasonIsOverNextSeasonsOpenerIsTheNextGame() async throws {
        var col = try schedule("team-schedule-col-2026.json")
        let opener = try #require(col.games.first { $0.isRegularSeason })
        col = TeamScheduleResponse(teamId: 9, season: 2026, abbreviation: "COL", games: col.games,
                                   seasonResult: col.seasonResult, nextSeasonFirstGame: opener)
        let vm = HomeViewModel(schedule: FakeSchedule([col]), finalizeDelays: [0])
        await vm.load(bdlTeamId: 9)
        #expect(vm.nextGame?.gamePk == opener.id)
    }

    /// Before Opening Day the year's schedule has no finished game: the hero
    /// reads last season's, so January doesn't blank every team's card.
    @Test func beforeOpeningDayTheHeroShowsLastSeasonsEnding() async throws {
        let col = try schedule("team-schedule-col-2026.json")
        let fake = FakeSchedule([noGames(season: 2027), col])
        let vm = HomeViewModel(schedule: fake, finalizeDelays: [0])
        await vm.load(bdlTeamId: 9)
        #expect(fake.seasons.count == 2 && fake.seasons[1] == fake.seasons[0] - 1,
                "asks for this season, then the one before: \(fake.seasons)")
        #expect(vm.seasonResult?.state == .missedPostseason)
        #expect(vm.lastGame != nil)
    }

    @Test func aSeasonWithFinishedGamesDoesNotLookBack() async throws {
        let col = try schedule("team-schedule-col-2026.json")
        let fake = FakeSchedule([col])
        let vm = HomeViewModel(schedule: fake, finalizeDelays: [0])
        await vm.load(bdlTeamId: 9)
        #expect(fake.seasons.count == 1)
    }

    @Test func theSeasonResultReadsAsALine() {
        func line(_ state: SeasonResult.State, _ round: String? = nil, _ w: Int? = nil, _ l: Int? = nil,
                  _ opp: String? = nil) -> String {
            HomeGameUtils.seasonResultLine(SeasonResult(state: state, roundName: round, wins: w,
                                                        losses: l, opponent: opp))
        }
        #expect(line(.missedPostseason) == "Season over · Missed the postseason")
        #expect(line(.eliminated, "NLCS", 0, 4, "MIL") == "Season over · Lost the NLCS 0-4 to MIL")
        #expect(line(.champion, "World Series", 4, 3, "TOR") == "World Series champions · 4-3 over TOR")
        #expect(line(.eliminated, "AL Wild Card", 1, 2, "OAK") == "Season over · Lost the AL Wild Card 1-2 to ATH",
                "balldontlie's OAK reads as the app shows the Athletics everywhere")
        #expect(line(.seasonOver) == "Season over")
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
