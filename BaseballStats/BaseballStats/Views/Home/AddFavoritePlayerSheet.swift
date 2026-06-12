//
//  AddFavoritePlayerSheet.swift
//  BaseballStats
//
//  Modal sheet for adding a player to the Home tab's favorites list.
//  Reuses `SearchViewModel` for the debounced search / cancellation /
//  error handling — same flow the Search tab uses. Tapping a row
//  fires `onPick` and dismisses; the parent decides what to do
//  with the picked player (typically: `FavoritePlayersStore.shared.add(...)`).
//

import SwiftUI

struct AddFavoritePlayerSheet: View {
    @StateObject private var vm = SearchViewModel()
    @Environment(\.dismiss) private var dismiss

    var onPick: (PlayerSearchResult) -> Void

    var body: some View {
        NavigationStack {
            content
                .navigationTitle("Add Player")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Cancel") { dismiss() }
                    }
                }
                .searchable(
                    text: $vm.searchText,
                    placement: .navigationBarDrawer(displayMode: .always),
                    prompt: "Search players",
                )
                .textInputAutocapitalization(.words)
                .autocorrectionDisabled()
        }
        // Glass sheet — lets the Home tab's team-color wash read
        // through, matching the app-wide sheet treatment.
        .presentationBackground(.ultraThinMaterial)
    }

    @ViewBuilder
    private var content: some View {
        let trimmed = vm.searchText.trimmingCharacters(in: .whitespacesAndNewlines)
        if vm.isLoading && vm.results.isEmpty {
            ProgressView()
                .controlSize(.large)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if trimmed.count < 2 {
            ContentUnavailableView {
                Label("Search players", systemImage: "magnifyingglass")
            } description: {
                Text("Type at least 2 characters to find a player.")
            }
        } else if vm.results.isEmpty {
            ContentUnavailableView.search(text: trimmed)
        } else {
            List(vm.results) { player in
                Button {
                    onPick(player)
                    dismiss()
                } label: {
                    PlayerSearchResultRow(player: player)
                }
                .buttonStyle(.plain)
                .listRowSeparatorTint(Color(.systemGray4))
            }
            .listStyle(.insetGrouped)
            .scrollContentBackground(.hidden)
        }
    }
}
