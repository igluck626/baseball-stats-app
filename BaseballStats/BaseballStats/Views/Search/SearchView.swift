//
//  SearchView.swift
//  BaseballStats
//
//  Root screen: a searchable list of players. The view is intentionally
//  thin — debounce, cancellation, and error handling all live in
//  SearchViewModel.
//

import SwiftUI

struct SearchView: View {
    @StateObject private var viewModel = SearchViewModel()

    var body: some View {
        NavigationStack {
            ZStack {
                backgroundGradient
                content
            }
            .navigationTitle("⚾ BaseballStats")
            .navigationBarTitleDisplayMode(.large)
            .toolbarBackground(.ultraThinMaterial, for: .navigationBar)
            .toolbarBackground(.visible, for: .navigationBar)
            .navigationDestination(for: PlayerSearchResult.self) { player in
                PlayerProfileView(player: player)
            }
            .searchable(
                text: $viewModel.searchText,
                placement: .navigationBarDrawer(displayMode: .always),
                prompt: "Search players"
            )
            .textInputAutocapitalization(.words)
            .autocorrectionDisabled()
        }
    }

    // MARK: - Chrome

    /// Subtle vertical fade — light gray at the top to white at the bottom.
    /// Sits behind the list (which has scrollContentBackground hidden) so
    /// the gradient is visible through the rows.
    private var backgroundGradient: some View {
        LinearGradient(
            colors: [
                Color(.systemGray6),
                Color(.systemBackground),
            ],
            startPoint: .top,
            endPoint: .bottom
        )
        .ignoresSafeArea()
    }

    // MARK: - Content states

    @ViewBuilder
    private var content: some View {
        if viewModel.isLoading && viewModel.results.isEmpty {
            loadingState
        } else if let message = viewModel.errorMessage, viewModel.results.isEmpty {
            errorState(message)
        } else if viewModel.results.isEmpty {
            emptyState
        } else {
            resultsList
        }
    }

    private var loadingState: some View {
        ProgressView()
            .controlSize(.large)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func errorState(_ message: String) -> some View {
        ContentUnavailableView {
            Label("Search failed", systemImage: "exclamationmark.triangle")
        } description: {
            Text(message)
        } actions: {
            Button("Try Again") {
                Task { await viewModel.search() }
            }
            .buttonStyle(.borderedProminent)
        }
    }

    @ViewBuilder
    private var emptyState: some View {
        let trimmed = viewModel.searchText.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.count < 2 {
            ContentUnavailableView {
                Label("Search players", systemImage: "magnifyingglass")
            } description: {
                Text("Type at least 2 characters to find a player.")
            }
        } else {
            ContentUnavailableView.search(text: trimmed)
        }
    }

    private var resultsList: some View {
        List(viewModel.results) { player in
            // Hidden NavigationLink behind the row so we keep the row's
            // custom chevron/look without the system disclosure indicator
            // a visible NavigationLink would add inside a List.
            ZStack {
                NavigationLink(value: player) { EmptyView() }
                    .opacity(0)
                PlayerSearchResultRow(player: player)
            }
            .listRowSeparatorTint(Color(.systemGray4))
        }
        .listStyle(.insetGrouped)
        .scrollContentBackground(.hidden)
    }
}

#Preview {
    SearchView()
}
