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
