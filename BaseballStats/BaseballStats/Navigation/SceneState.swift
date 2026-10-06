//
//  SceneState.swift
//  BaseballStats
//
//  One store per window: the state that must survive a resize, a size-class
//  change, and a foldable moving the app between displays.
//

import Combine
import SwiftUI

/// The per-scene root store. `ContentView` creates one with `@StateObject`, at
/// the top of the window — above any layout that branches on size class — so a
/// Split View resize, a rotation or a fold rebuilds views without rebuilding it.
///
/// ⚠️ WHY IT EXISTS. Every navigation path, the Scores date, the Ask
/// conversation and the profile's tabs and filters live in view-local `@State`.
/// That survives today only because no layout branches on width. The first one
/// that does (a split view on iPad) would throw all of it away on every resize.
/// State moves in here one screen at a time.
///
/// It owns the scene's `AppNavigation` rather than copying its `selectedTab`:
/// the live-polling loops read `navigation.shouldPoll(on:)` and re-evaluate when
/// `navigation` publishes, so the tab has to stay where it publishes. Changes
/// to `navigation` are forwarded, so a view observing the scene redraws exactly
/// when it redrew observing `navigation` directly.
@MainActor
final class SceneState: ObservableObject {
    let navigation: AppNavigation
    private var forward: AnyCancellable?

    init(navigation: AppNavigation? = nil) {
        // Built here, not as a default argument: a default argument is evaluated
        // off the main actor, and AppNavigation's init is main-actor isolated.
        let navigation = navigation ?? AppNavigation()
        self.navigation = navigation
        forward = navigation.objectWillChange.sink { [weak self] _ in
            self?.objectWillChange.send()
        }
    }
}

/// This run of the app. Scene state is stored with it and restored only when it
/// matches — so state survives a scene being rebuilt WITHIN a run (a fold, a
/// window closed and reopened in Stage Manager), while a cold launch opens on the
/// tab-bar order's first tab exactly as it always has.
enum AppLaunch {
    static let id = UUID().uuidString
}

/// The restoration rules, kept pure so they can be tested without a scene.
enum SceneRestoration {
    /// The tab to restore, or nil to keep the launch tab: nil unless the state
    /// was written by THIS run, names a tab that exists, and that tab is still
    /// in the bar.
    static func tab(storedRaw: Int?, storedLaunch: String?, currentLaunch: String,
                    order: [AppNavigation.Tab]) -> AppNavigation.Tab? {
        guard let storedLaunch, storedLaunch == currentLaunch,
              let storedRaw, let tab = AppNavigation.Tab(rawValue: storedRaw),
              order.contains(tab) else { return nil }
        return tab
    }
}
