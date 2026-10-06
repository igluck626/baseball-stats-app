//
//  SceneStateTests.swift
//
//  The per-scene store, step a: the store forwards its navigation's changes (so
//  the root redraws as it did when it observed `navigation` directly), and the
//  selected tab is restored only within the same run of the app — a scene rebuilt
//  by a fold or Stage Manager reopens on its tab; a cold launch opens on the
//  tab-bar order's first tab, exactly as before.
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
}
