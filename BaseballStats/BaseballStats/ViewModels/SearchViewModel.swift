//
//  SearchViewModel.swift
//  BaseballStats
//
//  Drives Views/Search/SearchView. Owns the search text and the async
//  fetch lifecycle: debounce typing, cancel in-flight requests when the
//  user types more, and short-circuit queries shorter than 2 characters
//  (the backend rejects them with a 422 anyway).
//

import Combine
import Foundation

@MainActor
final class SearchViewModel: ObservableObject {
    @Published var searchText: String = ""
    @Published var results: [PlayerSearchResult] = []
    @Published var isLoading: Bool = false
    @Published var errorMessage: String? = nil
    /// Curated discover-shelf for the idle landing state. Order is
    /// preserved across the parallel fetch via re-keying by MLBAM id.
    /// Now a FALLBACK — only shown when no heat has been computed yet.
    @Published var activeStars: [PlayerSearchResult] = []

    /// League-wide hot/cold leaders for the idle landing state. Replaces
    /// the static Active Stars shelf with always-fresh form data.
    @Published var heat: HeatLeadersResponse = .empty
    @Published var isLoadingHeat: Bool = false
    private var didLoadHeat = false

    /// True once a heat fetch has succeeded with at least one rated player.
    var hasHeat: Bool { !heat.isEmpty }

    /// MLBAM ids the browse shelf renders, in display order. Hand-
    /// curated so the shelf doesn't drift with stat changes — keeps
    /// the landing screen stable across deploys.
    private static let activeStarIds: [Int] = [
        660271, // Shohei Ohtani
        592450, // Aaron Judge
        660670, // Ronald Acuña Jr.
        665742, // Juan Soto
        605141, // Mookie Betts
        518692, // Freddie Freeman
        545361, // Mike Trout
        677951, // Bobby Witt Jr.
        673357, // Julio Rodríguez
        650333, // Luis Arraez
    ]

    private let api: APIClient
    private var cancellables = Set<AnyCancellable>()
    private var searchTask: Task<Void, Never>?

    init(api: APIClient = .shared) {
        self.api = api

        // Debounce keystrokes so we hit the API only after the user pauses.
        // 0.4s is short enough to feel responsive while letting fast typists
        // skip past intermediate prefixes.
        $searchText
            .debounce(for: .seconds(0.4), scheduler: RunLoop.main)
            .removeDuplicates()
            .sink { [weak self] text in
                self?.handleQueryChange(text)
            }
            .store(in: &cancellables)
    }

    /// Public entry point — kicks off a search using the current `searchText`.
    /// Safe to call repeatedly; in-flight tasks are cancelled by
    /// `handleQueryChange` before this is invoked.
    func search() async {
        let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard query.count >= 2 else { return }

        isLoading = true
        errorMessage = nil

        do {
            let players = try await api.searchPlayers(name: query)
            // The user may have typed more characters while we were waiting;
            // a newer task will have cancelled this one.
            guard !Task.isCancelled else { return }
            results = players
            isLoading = false
        } catch {
            guard !Task.isCancelled else { return }
            errorMessage = error.localizedDescription
            results = []
            isLoading = false
        }
    }

    /// Fan out parallel `/players/by-mlb-id/{id}` fetches for the
    /// curated active-star ids and publish them in the canonical
    /// display order. No-op if already loaded so view-appear retries
    /// don't churn the API. Silently skips any id whose fetch failed
    /// — a missing bio shouldn't blank the whole shelf.
    func loadActiveStars() async {
        guard activeStars.isEmpty else { return }
        let ids = Self.activeStarIds
        let pairs: [(Int, PlayerSearchResult)] = await withTaskGroup(
            of: (Int, PlayerSearchResult)?.self
        ) { group in
            for id in ids {
                group.addTask { [api] in
                    guard let p = try? await api.getPlayerByMlbId(id) else {
                        return nil
                    }
                    return (id, p)
                }
            }
            var hits: [(Int, PlayerSearchResult)] = []
            for await maybe in group {
                if let m = maybe { hits.append(m) }
            }
            return hits
        }
        let byId = Dictionary(uniqueKeysWithValues: pairs)
        activeStars = ids.compactMap { byId[$0] }
    }

    /// Fetch the league-wide hot/cold leaders for the idle landing. Loads
    /// once per VM lifetime (heat only changes nightly); a failed/empty
    /// fetch leaves `heat` empty so the view falls back to Active Stars.
    func loadHeat() async {
        guard !didLoadHeat else { return }
        isLoadingHeat = true
        if let resp = try? await api.getHeatLeaders(limit: 12) {
            heat = resp
            didLoadHeat = true
        }
        isLoadingHeat = false
    }

    /// Resolve a heat-card player_id to the full `PlayerSearchResult` the
    /// profile needs (heat lists carry only a lightweight card shape).
    func resolveHeatPlayer(_ playerId: Int) async -> PlayerSearchResult? {
        (try? await api.getPlayerByMlbId(playerId)) ?? nil
    }

    private func handleQueryChange(_ text: String) {
        searchTask?.cancel()

        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.count >= 2 else {
            // Below the 2-char floor: clear state instead of querying.
            results = []
            errorMessage = nil
            isLoading = false
            return
        }

        searchTask = Task { [weak self] in
            await self?.search()
        }
    }
}
