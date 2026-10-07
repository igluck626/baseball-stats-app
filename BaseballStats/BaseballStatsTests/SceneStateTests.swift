//
//  SceneStateTests.swift
//
//  The per-scene store, step a: the store forwards its navigation's changes (so
//  the root redraws as it did when it observed `navigation` directly), and the
//  selected tab is restored only within the same run of the app — a scene rebuilt
//  by a fold or Stage Manager reopens on its tab; a cold launch opens on the
//  tab-bar order's first tab, exactly as before.
//
//  Step b, Scores: the day, the path (which carries the open box score) and the
//  expanded cards live in the window's store; the day and the path round-trip
//  through scene storage, gated on the same run.
//
//  Step c, Ask: the presented flag, the conversation and the draft live in the
//  window's store; the draft round-trips through scene storage, gated on the run.
//
//  Step d, Home / Search / Leaders: their paths live in the window's store and
//  round-trip through scene storage (every type they push is Codable); the
//  Leaders filters live in its view model, owned by the store.
//
//  Step e, Standings: its own path in the store, saved like the others, and its
//  league and mode pickers, in memory.
//

import Combine
import Foundation
import SwiftUI
import Testing
@testable import BaseballStats

@MainActor
struct SceneStateTests {
    let order: [AppNavigation.Tab] = [.scores, .search, .standings, .leaders, .home]

    @Test func restoresTheTabWrittenByThisRun() {
        let tab = SceneRestoration.tab(storedRaw: AppNavigation.Tab.standings.rawValue,
                                       storedLaunch: "run-1", currentLaunch: "run-1", order: order)
        #expect(tab == .standings)
    }

    @Test func ignoresATabFromAnEarlierRun() {
        // A cold launch: the saved state belongs to the previous process.
        let tab = SceneRestoration.tab(storedRaw: AppNavigation.Tab.standings.rawValue,
                                       storedLaunch: "run-0", currentLaunch: "run-1", order: order)
        #expect(tab == nil)
    }

    @Test func ignoresMissingOrUnknownState() {
        #expect(SceneRestoration.tab(storedRaw: nil, storedLaunch: "run-1", currentLaunch: "run-1", order: order) == nil)
        #expect(SceneRestoration.tab(storedRaw: 2, storedLaunch: nil, currentLaunch: "run-1", order: order) == nil)
        #expect(SceneRestoration.tab(storedRaw: 99, storedLaunch: "run-1", currentLaunch: "run-1", order: order) == nil)
    }

    @Test func ignoresATabNoLongerInTheBar() {
        let tab = SceneRestoration.tab(storedRaw: AppNavigation.Tab.home.rawValue, storedLaunch: "run-1",
                                       currentLaunch: "run-1", order: [.scores, .search])
        #expect(tab == nil)
    }

    @Test func forwardsNavigationChanges() {
        let scene = SceneState()
        var fired = 0
        let sub = scene.objectWillChange.sink { _ in fired += 1 }
        scene.navigation.selectedTab = .leaders
        scene.navigation.scenePhase = .background
        #expect(fired == 2)
        _ = sub
    }

    @Test func ownsOneNavigationForItsLifetime() {
        let scene = SceneState()
        let first = ObjectIdentifier(scene.navigation)
        scene.navigation.selectedTab = .standings
        #expect(ObjectIdentifier(scene.navigation) == first)
        #expect(scene.navigation.selectedTab == .standings)
    }

    @Test func launchIdIsStableWithinARun() {
        #expect(AppLaunch.id == AppLaunch.id)
        #expect(!AppLaunch.id.isEmpty)
    }

    // MARK: - Scores (step b)

    static func game(_ pk: Int) throws -> Game {
        let json = """
        {"gamePk": \(pk), "gameDate": "2026-10-04T23:08:00Z",
         "status": {"abstractGameState": "Final", "detailedState": "Final"},
         "teams": {"away": {"team": {"id": 147, "name": "New York Yankees"}},
                   "home": {"team": {"id": 139, "name": "Tampa Bay Rays"}}}}
        """
        return try JSONDecoder().decode(Game.self, from: Data(json.utf8))
    }

    static func player() throws -> PlayerSearchResult {
        let json = #"{"player_id": 592450, "name": "Aaron Judge"}"#
        return try JSONDecoder().decode(PlayerSearchResult.self, from: Data(json.utf8))
    }

    @Test func scoresPathRoundTripsWithTheOpenBoxScore() throws {
        var path = NavigationPath()
        path.append(try Self.game(776_001))
        path.append(try Self.player())
        let data = try #require(SceneRestoration.encode(path))
        let back = try #require(SceneRestoration.path(stored: data, storedLaunch: "run-1", currentLaunch: "run-1"))
        // A decoded path stays LAZY until a stack resolves it, so `==` against the
        // eager original is false by construction; compare what it holds instead.
        #expect(back.count == 2)
        #expect(SceneRestoration.encode(back) == data)
        let items = try #require(try JSONSerialization.jsonObject(with: data) as? [String])
        #expect(items.contains("BaseballStats.Game"))
        #expect(items.contains("BaseballStats.PlayerSearchResult"))
        #expect(items.contains { $0.contains("\"gamePk\":776001") })
    }

    @Test func scoresPathFromAnEarlierRunIsIgnored() throws {
        var path = NavigationPath()
        path.append(try Self.game(776_001))
        let data = try #require(SceneRestoration.encode(path))
        #expect(SceneRestoration.path(stored: data, storedLaunch: "run-0", currentLaunch: "run-1") == nil)
        #expect(SceneRestoration.path(stored: data, storedLaunch: nil, currentLaunch: "run-1") == nil)
    }

    @Test func aCorruptPathRestoresNothing() {
        #expect(SceneRestoration.path(stored: Data("not json".utf8), storedLaunch: "run-1", currentLaunch: "run-1") == nil)
        #expect(SceneRestoration.path(stored: nil, storedLaunch: "run-1", currentLaunch: "run-1") == nil)
    }

    @Test func aPathHoldingSomethingNotCodableIsNotStored() {
        struct NotCodable: Hashable {}
        var path = NavigationPath()
        path.append(NotCodable())
        #expect(SceneRestoration.encode(path) == nil)
    }

    @Test func scoresDateRoundTrips() throws {
        let cal = Calendar.current
        let day = try #require(cal.date(from: DateComponents(year: 2026, month: 10, day: 4)))
        let stored = SceneRestoration.dayString(day)
        #expect(stored == "2026-10-04")
        let back = SceneRestoration.scoresDate(stored: stored, storedLaunch: "run-1", currentLaunch: "run-1")
        #expect(back == cal.startOfDay(for: day))
    }

    @Test func scoresDateIsGatedOnTheRunAndTheSelectableRange() {
        #expect(SceneRestoration.scoresDate(stored: "2026-10-04", storedLaunch: "run-0", currentLaunch: "run-1") == nil)
        #expect(SceneRestoration.scoresDate(stored: "2026-13-40", storedLaunch: "run-1", currentLaunch: "run-1") == nil)
        #expect(SceneRestoration.scoresDate(stored: nil, storedLaunch: "run-1", currentLaunch: "run-1") == nil)
        // Before the first game we hold, and after today: the picker can't reach them.
        #expect(SceneRestoration.scoresDate(stored: "1897-06-01", storedLaunch: "run-1", currentLaunch: "run-1") == nil)
        #expect(SceneRestoration.scoresDate(stored: "2999-01-01", storedLaunch: "run-1", currentLaunch: "run-1") == nil)
    }

    @Test func theSceneOwnsOneScoresStateAndViewModel() throws {
        let scene = SceneState()
        let state = ObjectIdentifier(scene.scores)
        let model = ObjectIdentifier(scene.scores.model)
        let day = try #require(Calendar.current.date(from: DateComponents(year: 2026, month: 10, day: 4)))
        scene.scores.model.selectedDate = day
        scene.scores.path.append(try Self.game(776_001))
        scene.scores.expandedGames.insert(776_002)
        #expect(ObjectIdentifier(scene.scores) == state)
        #expect(ObjectIdentifier(scene.scores.model) == model)
        #expect(scene.scores.model.selectedDate == day)
        #expect(scene.scores.path.count == 1)
        #expect(scene.scores.expandedGames == [776_002])
    }

    @Test func scoresChangesDoNotRedrawTheWholeWindow() throws {
        let scene = SceneState()
        var fired = 0
        let sub = scene.objectWillChange.sink { _ in fired += 1 }
        scene.scores.path.append(try Self.game(776_001))
        scene.scores.expandedGames.insert(776_001)
        scene.scores.model.selectedDate = Date(timeIntervalSince1970: 0)
        #expect(fired == 0)
        _ = sub
    }

    @Test func aNewScoresStateStartsOnTodayAtTheRoot() {
        let state = ScoresSceneState()
        #expect(Calendar.current.isDateInToday(state.model.selectedDate))
        #expect(state.path.isEmpty)
        #expect(state.expandedGames.isEmpty)
        #expect(state.restoredFromScene == false)
    }

    // MARK: - Ask (step c)

    @Test func askDraftIsRestoredOnlyFromThisRun() {
        #expect(SceneRestoration.askDraft(stored: "Who has the most saves",
                                          storedLaunch: "run-1", currentLaunch: "run-1") == "Who has the most saves")
        #expect(SceneRestoration.askDraft(stored: "Who has the most saves",
                                          storedLaunch: "run-0", currentLaunch: "run-1") == nil)
        #expect(SceneRestoration.askDraft(stored: "Who", storedLaunch: nil, currentLaunch: "run-1") == nil)
        #expect(SceneRestoration.askDraft(stored: "   ", storedLaunch: "run-1", currentLaunch: "run-1") == nil)
        #expect(SceneRestoration.askDraft(stored: nil, storedLaunch: "run-1", currentLaunch: "run-1") == nil)
    }

    @Test func theSceneOwnsOneAskModelAcrossPresentations() {
        let scene = SceneState()
        let model = ObjectIdentifier(scene.ask.model)
        scene.ask.presented = true
        scene.ask.model.draft = "How many home runs does Aaron Judge have?"
        scene.ask.presented = false          // Done
        scene.ask.presented = true           // reopened
        #expect(ObjectIdentifier(scene.ask.model) == model)
        #expect(scene.ask.model.draft == "How many home runs does Aaron Judge have?")
    }

    @Test func startOverClearsTheConversationAndTheDraft() {
        let model = AskViewModel()
        model.exchanges = [AskExchange(id: UUID(), question: "Who has the most MVPs?", state: .failed("x"))]
        model.draft = "Who has the most"
        model.startOver()
        #expect(model.exchanges.isEmpty)
        #expect(model.draft.isEmpty)
    }

    @Test func presentingAskRedrawsTheWindowButTypingDoesNot() {
        let scene = SceneState()
        var fired = 0
        let sub = scene.objectWillChange.sink { _ in fired += 1 }
        scene.ask.model.draft = "Who has"
        scene.ask.model.draft = "Who has the most"
        #expect(fired == 0)
        scene.ask.presented = true
        #expect(fired == 1)
        _ = sub
    }

    @Test func aNewAskStateStartsClosedAndEmpty() {
        let ask = AskSceneState()
        #expect(ask.presented == false)
        #expect(ask.model.exchanges.isEmpty)
        #expect(ask.model.draft.isEmpty)
        #expect(ask.restoredFromScene == false)
    }

    // MARK: - Home, Search, Leaders (step d)

    @Test func homePathRoundTripsWithTheNewsListAndABoxScore() throws {
        var path = NavigationPath()
        path.append(TeamNewsDestination(scope: .team, lahmanCode: "NYA", teamName: "New York Yankees"))
        path.append(try Self.game(776_003))
        let data = try #require(SceneRestoration.encode(path))
        let back = try #require(SceneRestoration.path(stored: data, storedLaunch: "run-1", currentLaunch: "run-1"))
        #expect(back.count == 2)
        #expect(SceneRestoration.encode(back) == data)
        let items = try #require(try JSONSerialization.jsonObject(with: data) as? [String])
        #expect(items.contains("BaseballStats.TeamNewsDestination"))
        #expect(items.contains { $0.contains("\"lahmanCode\":\"NYA\"") })
    }

    @Test func searchPathRoundTripsWithItsBrowsers() throws {
        var path = NavigationPath()
        path.append(AwardVotingBrowserDestination())
        path.append(try Self.player())
        var bracket = NavigationPath()
        bracket.append(PostseasonBracketDestination())
        for p in [path, bracket] {
            let data = try #require(SceneRestoration.encode(p))
            let back = try #require(SceneRestoration.path(stored: data, storedLaunch: "run-1", currentLaunch: "run-1"))
            #expect(back.count == p.count)
            #expect(SceneRestoration.encode(back) == data)
        }
    }

    @Test func leaguewideNewsDestinationRoundTrips() throws {
        let dest = TeamNewsDestination(scope: .league, lahmanCode: nil, teamName: nil)
        let back = try JSONDecoder().decode(TeamNewsDestination.self, from: JSONEncoder().encode(dest))
        #expect(back == dest)
    }

    @Test func theSceneOwnsOneStackPerTab() throws {
        let scene = SceneState()
        scene.home.path.append(try Self.game(1))
        scene.search.path.append(AwardVotingBrowserDestination())
        scene.leaders.path.append(try Self.player())
        #expect(scene.home.path.count == 1)
        #expect(scene.search.path.count == 1)
        #expect(scene.leaders.path.count == 1)
        #expect(scene.scores.path.isEmpty)
        #expect(ObjectIdentifier(scene.home) != ObjectIdentifier(scene.search))
    }

    @Test func theLeadersFiltersLiveInTheStore() {
        let scene = SceneState()
        let model = ObjectIdentifier(scene.leaders.model)
        scene.leaders.model.selectedMode = .career
        scene.leaders.model.playerKind = .pitcher
        #expect(ObjectIdentifier(scene.leaders.model) == model)
        #expect(scene.leaders.model.selectedMode == .career)
        #expect(scene.leaders.model.playerKind == .pitcher)
    }

    @Test func tabPushesAndFiltersDoNotRedrawTheWholeWindow() throws {
        let scene = SceneState()
        var fired = 0
        let sub = scene.objectWillChange.sink { _ in fired += 1 }
        scene.home.path.append(try Self.game(1))
        scene.search.path.append(PostseasonBracketDestination())
        scene.leaders.path.append(try Self.player())
        scene.leaders.model.selectedMode = .career
        #expect(fired == 0)
        _ = sub
    }

    @Test func newTabStoresStartAtTheRootWithDefaultFilters() {
        let fresh = LeaderboardsViewModel()
        let leaders = LeadersSceneState()
        #expect(leaders.path.isEmpty && !leaders.restoredFromScene)
        #expect(leaders.model.selectedMode == fresh.selectedMode)
        #expect(leaders.model.playerKind == fresh.playerKind)
        #expect(leaders.model.selectedStat == fresh.selectedStat)
        let home = StackSceneState()
        #expect(home.path.isEmpty && !home.restoredFromScene)
    }

    // MARK: - Standings (step e)

    @Test func standingsHasItsOwnStackThatRoundTrips() throws {
        let scene = SceneState()
        scene.standings.path.append(try Self.game(776_004))
        scene.standings.path.append(try Self.player())
        #expect(scene.standings.path.count == 2)
        #expect(scene.home.path.isEmpty && scene.search.path.isEmpty && scene.leaders.path.isEmpty)
        let data = try #require(SceneRestoration.encode(scene.standings.path))
        let back = try #require(SceneRestoration.path(stored: data, storedLaunch: "run-1", currentLaunch: "run-1"))
        #expect(back.count == 2)
        #expect(SceneRestoration.encode(back) == data)
        #expect(SceneRestoration.path(stored: data, storedLaunch: "run-0", currentLaunch: "run-1") == nil)
    }

    @Test func standingsPushesAndPickersDoNotRedrawTheWholeWindow() throws {
        let scene = SceneState()
        var fired = 0
        let sub = scene.objectWillChange.sink { _ in fired += 1 }
        scene.standings.path.append(try Self.game(776_004))
        scene.standings.league = .wc
        scene.standings.mode = .bracket
        #expect(fired == 0)
        _ = sub
    }

    @Test func theStandingsPickersLiveInTheStore() {
        let scene = SceneState()
        let state = ObjectIdentifier(scene.standings)
        #expect(scene.standings.league == .al && scene.standings.mode == .standings)
        scene.standings.league = .nl
        scene.standings.mode = .bracket
        scene.standings.didApplyDefaultMode = true
        scene.standings.didApplyFavoriteLeague = true
        #expect(ObjectIdentifier(scene.standings) == state)
        #expect(scene.standings.league == .nl && scene.standings.mode == .bracket)
        // The one-time defaults are spent, so a rebuilt view won't re-apply them.
        #expect(scene.standings.didApplyDefaultMode && scene.standings.didApplyFavoriteLeague)
    }
}
