//
//  LeaderboardsView.swift
//  BaseballStats
//
//  Leaderboards tab — top 25 players for a given (player kind, stat, year).
//  UI: Batting/Pitching segmented control, stat menu, season menu in the
//  toolbar, ranked list below. Tapping a row navigates to the existing
//  PlayerProfileView. Defaults: current year, Batting, WAR.
//

import SwiftUI

struct LeaderboardsView: View {
    @StateObject private var viewModel = LeaderboardsViewModel()

    var body: some View {
        NavigationStack {
            ZStack {
                backgroundGradient
                content
            }
            .navigationTitle("Leaderboards")
            .navigationBarTitleDisplayMode(.large)
            .toolbarBackground(.ultraThinMaterial, for: .navigationBar)
            .navigationDestination(for: PlayerSearchResult.self) { player in
                PlayerProfileView(player: player)
            }
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    yearMenu
                }
            }
        }
        .task { await viewModel.load() }
        .onChange(of: viewModel.playerKind) { _, _ in
            // Switching kinds may invalidate the selected stat (e.g. AVG
            // doesn't exist for pitchers). Reset before re-fetching.
            if !viewModel.statBelongsToCurrentKind() {
                viewModel.resetStatForCurrentKind()
            }
            Task { await viewModel.load() }
        }
        .onChange(of: viewModel.selectedStat) { _, _ in
            Task { await viewModel.load() }
        }
        .onChange(of: viewModel.selectedYear) { _, _ in
            Task { await viewModel.load() }
        }
        .onChange(of: viewModel.selectedLeague) { _, _ in
            Task { await viewModel.load() }
        }
    }

    // MARK: - Chrome

    private var backgroundGradient: some View {
        LinearGradient(
            colors: [Color(.systemGray6), Color(.systemBackground)],
            startPoint: .top, endPoint: .bottom
        )
        .ignoresSafeArea()
    }

    /// Year picker — current year down to 2000, newest first. Mirrors
    /// the Standings tab so the toolbar slot reads the same across tabs.
    private var yearMenu: some View {
        let years = Array((2000...LeaderboardsViewModel.currentYear).reversed())
        return Picker("Year", selection: $viewModel.selectedYear) {
            ForEach(years, id: \.self) { year in
                Text(String(year)).tag(year)
            }
        }
        .pickerStyle(.menu)
        .labelsHidden()
    }

    // MARK: - Content

    @ViewBuilder
    private var content: some View {
        VStack(spacing: 10) {
            kindAndStatBar
            leaguePicker
            list
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
    }

    /// Top control row — kind (segmented) on the left, stat (menu) pill
    /// on the right. Kind is allowed to flex so the stat menu stays at
    /// its intrinsic width on the right edge.
    private var kindAndStatBar: some View {
        HStack(spacing: 12) {
            Picker("Kind", selection: $viewModel.playerKind) {
                ForEach(LeaderboardsViewModel.PlayerKind.allCases) { kind in
                    Text(kind.label).tag(kind)
                }
            }
            .pickerStyle(.segmented)
            .frame(maxWidth: .infinity)

            statMenu
        }
    }

    /// All / AL / NL segmented filter sitting under the kind toggle.
    /// Maps to the backend's optional `league` query param via
    /// `LeagueFilter.apiValue`.
    private var leaguePicker: some View {
        Picker("League", selection: $viewModel.selectedLeague) {
            ForEach(LeaderboardsViewModel.LeagueFilter.allCases) { league in
                Text(league.label).tag(league)
            }
        }
        .pickerStyle(.segmented)
    }

    private var statMenu: some View {
        Menu {
            Picker("Stat", selection: $viewModel.selectedStat) {
                ForEach(viewModel.availableStats, id: \.self) { stat in
                    Text(stat).tag(stat)
                }
            }
        } label: {
            HStack(spacing: 4) {
                Text(viewModel.selectedStat)
                    .font(.subheadline.weight(.semibold))
                Image(systemName: "chevron.down")
                    .font(.caption2.weight(.semibold))
            }
            .foregroundStyle(.primary)
            .padding(.horizontal, 12)
            .padding(.vertical, 7)
            .background(Capsule().fill(Color(.systemGray5)))
        }
    }

    @ViewBuilder
    private var list: some View {
        if viewModel.isLoading && viewModel.entries.isEmpty {
            ProgressView()
                .controlSize(.large)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if let error = viewModel.error, viewModel.entries.isEmpty {
            errorState(error)
        } else if viewModel.entries.isEmpty {
            emptyState
        } else {
            resultsList
        }
    }

    private var resultsList: some View {
        List(viewModel.entries) { entry in
            ZStack {
                NavigationLink(value: entry.player) { EmptyView() }
                    .opacity(0)
                LeaderboardRow(entry: entry, format: rowFormat)
            }
            .listRowSeparatorTint(Color(.systemGray4))
        }
        .listStyle(.insetGrouped)
        .scrollContentBackground(.hidden)
    }

    private func errorState(_ message: String) -> some View {
        ContentUnavailableView {
            Label("Couldn't load leaderboard", systemImage: "exclamationmark.triangle")
        } description: {
            Text(message)
        } actions: {
            Button("Try Again") { Task { await viewModel.load() } }
                .buttonStyle(.borderedProminent)
        }
    }

    private var emptyState: some View {
        ContentUnavailableView {
            Label("No leaders", systemImage: "list.number")
        } description: {
            Text("No \(viewModel.selectedStat) leaders for \(String(viewModel.selectedYear)).")
        }
    }

    /// Display format for the trailing value cell. Picks the right number
    /// of decimal places per stat — three for batting rate stats, two for
    /// pitching rate stats, integer for counting, single-decimal for WAR.
    private var rowFormat: LeaderboardRow.ValueFormat {
        switch viewModel.selectedStat {
        case "AVG", "OPS":           return .threeDecimal
        case "ERA", "WHIP":          return .twoDecimal
        case "WAR":                  return .oneDecimal
        default:                     return .integer
        }
    }
}

#Preview {
    LeaderboardsView()
}
