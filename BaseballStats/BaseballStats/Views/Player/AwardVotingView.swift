//
//  AwardVotingView.swift
//  BaseballStats
//
//  Sheet presented when the user taps an MVP / Cy Young / Rookie of
//  the Year chiclet on a career-table row. Shows the full voting
//  leaderboard for that (award, year, league) triple — ranked list
//  of players with their points won, points max, and first-place
//  votes. Tapping a row drills into that player's profile.
//
//  Lives next to PlayerProfileView since it's only ever presented
//  from there; the parent owns the @State that drives the sheet.
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
            votingList(response: response)
        } else {
            ContentUnavailableView {
                Label("No voting results", systemImage: "list.number")
            } description: {
                Text("No \(vm.destination.award) voting results recorded for \(vm.destination.league) \(String(vm.destination.year)).")
            }
        }
    }

    private func votingList(response: AwardVotingResponse) -> some View {
        List {
            ForEach(response.entries) { entry in
                ZStack {
                    NavigationLink(value: entry.player) { EmptyView() }
                        .opacity(0)
                    AwardVotingRow(entry: entry)
                }
                .listRowSeparatorTint(Color(.systemGray4))
            }
        }
        .listStyle(.insetGrouped)
        .scrollContentBackground(.hidden)
    }
}

/// Single voting row — rank badge + player name (with team / debut
/// year subtitle) + points-won total + first-place vote count.
/// Visual hierarchy parallels the LeaderboardRow so the two list
/// surfaces feel related.
private struct AwardVotingRow: View {
    let entry: AwardVotingEntry

    var body: some View {
        HStack(spacing: 10) {
            rankBadge
            VStack(alignment: .leading, spacing: 2) {
                Text(entry.player.name)
                    .font(.headline)
                    .lineLimit(1)
                    .minimumScaleFactor(0.75)
                if let subtitle {
                    Text(subtitle)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                Text(pointsWonText)
                    .font(.subheadline.weight(.semibold))
                    .monospacedDigit()
                if let votesFirst = entry.votes_first, votesFirst > 0 {
                    Text("\(votesFirst) 1st-place")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .monospacedDigit()
                }
            }
            Image(systemName: "chevron.right")
                .font(.footnote.weight(.semibold))
                .foregroundStyle(.tertiary)
        }
        .padding(.vertical, 4)
    }

    /// Top-3 ranks get the accent fill (matches the LeagueRankings
    /// card convention); 4+ render as a plain neutral chip.
    private var rankBadge: some View {
        let isTopThree = entry.rank <= 3
        return Text("\(entry.rank)")
            .font(.callout.weight(.bold))
            .monospacedDigit()
            .foregroundStyle(isTopThree ? Color.white : Color.primary)
            .frame(width: 28, height: 28)
            .background(
                Circle().fill(
                    isTopThree
                        ? Color.accentColor
                        : Color(.secondarySystemFill)
                )
            )
    }

    /// "Los Angeles Angels · debut 2011" — uses whichever bio
    /// breadcrumbs the player block carries. Falls back to a single
    /// piece if only one is available, nil if both are missing.
    private var subtitle: String? {
        var pieces: [String] = []
        if let code = entry.player.teamCode, !code.isEmpty,
           let name = teamFullName(for: code) {
            pieces.append(name)
        }
        if let debut = entry.player.mlb_debut {
            pieces.append("debut \(debut)")
        }
        return pieces.isEmpty ? nil : pieces.joined(separator: " · ")
    }

    /// Vote total formatted to one decimal — Lahman's pointsWon
    /// frequently includes half-points from older ballot rules.
    /// "302.0 / 420" style; missing max collapses to bare points.
    private var pointsWonText: String {
        guard let won = entry.points_won else { return "—" }
        let wonStr = String(format: "%g", won)
        if let max = entry.points_max {
            return "\(wonStr) / \(String(format: "%g", max))"
        }
        return wonStr
    }
}
