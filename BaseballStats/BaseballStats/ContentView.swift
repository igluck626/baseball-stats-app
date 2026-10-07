//
//  ContentView.swift
//  BaseballStats
//
//  Created by Isaac Gluck on 5/4/26.
//

import SwiftUI

struct ContentView: View {
    /// This window's store (see `SceneState`). Its `navigation` coordinates
    /// cross-tab navigation — owns the TabView's selection binding and the
    /// one-shot "open Leaderboards with this stat" deeplink slot. Injected as an
    /// environment object so any view in the tree (e.g. AllTimeRankingsCard on a
    /// player profile) can push to the Leaderboards tab.
    @StateObject private var scene = SceneState()
    private var navigation: AppNavigation { scene.navigation }
    /// The selected tab, saved with this run's id so a scene rebuilt mid-run (a
    /// fold, Stage Manager) reopens on it. See `SceneRestoration`.
    @SceneStorage("scene.selectedTab") private var storedTab: Int?
    @SceneStorage("scene.launchID") private var storedLaunch: String?
    /// Phase 2: single source of truth for live-game data, root-injected like
    /// `navigation`. Created here so it lives for the app's lifetime; nothing
    /// reads it yet (surfaces migrate onto it in later steps).
    @StateObject private var liveStore = LiveGameStore()
    /// Drives the tab bar's order (and the launch tab). Same shared store
    /// AppNavigation seeds `selectedTab` from, so reordering in Settings
    /// rebuilds the bar here.
    @ObservedObject private var tabOrder = TabOrderStore.shared

    /// App lifecycle, observed once at the root and pushed into `navigation`
    /// so every live-polling loop can gate on it via `navigation.shouldPoll(on:)`.
    @Environment(\.scenePhase) private var scenePhase

    /// Where the Ask button goes. The floating button sat on a leaders column on
    /// iPad and on the live card's inning in phone landscape.
    @Environment(\.horizontalSizeClass) private var horizontalSizeClass
    @Environment(\.verticalSizeClass) private var verticalSizeClass
    /// The trailing safe-area margin — in landscape, the strip beside the
    /// Dynamic Island / notch that content never uses.
    @State private var trailingMargin: CGFloat = 0

    private enum AskPlacement { case floating, sideMargin, accessory }

    private var askPlacement: AskPlacement {
        if verticalSizeClass == .compact {
            // Any phone on its side. The margin holds a 44pt button clear of
            // content; a phone without one (home-button models) uses the
            // accessory instead.
            return trailingMargin >= AskFloatingButton.compactDiameter + 4 ? .sideMargin : .accessory
        }
        // iPad and other wide windows: the tab view's bottom accessory.
        if horizontalSizeClass == .regular { return .accessory }
        // A phone held upright: exactly as before.
        return .floating
    }

    /// Drives the full-screen Ask experience, launched from the floating
    /// button overlaid above the tab bar.
    @State private var showingAsk = false

    var body: some View {
        TabView(selection: Binding(get: { navigation.selectedTab },
                                   set: { navigation.selectedTab = $0 })) {
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
        // ⚠️ VALUES, NOT BRANCHES. Rotating or resizing flips these, and an
        // if/else around the TabView would give it a new identity and throw away
        // every tab's navigation path. A changed modifier value does not.
        //
        // Landscape phone: a 400-point-tall screen can't spare the tab bar, so it
        // shrinks as you scroll down. Upright, the system default is unchanged.
        .tabBarMinimizeBehavior(verticalSizeClass == .compact ? .onScrollDown : .automatic)
        .tabViewBottomAccessory(isEnabled: askPlacement == .accessory) {
            AskAccessoryButton { showingAsk = true }
        }
        // Mirror app lifecycle into the shared coordinator. Backgrounding /
        // going inactive flips `shouldPoll(on:)` false for every tab, which
        // each live loop observes to cancel itself; returning to active
        // re-arms the visible tab's loop with an immediate refresh.
        .onAppear {
            if let tab = SceneRestoration.tab(storedRaw: storedTab, storedLaunch: storedLaunch,
                                              currentLaunch: AppLaunch.id, order: tabOrder.order) {
                navigation.selectedTab = tab
            }
            storedTab = navigation.selectedTab.rawValue
            storedLaunch = AppLaunch.id
        }
        .onChange(of: navigation.selectedTab) { _, tab in
            storedTab = tab.rawValue
            storedLaunch = AppLaunch.id
        }
        .onChange(of: scenePhase) { _, phase in
            navigation.scenePhase = phase
            if phase == .active {
                Task { await SeriesStore.shared.loadCurrentIfPostseason() }
            }
        }
        .environmentObject(navigation)
        .environmentObject(scene)
        .environmentObject(liveStore)
        // Postseason series lines refetch when a game goes final; the store
        // watches the live list for that transition. See `SeriesStore`.
        .task {
            SeriesStore.shared.watch(liveStore)
            await SeriesStore.shared.loadCurrentIfPostseason()
        }
        // Floating Ask entry point, overlaid above the tab bar. The bottom
        // padding lifts it clear of the ~49pt tab bar; trailing inset matches
        // the standard system margin.
        .overlay(alignment: .bottomTrailing) {
            if askPlacement == .floating {
                AskFloatingButton { showingAsk = true }
                    .padding(.trailing, 18)
                    .padding(.bottom, 66)
            }
        }
        // Landscape: centred in the trailing margin, outside the content area.
        .overlay(alignment: .trailing) {
            if askPlacement == .sideMargin {
                AskFloatingButton(diameter: AskFloatingButton.compactDiameter) { showingAsk = true }
                    .frame(width: trailingMargin)
                    .ignoresSafeArea(.container, edges: .trailing)
            }
        }
        .onGeometryChange(for: CGFloat.self) { $0.safeAreaInsets.trailing } action: { trailingMargin = $0 }
        .fullScreenCover(isPresented: $showingAsk) {
            AskView()
        }
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
        case .scores:    ScoresTab()
        case .standings: StandingsView()
        case .leaders:   LeaderboardsView()
        case .search:    SearchView()
        }
    }
}

#Preview {
    ContentView()
}
