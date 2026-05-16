//
//  ContentView.swift
//  BaseballStats
//
//  Created by Isaac Gluck on 5/4/26.
//

import SwiftUI

struct ContentView: View {
    /// Coordinates cross-tab navigation — owns the TabView's selection
    /// binding and the one-shot "open Leaderboards with this stat"
    /// deeplink slot. Injected as an environment object so any view
    /// in the tree (e.g. AllTimeRankingsCard on a player profile)
    /// can push to the Leaderboards tab.
    @StateObject private var navigation = AppNavigation()

    var body: some View {
        TabView(selection: $navigation.selectedTab) {
            ScoresView()
                .tabItem {
                    // `baseball.fill` ships in iOS 15+ and reads as a
                    // baseball at tab-bar resolution. `figure.baseball`
                    // (batter silhouette) is the documented fallback if
                    // we ever drop back to a pre-iOS-15 deployment
                    // target, but the project's deployment target is
                    // well past that floor today.
                    Label("Scores", systemImage: "baseball.fill")
                }
                .tag(AppNavigation.Tab.scores)

            SearchView()
                .tabItem {
                    Label("Search", systemImage: "magnifyingglass")
                }
                .tag(AppNavigation.Tab.search)

            StandingsView()
                .tabItem {
                    Label("Standings", systemImage: "list.bullet")
                }
                .tag(AppNavigation.Tab.standings)

            LeaderboardsView()
                .tabItem {
                    Label("Leaders", systemImage: "trophy")
                }
                .tag(AppNavigation.Tab.leaders)
        }
        .toolbarBackground(.ultraThinMaterial, for: .tabBar)
        .toolbarBackground(.visible, for: .tabBar)
        .environmentObject(navigation)
    }
}

#Preview {
    ContentView()
}
