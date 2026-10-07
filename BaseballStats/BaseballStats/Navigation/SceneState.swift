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
    /// The Scores tab's state. NOT forwarded: a pushed box score or a changed date
    /// redraws the Scores tab, which observes it, not the whole window.
    let scores = ScoresSceneState()
    /// Ask's state. Forwarded, because the window presents Ask: its one published
    /// value is the presented flag. The conversation and the draft live in its
    /// model, which only the Ask screen observes, so typing never redraws the window.
    let ask = AskSceneState()
    private var forward: [AnyCancellable] = []

    init(navigation: AppNavigation? = nil) {
        // Built here, not as a default argument: a default argument is evaluated
        // off the main actor, and AppNavigation's init is main-actor isolated.
        let navigation = navigation ?? AppNavigation()
        self.navigation = navigation
        forward = [
            navigation.objectWillChange.sink { [weak self] _ in self?.objectWillChange.send() },
            ask.objectWillChange.sink { [weak self] _ in self?.objectWillChange.send() },
        ]
    }
}

/// Ask's state for one window: whether it's presented, and its model — the
/// conversation and the draft. Owned by the window, so dismissing Ask no longer
/// throws the conversation away; "New question" starts fresh.
@MainActor
final class AskSceneState: ObservableObject {
    @Published var presented = false
    /// Built on first use, when Ask is first presented.
    private(set) lazy var model = AskViewModel()
    /// Set once the Ask screen has looked at `@SceneStorage` for this store.
    var restoredFromScene = false
}

/// The Scores tab's state for one window: the day on screen (inside the view
/// model), the navigation path — which carries the open box score — and which
/// finished games' cards are expanded.
@MainActor
final class ScoresSceneState: ObservableObject {
    /// Built on first use, by the Scores tab's first render — when the view's
    /// `@StateObject` used to build it — so the day it starts on is the same.
    private(set) lazy var model = ScoresViewModel()
    @Published var path = NavigationPath()
    /// Expanded final-game cards, by `gamePk`. Cleared when the day changes: a
    /// card used to start collapsed whenever its day was shown again.
    @Published var expandedGames: Set<Int> = []
    /// Set once this store has looked at `@SceneStorage`, so a tab switch (which
    /// re-runs the view's `.task`) never restores over what the user has done since.
    var restoredFromScene = false
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

    /// The Scores day to restore, or nil to keep today: nil unless this run wrote
    /// it, it parses as a `yyyy-MM-dd` day, and the date picker could select it.
    @MainActor
    static func scoresDate(stored: String?, storedLaunch: String?, currentLaunch: String) -> Date? {
        guard let storedLaunch, storedLaunch == currentLaunch,
              let stored, let day = dayFormatter.date(from: stored) else { return nil }
        let start = Calendar.current.startOfDay(for: day)
        return ScoresViewModel.selectableDateRange.contains(start) ? start : nil
    }

    /// The Ask draft to restore, or nil: nil unless this run wrote it and it isn't
    /// blank.
    static func askDraft(stored: String?, storedLaunch: String?, currentLaunch: String) -> String? {
        guard let storedLaunch, storedLaunch == currentLaunch, let stored,
              !stored.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
        return stored
    }

    /// `yyyy-MM-dd` in the device's calendar day — the form the Scores day is stored in.
    static func dayString(_ date: Date) -> String { dayFormatter.string(from: date) }

    /// A navigation path to restore, or nil to keep an empty one: nil unless this
    /// run wrote it and it decodes. A payload that doesn't decode (a type that stopped
    /// being Codable, a format change) restores nothing rather than crashing.
    static func path(stored: Data?, storedLaunch: String?, currentLaunch: String) -> NavigationPath? {
        guard let storedLaunch, storedLaunch == currentLaunch, let stored,
              let rep = try? JSONDecoder().decode(NavigationPath.CodableRepresentation.self, from: stored)
        else { return nil }
        return NavigationPath(rep)
    }

    /// The stored form of `path`, or nil when something on it isn't Codable (then
    /// nothing is stored, and a rebuilt scene opens at the stack's root).
    static func encode(_ path: NavigationPath) -> Data? {
        guard let rep = path.codable else { return nil }
        return try? JSONEncoder().encode(rep)
    }

    private static let dayFormatter: DateFormatter = {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = .current
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()
}
