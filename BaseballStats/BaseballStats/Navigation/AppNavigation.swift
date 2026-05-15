//
//  AppNavigation.swift
//  BaseballStats
//
//  App-level coordinator for cross-tab navigation. Owns the selected
//  tab (used as the TabView's selection binding) and a one-shot
//  "pending leaderboard destination" slot that lets one view push a
//  prefilled state into the Leaderboards tab.
//
//  Driving use case: tapping an All-Time Rankings row on the player
//  profile's Career tab should jump the user to the Leaderboards tab
//  with mode=Career, the right player kind, and the tapped stat
//  preselected. LeaderboardsView consumes the destination on appear /
//  on change and clears it so a future tab switch doesn't re-apply
//  a stale jump.
//
//  Inject once at the app root via `.environmentObject(navigation)`
//  on ContentView. Any view in the tree can then access it via
//  `@EnvironmentObject var navigation: AppNavigation`.
//

import Combine
import SwiftUI

@MainActor
final class AppNavigation: ObservableObject {
    /// Tab cases match ContentView's TabView. Tag values are stable so
    /// a future reorder doesn't break deeplinks.
    enum Tab: Int, Hashable {
        case search    = 0
        case scores    = 1
        case standings = 2
        case leaders   = 3
    }

    @Published var selectedTab: Tab = .search
    /// One-shot deeplink slot. Set by callers wanting to push state
    /// into the Leaderboards tab; consumed (and cleared) by
    /// LeaderboardsView on appear / on change. Optional so the
    /// "no pending deeplink" state has a clear sentinel.
    @Published var pendingLeaderboardDestination: LeaderboardDestination?

    struct LeaderboardDestination: Equatable {
        let mode: LeaderboardsViewModel.Mode
        let playerKind: LeaderboardsViewModel.PlayerKind
        let stat: String
    }

    /// Set the destination + flip the active tab in one shot. Both
    /// state mutations are batched into the same render cycle so
    /// LeaderboardsView sees the new tab AND a non-nil destination
    /// when it appears.
    func openLeaderboard(
        mode: LeaderboardsViewModel.Mode,
        playerKind: LeaderboardsViewModel.PlayerKind,
        stat: String
    ) {
        pendingLeaderboardDestination = LeaderboardDestination(
            mode: mode, playerKind: playerKind, stat: stat
        )
        selectedTab = .leaders
    }
}
