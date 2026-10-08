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

    /// Where the Ask entry point goes: a phone on its side (compact height) uses
    /// the trailing margin; everything else, the tab view's bottom accessory.
    @Environment(\.verticalSizeClass) private var verticalSizeClass
    /// The trailing safe-area margin — in landscape, the strip beside the
    /// Dynamic Island / notch that content never uses.
    @State private var trailingMargin: CGFloat = 0

    private enum AskPlacement { case sideMargin, accessory }

    private var askPlacement: AskPlacement {
        if verticalSizeClass == .compact {
            // Any phone on its side. The margin holds a 44pt button clear of
            // content; a phone without one (home-button models) uses the
            // accessory instead.
            return trailingMargin >= AskFloatingButton.compactDiameter + 4 ? .sideMargin : .accessory
        }
        // Everywhere else — iPad, wide windows, and a phone held upright — the
        // tab view's bottom accessory, which follows the bar as it minimizes.
        return .accessory
    }

    /// Drives the full-screen Ask experience, launched from the "Ask a
    /// question" bar in the tab view's bottom accessory (or, on a phone on its
    /// side, the button in the trailing margin). Kept in the window's store,
    /// with the conversation and the draft (see `AskSceneState`).
    private var showingAsk: Binding<Bool> {
        Binding(get: { scene.ask.presented }, set: { scene.ask.presented = $0 })
    }

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
        // The tab bar shrinks as you scroll down, in every orientation, and comes
        // back on scroll-up or a tap.
        .tabBarMinimizeBehavior(.onScrollDown)
        .tabViewBottomAccessory(isEnabled: askPlacement == .accessory) {
            AskAccessoryButton { scene.ask.presented = true }
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
        // Per-player profile choices, for every profile in this window — on a
        // tab's stack, in a sheet, or in Ask (see `ProfileUIStore`).
        .environment(\.profileUIStore, scene.profiles)
        // Postseason series lines refetch when a game goes final; the store
        // watches the live list for that transition. See `SeriesStore`.
        .task {
            SeriesStore.shared.watch(liveStore)
            await SeriesStore.shared.loadCurrentIfPostseason()
        }
        // Landscape: centred in the trailing margin, outside the content area.
        .overlay(alignment: .trailing) {
            if askPlacement == .sideMargin {
                AskFloatingButton(diameter: AskFloatingButton.compactDiameter) { scene.ask.presented = true }
                    .frame(width: trailingMargin)
                    .ignoresSafeArea(.container, edges: .trailing)
            }
        }
        .onGeometryChange(for: CGFloat.self) { $0.safeAreaInsets.trailing } action: { trailingMargin = $0 }
        .fullScreenCover(isPresented: showingAsk) {
            AskView(ask: scene.ask)
                // The cover is presented from outside the `.environment` above,
                // so it doesn't inherit it: profiles opened from Ask need the
                // store handed in here.
                .environment(\.profileUIStore, scene.profiles)
                #if DEBUG
                .sizeClassFlipHarness(id: "harness.flipSizeClass.ask")   // a UI-test hook
                #endif
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
        case .home:      HomeView(stack: scene.home)
        case .scores:    ScoresTab()
        case .standings: StandingsView(stack: scene.standings)
        case .leaders:   LeadersTab()
        case .search:    SearchView(stack: scene.search)
        }
    }
}

#Preview {
    ContentView()
}
