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

    /// Hashable snapshot of every input that should trigger a fetch.
    /// Driving `.task(id:)` off this guarantees one fetch per coherent
    /// state — even when a single user gesture mutates two fields at
    /// once (kind toggle → stat reset + pagination reset). Without
    /// this, the previous chained-onChange approach raced and could
    /// either skip the fetch (when reset to the same stat) or fire
    /// twice (when stat actually changed).
    private struct FetchKey: Hashable {
        let kind:   LeaderboardsViewModel.PlayerKind
        let stat:   String
        let year:   Int
        let league: LeaderboardsViewModel.LeagueFilter
        let team:   LeaderboardsViewModel.TeamFilter
        let limit:  Int
    }

    private var fetchKey: FetchKey {
        FetchKey(
            kind:   viewModel.playerKind,
            stat:   viewModel.selectedStat,
            year:   viewModel.selectedYear,
            league: viewModel.selectedLeague,
            team:   viewModel.selectedTeam,
            limit:  viewModel.displayedLimit
        )
    }

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
        // Single source of truth for fetching — runs once on mount and
        // again whenever any selection field changes. SwiftUI cancels
        // the in-flight fetch automatically when the id changes mid-
        // request, so there's no chance of a stale response landing.
        .task(id: fetchKey) {
            await viewModel.load()
        }
        // The onChange handlers below only mutate side-state (stat
        // default, team validity, page limit). The fetch is the .task
        // above, which reacts to whatever those mutations resolve to.
        .onChange(of: viewModel.playerKind) { _, _ in
            viewModel.resetStatForCurrentKind()
            viewModel.resetPagination()
        }
        .onChange(of: viewModel.selectedStat) { _, _ in
            viewModel.resetPagination()
        }
        .onChange(of: viewModel.selectedYear) { _, _ in
            viewModel.resetPagination()
        }
        .onChange(of: viewModel.selectedLeague) { _, _ in
            viewModel.resetTeamIfHidden()
            viewModel.resetPagination()
        }
        .onChange(of: viewModel.selectedTeam) { _, _ in
            viewModel.resetPagination()
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
            // Just bumps displayedLimit — the .task(id: fetchKey) above
            // sees the new key and fires the load.
            viewModel.loadMore()
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
