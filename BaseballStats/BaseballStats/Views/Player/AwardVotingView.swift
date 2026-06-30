//
//  AwardVotingView.swift
//  BaseballStats
//
//  Sheet presented when the user taps an MVP / Cy Young / Rookie of
//  the Year chiclet on a career-table row (and from Team History).
//  Shows the full voting leaderboard for ONE fixed (award, year,
//  league) triple — ranked list of players with their points won,
//  points max, and first-place votes. Tapping a row drills into that
//  player's profile.
//
//  This is the fixed-combo entry point (no filters). The filtered
//  Search experience is `AwardVotingBrowserView`; both share the row
//  and list rendering via `AwardVotingResultsList` / `AwardVotingRow`.
//

import Combine
import SwiftUI

@MainActor
final class AwardVotingViewModel: ObservableObject {
    let destination: AwardVotingDestination
    @Published var response: AwardVotingResponse?
    @Published var isLoading = false
    @Published var error: String?

    private let api: APIClient

    init(destination: AwardVotingDestination, api: APIClient = .shared) {
        self.destination = destination
        self.api = api
    }

    func load() async {
        isLoading = true
        error = nil
        do {
            response = try await api.getAwardVoting(
                award: destination.award,
                year: destination.year,
                league: destination.league
            )
        } catch {
            self.error = error.localizedDescription
            response = nil
        }
        isLoading = false
    }
}

struct AwardVotingView: View {
    @StateObject private var vm: AwardVotingViewModel
    @Environment(\.dismiss) private var dismiss

    init(destination: AwardVotingDestination) {
        _vm = StateObject(wrappedValue: AwardVotingViewModel(destination: destination))
    }

    var body: some View {
        NavigationStack {
            content
                .navigationTitle(navigationTitle)
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button("Done") { dismiss() }
                    }
                }
                .navigationDestination(for: PlayerSearchResult.self) { player in
                    PlayerProfileView(player: player)
                }
        }
        // Glass sheet — matches the app-wide sheet treatment.
        .presentationBackground(.ultraThinMaterial)
        .task { await vm.load() }
    }

    /// "AL MVP · 2023" style — short enough to fit comfortably in
    /// the inline title bar.
    private var navigationTitle: String {
        let award = vm.destination.award
        let lg = vm.destination.league == "ML" ? "" : "\(vm.destination.league) "
        return "\(lg)\(award) · \(vm.destination.year)"
    }

    @ViewBuilder
    private var content: some View {
        if vm.isLoading && vm.response == nil {
            ProgressView()
                .controlSize(.large)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if let error = vm.error, vm.response == nil {
            ContentUnavailableView {
                Label("Couldn't load voting", systemImage: "exclamationmark.triangle")
            } description: {
                Text(error)
            } actions: {
                Button("Try Again") { Task { await vm.load() } }
                    .buttonStyle(.borderedProminent)
            }
        } else if let response = vm.response, !response.entries.isEmpty {
            // Shared list/row rendering — identical to the filtered browser.
            AwardVotingResultsList(entries: response.entries)
        } else {
            ContentUnavailableView {
                Label("No voting results", systemImage: "list.number")
            } description: {
                Text("No \(vm.destination.award) voting results recorded for \(vm.destination.league) \(String(vm.destination.year)).")
            }
        }
    }
}
