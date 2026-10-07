//
//  ProfileUIStore.swift
//  BaseballStats
//
//  A player profile's on-screen choices, kept per player by the window, so a
//  resize or a size-class change doesn't put a profile back on its defaults.
//  In memory only.
//

import Combine
import SwiftUI

/// One profile's choices: the tab, the batting/pitching side, the Career
/// table's scope, columns, sorts and selected seasons, and the Game Logs year
/// and scope. Each starts where the profile always started.
@MainActor
final class ProfileUIState: ObservableObject {
    @Published var selectedTab: PlayerProfileScreen.Tab
    /// nil until the user picks a side; the profile then shows its default.
    @Published var selectedRole: PlayerProfileScreen.Role?
    @Published var careerScope: CareerScope = .regular
    @Published var visibleBattingColumns: Set<String> = PlayerProfileScreen.defaultBattingColumns
    @Published var visiblePitchingColumns: Set<String> = PlayerProfileScreen.defaultPitchingColumns
    @Published var battingSort = CareerSort(key: "Year", direction: .descending)
    @Published var pitchingSort = CareerSort(key: "Year", direction: .descending)
    @Published var gameLogYear = Calendar.current.component(.year, from: Date())
    @Published var gameLogScope: GameLogScope = .regular
    @Published var postseasonGameLogYear = Calendar.current.component(.year, from: Date())
    @Published var selectedSeasons: Set<Int> = []

    /// Retired players open on Career, active players on Overview — the
    /// profile's own rule (`PlayerViewModel.isRetired`).
    init(player: PlayerSearchResult) {
        selectedTab = PlayerViewModel(player: player).isRetired ? .career : .overview
    }
}

/// The window's profile choices, keyed by player. Holds the most recently
/// opened `capacity` players; older ones are dropped and start fresh if opened
/// again. Not observable itself — each `ProfileUIState` is.
@MainActor
final class ProfileUIStore {
    let capacity: Int
    private var states: [Int: ProfileUIState] = [:]
    /// Player ids, least recently used first.
    private var order: [Int] = []

    init(capacity: Int = 20) {
        self.capacity = capacity
    }

    /// This player's choices, created on first use.
    func state(for player: PlayerSearchResult) -> ProfileUIState {
        let id = player.player_id
        if let existing = states[id] {
            order.removeAll { $0 == id }
            order.append(id)
            return existing
        }
        let state = ProfileUIState(player: player)
        states[id] = state
        order.append(id)
        while order.count > capacity {
            states[order.removeFirst()] = nil
        }
        return state
    }

    var count: Int { states.count }
    func contains(_ playerId: Int) -> Bool { states[playerId] != nil }
}

private struct ProfileUIStoreKey: EnvironmentKey {
    static let defaultValue: ProfileUIStore? = nil
}

extension EnvironmentValues {
    /// The window's profile choices. An environment VALUE rather than an object,
    /// and optional: a profile shown where none was injected keeps its choices
    /// for its own lifetime instead of crashing (a missing environment object is
    /// a runtime crash, and five of the stacks that show a profile are sheets).
    var profileUIStore: ProfileUIStore? {
        get { self[ProfileUIStoreKey.self] }
        set { self[ProfileUIStoreKey.self] = newValue }
    }
}
