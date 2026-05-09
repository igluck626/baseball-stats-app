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
            // Always snap to the kind's headline default — Batting→WAR,
            // Pitching→ERA — even when the previous stat (e.g. WAR)
            // exists in both catalogs. Toggle reads as a fresh start.
            viewModel.resetStatForCurrentKind()
            // The stat reset will fire its own onChange and reload, so
            // we don't need to call load() here too.
        }
        .onChange(of: viewModel.selectedStat) { _, _ in
            viewModel.resetPagination()
            Task { await viewModel.load() }
        }
        .onChange(of: viewModel.selectedYear) { _, _ in
            viewModel.resetPagination()
            Task { await viewModel.load() }
        }
        .onChange(of: viewModel.selectedLeague) { _, _ in
            // If the user had a team selected from the other league,
            // drop back to All Teams before refetching.
            viewModel.resetTeamIfHidden()
            viewModel.resetPagination()
            Task { await viewModel.load() }
        }
        .onChange(of: viewModel.selectedTeam) { _, _ in
            viewModel.resetPagination()
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
            // Controls keep their inset so the segmented and pill
            // chrome reads at a comfortable width...
            VStack(spacing: 10) {
                kindAndStatBar
                leaguePicker
                teamPicker
            }
            .padding(.horizontal, 16)
            .padding(.top, 12)

            // ...but the list itself sits flush so the inset-grouped
            // card spans nearly the full screen, giving the player
            // name + team line as much room as possible.
            list
        }
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

    /// Full-width Menu picker for team selection — defaults to "All
    /// Teams". The list narrows based on the active league filter so
    /// AL + NL-only teams (e.g. Yankees while NL is selected) can't
    /// produce a guaranteed-empty leaderboard.
    private var teamPicker: some View {
        Menu {
            Picker("Team", selection: $viewModel.selectedTeam) {
                ForEach(viewModel.availableTeams) { team in
                    Text(team.displayName).tag(team)
                }
            }
        } label: {
            HStack {
                Text(viewModel.selectedTeam.displayName)
                    .font(.subheadline.weight(.medium))
                    .lineLimit(1)
                Spacer()
                Image(systemName: "chevron.down")
                    .font(.caption2.weight(.semibold))
            }
            .foregroundStyle(.primary)
            .padding(.horizontal, 12)
            .padding(.vertical, 9)
            .background(
                RoundedRectangle(cornerRadius: 10)
                    .fill(Color(.systemGray5))
            )
        }
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
        List {
            ForEach(viewModel.entries) { entry in
                ZStack {
                    NavigationLink(value: entry.player) { EmptyView() }
                        .opacity(0)
                    LeaderboardRow(entry: entry, format: rowFormat)
                }
                .listRowSeparatorTint(Color(.systemGray4))
            }
            if viewModel.canLoadMore {
                showMoreRow
            }
        }
        .listStyle(.insetGrouped)
        .scrollContentBackground(.hidden)
    }

    /// Footer row inside the inset-grouped card. A button styled to
    /// blend with the card chrome (no row chevron, accent color text)
    /// that bumps the displayed limit by one page until it caps at 100.
    /// Hidden via `viewModel.canLoadMore` once the cap is reached or
    /// the dataset is exhausted.
    private var showMoreRow: some View {
        Button {
            Task { await viewModel.loadMore() }
        } label: {
            HStack {
                Spacer()
                if viewModel.isLoadingMore {
                    ProgressView()
                } else {
                    Text("Show more")
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(Color.accentColor)
                }
                Spacer()
            }
            .frame(maxWidth: .infinity)
            .contentShape(Rectangle())
        }
        .disabled(viewModel.isLoadingMore)
        .buttonStyle(.plain)
        .listRowSeparator(.hidden)
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
    /// pitching rate stats, single-decimal for WAR / IP, integer for the
    /// rest.
    private var rowFormat: LeaderboardRow.ValueFormat {
        switch viewModel.selectedStat {
        case "AVG", "OBP", "SLG", "OPS": return .threeDecimal
        case "ERA", "WHIP":              return .twoDecimal
        case "WAR", "IP":                return .oneDecimal
        default:                         return .integer
        }
    }
}

#Preview {
    LeaderboardsView()
}
