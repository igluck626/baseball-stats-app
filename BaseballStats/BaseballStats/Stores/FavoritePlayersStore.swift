//
//  FavoritePlayersStore.swift
//  BaseballStats
//
//  UserDefaults-backed list of MLBAM player ids the user wants on
//  their Home tab. Capped at 20 entries — past that the horizontal
//  scroll loses its quick-glance value and the parallel stats fetch
//  starts to feel slow on cold launches.
//
//  Stores only the ids; bio + stats are re-fetched on each load
//  via `HomeViewModel.loadFavoritePlayers(ids:)`. Cheap on repeat
//  loads because the backend stats endpoints are cached.
//

import Combine
import Foundation

@MainActor
final class FavoritePlayersStore: ObservableObject {
    static let shared = FavoritePlayersStore()

    /// User-curated display order of MLBAM ids. Mutations go through
    /// `add(_:)` / `remove(_:)` so the persistence write is centralized.
    @Published private(set) var playerIds: [Int]

    static let maxCount = 20
    private let key = "favoritePlayerIds"

    init() {
        let stored = UserDefaults.standard.array(forKey: key) as? [Int] ?? []
        self.playerIds = Array(stored.prefix(Self.maxCount))
    }

    func add(_ id: Int) {
        // No duplicates; cap at maxCount. A re-add of an existing id
        // is a no-op rather than a bump-to-end so the user's curated
        // order is preserved.
        guard !playerIds.contains(id), playerIds.count < Self.maxCount else { return }
        playerIds.append(id)
        persist()
    }

    func remove(_ id: Int) {
        playerIds.removeAll { $0 == id }
        persist()
    }

    private func persist() {
        UserDefaults.standard.set(playerIds, forKey: key)
    }
}
