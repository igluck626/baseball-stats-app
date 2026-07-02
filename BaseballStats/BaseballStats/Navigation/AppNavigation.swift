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

extension Notification.Name {
    /// Posted by the Scores tab when a game on the visible day
    /// transitions from Live → Final. The Standings tab subscribes
    /// and re-runs `loadStandings()` so freshly-completed games
    /// show in the W/L columns without waiting for the next tab
    /// switch or the next nightly run.
    static let standingsShouldRefresh = Notification.Name("standingsShouldRefresh")
}

@MainActor
final class AppNavigation: ObservableObject {
    /// Tab cases match ContentView's TabView. Raw values are stable identities
    /// (decoupled from on-screen position) so deeplinks and the persisted tab
    /// order keep working across reorders and app versions.
    enum Tab: Int, Hashable, CaseIterable, Identifiable {
        case scores    = 0
        case search    = 1
        case standings = 2
        case leaders   = 3
        case home      = 4

        var id: Int { rawValue }

        /// On-screen label — matches the original hardcoded `.tabItem` text.
        var title: String {
            switch self {
            case .home:      return "Home"
            case .scores:    return "Scores"
            case .standings: return "Standings"
            case .leaders:   return "Leaders"
            case .search:    return "Search"
            }
        }

        /// SF Symbol — matches the original hardcoded `.tabItem` icon.
        var icon: String {
            switch self {
            case .home:      return "house.fill"
            case .scores:    return "baseball.diamond.bases"
            case .standings: return "list.bullet"
            case .leaders:   return "trophy"
            case .search:    return "magnifyingglass"
            }
        }

        /// Factory-default order — exactly today's visual order. Used when
        /// nothing is saved, and to backfill any missing tabs during reconcile.
        static let defaultOrder: [Tab] = [.home, .scores, .standings, .leaders, .search]

        /// Reconcile a (possibly malformed/stale) saved order into a complete,
        /// valid order: keep known saved cases in their saved order (deduped),
        /// drop unknowns, then append any missing cases in default order.
        /// Always returns exactly all five cases — never fewer, never crashes.
        static func reconciledOrder(from saved: [Tab]) -> [Tab] {
            var result: [Tab] = []
            var seen = Set<Tab>()
            for tab in saved where !seen.contains(tab) {
                result.append(tab)
                seen.insert(tab)
            }
            for tab in defaultOrder where !seen.contains(tab) {
                result.append(tab)
                seen.insert(tab)
            }
            return result
        }

        /// Same reconciliation, starting from persisted raw Int values.
        /// Unknown raw values (e.g. a tab removed in a future version) are
        /// dropped via `compactMap`; nil/empty falls back to the default order.
        static func reconciledOrder(fromRawValues raw: [Int]?) -> [Tab] {
            reconciledOrder(from: (raw ?? []).compactMap(Tab.init(rawValue:)))
        }
    }

    @Published var selectedTab: Tab

    /// App lifecycle phase, mirrored from the root `.onChange(of: scenePhase)`
    /// in ContentView. The single source of truth for "is the app foregrounded"
    /// that the live-polling loops gate on — see `shouldPoll(on:)`.
    @Published var scenePhase: ScenePhase = .active

    /// Whether a live-polling loop owned by `tab` is allowed to run right now:
    /// the app must be active AND `tab` must be the selected one. Pushed detail
    /// views (e.g. a box score) pass the tab they were pushed from as `tab`, so
    /// they pause when the user switches tabs and resume when they switch back.
    func shouldPoll(on tab: Tab) -> Bool {
        scenePhase == .active && selectedTab == tab
    }

    init() {
        // Launch on the FIRST tab in the user's saved order (same source of
        // truth the tab bar uses). Falls back to .home if somehow empty.
        selectedTab = TabOrderStore.shared.order.first ?? .home
    }
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
