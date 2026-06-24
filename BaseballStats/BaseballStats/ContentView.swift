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
    /// Drives the tab bar's order (and the launch tab). Same shared store
    /// AppNavigation seeds `selectedTab` from, so reordering in Settings
    /// rebuilds the bar here.
    @ObservedObject private var tabOrder = TabOrderStore.shared

    var body: some View {
        TabView(selection: $navigation.selectedTab) {
            // Tabs are data-driven: render each tab in the user's saved order.
            // `.tag(tab)` keys selection by identity, so reordering preserves
            // the current selection and the openLeaderboard jump still works.
            ForEach(tabOrder.order) { tab in
                tabContent(tab)
                    .tabItem {
                        Label(tab.title, systemImage: tab.icon)
                    }
                    .tag(tab)
            }
        }
        .toolbarBackground(.ultraThinMaterial, for: .tabBar)
        .toolbarBackground(.visible, for: .tabBar)
        .environmentObject(navigation)
        // User's System/Light/Dark choice, applied over the device appearance.
        // Cascades to every tab and the tab bar.
        .appearanceOverride()
    }

    /// The view for each tab. A @ViewBuilder switch (not AnyView) so SwiftUI
    /// keeps each tab's concrete type and view identity.
    @ViewBuilder
    private func tabContent(_ tab: AppNavigation.Tab) -> some View {
        switch tab {
        case .home:      HomeView()
        case .scores:    ScoresView()
        case .standings: StandingsView()
        case .leaders:   LeaderboardsView()
        case .search:    SearchView()
        }
    }
}

#Preview {
    ContentView()
}
