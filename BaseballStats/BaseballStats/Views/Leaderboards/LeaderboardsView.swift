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
    /// Cross-tab deeplink coordinator — read here so we can apply
    /// (and clear) a pending leaderboard destination when other tabs
    /// route the user into this one (e.g. "View on the all-time
    /// leaderboard" from the player profile's All-Time Rankings).
    @EnvironmentObject private var navigation: AppNavigation
    /// Secondary filters (league, team, year-range) collapse behind a
    /// toggle by default so the list gets maximum vertical real
    /// estate — the previous always-visible layout left only ~4
    /// player rows. Primary navigation (mode pills, kind toggle,
    /// stat picker) stays pinned at the top regardless.
    @State private var showFilters = false

    /// Hashable identifier for the `.task(id:)` modifier. The VM
    /// owns all the actual filter state and a `commitToken` that
    /// bumps once per debounced commit (200ms for menu changes,
    /// 500ms for slider drags) plus once per "Show more" tap. Rapid
    /// sequential filter changes coalesce into a single token bump
    /// — that's the whole point of routing the FetchKey through one
    /// scalar instead of the previous tuple of every field.
    private struct FetchKey: Hashable {
        let token: Int
    }

    private var fetchKey: FetchKey {
        FetchKey(token: viewModel.commitToken)
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
                    // Year picker only meaningful in season mode —
                    // all-time / career take their scope from the whole
                    // database. Hidden via the if rather than disabled
                    // so the toolbar doesn't carry a stale-looking
                    // greyed control.
                    if viewModel.selectedMode.usesYear {
                        yearMenu
                    }
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
        // Every filter onChange routes through one of two VM helpers:
        //   • filterDidChange — 200ms debounce (menu / segmented taps)
        //   • rangeDidChange  — 500ms debounce (year-range slider)
        // Both clear `error` synchronously, reset pagination, and bump
        // `commitToken` after the wait. The `.task(id:)` above only
        // re-fires when the token actually changes, so rapid taps
        // collapse into a single API call.
        .onChange(of: viewModel.playerKind) { _, _ in
            viewModel.resetStatForCurrentKind()
            viewModel.filterDidChange()
        }
        .onChange(of: viewModel.selectedStat) { _, _ in
            viewModel.filterDidChange()
        }
        .onChange(of: viewModel.selectedYear) { _, _ in
            viewModel.filterDidChange()
        }
        .onChange(of: viewModel.selectedLeague) { _, _ in
            viewModel.resetTeamIfHidden()
            viewModel.filterDidChange()
        }
        .onChange(of: viewModel.selectedTeam) { _, _ in
            viewModel.filterDidChange()
        }
        .onChange(of: viewModel.selectedMode) { _, _ in
            viewModel.filterDidChange()
        }
        .onChange(of: viewModel.selectedYearFrom) { _, _ in
            viewModel.rangeDidChange()
        }
        .onChange(of: viewModel.selectedYearTo) { _, _ in
            viewModel.rangeDidChange()
        }
        // Deeplink hookup — apply any pending destination on first
        // mount and on every subsequent change. .onAppear handles
        // the case where the deeplink was queued *before* this view
        // existed (cold-start path); .onChange handles the live case
        // where the user is already on this tab and another view
        // pushes a fresh destination.
        .onAppear { applyPendingDestination() }
        .onChange(of: navigation.pendingLeaderboardDestination) { _, _ in
            applyPendingDestination()
        }
    }

    /// Consume the one-shot deeplink. Setting `playerKind` triggers
    /// the View's existing onChange handler, which calls
    /// `resetStatForCurrentKind()` and would otherwise overwrite our
    /// desired stat back to the kind's default. Setting `selectedStat`
    /// inside a follow-up Task pushes the write to the next runloop
    /// tick, so it lands after the kind-onChange has fired and reset
    /// the stat — at which point our explicit value sticks.
    private func applyPendingDestination() {
        guard let dest = navigation.pendingLeaderboardDestination else { return }
        // Clear any prior error and force the loading flag up front
        // so the View's `list` branch picks ProgressView over the
        // error card during the gap between this tab-switch and the
        // debounced fetch landing. Without this, a stale "Couldn't
        // Load Leaderboard" can flash for ~200ms (filter debounce)
        // before the new fetch completes.
        viewModel.error = nil
        viewModel.isLoading = true
        viewModel.playerKind = dest.playerKind
        viewModel.selectedMode = dest.mode
        Task { @MainActor in
            viewModel.selectedStat = dest.stat
        }
        navigation.pendingLeaderboardDestination = nil
    }

    // MARK: - Chrome

    private var backgroundGradient: some View {
        LinearGradient(
            colors: [Color(.systemGray6), Color(.systemBackground)],
            startPoint: .top, endPoint: .bottom
        )
        .ignoresSafeArea()
    }

    /// Year picker — current year down to 1900, newest first. The
    /// modern game's record-keeping starts in 1901, but Lahman carries
    /// usable counting-stat rows for 1900 so we extend that far back.
    /// Pre-1900 NL / "deadball-era" seasons exist in the archive but
    /// lack many of the columns (BB rules, sac flies) and would skew
    /// rate leaderboards unfairly — so 1900 is the floor.
    private var yearMenu: some View {
        let years = Array((1900...LeaderboardsViewModel.currentYear).reversed())
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
            // Primary nav stays pinned. League / team / year-range
            // collapse behind the filter toggle in `kindAndStatBar` so
            // the list below has full breathing room by default.
            VStack(spacing: 10) {
                modePicker
                kindAndStatBar
                if showFilters {
                    secondaryFilters
                        // Slide down from the top edge + fade, so
                        // the panel reads as expanding out of the
                        // filter button above it.
                        .transition(.opacity.combined(with: .move(edge: .top)))
                }
            }
            .padding(.horizontal, 16)
            .padding(.top, 12)

            // ...but the list itself sits flush so the inset-grouped
            // card spans nearly the full screen, giving the player
            // name + team line as much room as possible.
            list
        }
    }

    /// Tap-to-expand year-range picker. Collapsed: one-line chip
    /// reading "Year range  1871 – 2026". Expanded: From / To sliders
    /// reveal underneath. All chrome lives inside the component now.
    private var yearRangePicker: some View {
        YearRangeSlider(
            lowerValue: $viewModel.selectedYearFrom,
            upperValue: $viewModel.selectedYearTo,
            bounds: LeaderboardsViewModel.yearRangeBounds
        )
        .padding(.horizontal, 4)
    }

    /// Season / All-Time / Career segmented control sitting above the
    /// kind toggle. Drives both the API request shape (year on/off)
    /// and the toolbar year picker's visibility.
    private var modePicker: some View {
        Picker("Mode", selection: $viewModel.selectedMode) {
            ForEach(LeaderboardsViewModel.Mode.allCases) { mode in
                Text(mode.label).tag(mode)
            }
        }
        .pickerStyle(.segmented)
    }

    /// Top control row — kind (segmented) flexes on the left, stat
    /// menu pill in the middle, filter toggle icon on the right. The
    /// filter toggle reveals / hides the secondary filters panel
    /// below the row.
    private var kindAndStatBar: some View {
        HStack(spacing: 10) {
            Picker("Kind", selection: $viewModel.playerKind) {
                ForEach(LeaderboardsViewModel.PlayerKind.allCases) { kind in
                    Text(kind.label).tag(kind)
                }
            }
            .pickerStyle(.segmented)
            .frame(maxWidth: .infinity)

            statMenu
            filterToggleButton
        }
    }

    /// SF-symbol button that toggles the secondary-filters panel.
    /// Renders filled + accent-tinted when any of league / team /
    /// year-range is off its default, so the user can tell at a
    /// glance that something is narrowing the leaderboard even
    /// while the panel is collapsed.
    private var filterToggleButton: some View {
        let active = hasActiveFilters
        return Button {
            // withAnimation here (rather than .animation(_:value:) on
            // the parent) so the slide is bounded to this toggle's
            // state change and doesn't accidentally animate
            // unrelated re-renders.
            withAnimation(.easeInOut(duration: 0.2)) {
                showFilters.toggle()
            }
        } label: {
            Image(systemName: active
                  ? "line.3.horizontal.decrease.circle.fill"
                  : "line.3.horizontal.decrease.circle")
                .font(.title3)
                .foregroundStyle(active ? Color.accentColor : Color.secondary)
                .symbolRenderingMode(.hierarchical)
        }
        .accessibilityLabel("Filters")
        .accessibilityHint(showFilters ? "Tap to hide filters" : "Tap to show filters")
    }

    /// Collapsible panel — league segmented, team menu, and the
    /// year-range slider (only when the active mode actually uses a
    /// range). Wrapped here so the conditional rendering in `content`
    /// stays one line.
    @ViewBuilder
    private var secondaryFilters: some View {
        VStack(spacing: 10) {
            leaguePicker
            teamPicker
            if !viewModel.selectedMode.usesYear {
                yearRangePicker
            }
        }
    }

    /// True when any of the collapsible filters is off its default.
    /// Drives the toggle button's "active" highlight. Excludes the
    /// year range in Season mode since the slider isn't sent to the
    /// backend in that mode anyway.
    private var hasActiveFilters: Bool {
        if viewModel.selectedLeague != .all { return true }
        if viewModel.selectedTeam.apiCode != nil { return true }
        if !viewModel.selectedMode.usesYear {
            let bounds = LeaderboardsViewModel.yearRangeBounds
            if viewModel.selectedYearFrom != bounds.lowerBound { return true }
            if viewModel.selectedYearTo   != bounds.upperBound { return true }
        }
        return false
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
            .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 10))
        }
    }

    private var statMenu: some View {
        Menu {
            Picker("Stat", selection: $viewModel.selectedStat) {
                ForEach(viewModel.availableStats, id: \.self) { stat in
                    Text(LeaderboardsViewModel.displayName(stat)).tag(stat)
                }
            }
        } label: {
            HStack(spacing: 4) {
                Text(LeaderboardsViewModel.displayName(viewModel.selectedStat))
                    .font(.subheadline.weight(.semibold))
                Image(systemName: "chevron.down")
                    .font(.caption2.weight(.semibold))
            }
            .foregroundStyle(.primary)
            .padding(.horizontal, 12)
            .padding(.vertical, 7)
            .glassEffect(.regular, in: Capsule())
        }
    }

    @ViewBuilder
    private var list: some View {
        if viewModel.isLoading && viewModel.entries.isEmpty {
            ProgressView()
                .controlSize(.large)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if let error = viewModel.error,
                  viewModel.entries.isEmpty,
                  !viewModel.isRangeAdjusting {
            // Don't paint the error UI while the user is mid-drag on
            // the year-range slider — the next debounced fetch is
            // about to land. Showing a transient "Couldn't load…"
            // card during slider interaction reads as a hard failure
            // even though it's just an in-flight request the user
            // is about to supersede anyway.
            errorState(error)
        } else if viewModel.entries.isEmpty,
                  !viewModel.isRangeAdjusting {
            emptyState
        } else if !viewModel.entries.isEmpty {
            resultsList
        }
        // else: dragging in-flight with no cached results yet —
        // render nothing (the slider stays interactive at the top).
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
            Text("No \(LeaderboardsViewModel.displayName(viewModel.selectedStat)) leaders for \(emptyStateScope).")
        }
    }

    /// "2026" in season mode, "all time" / "career" otherwise — keeps
    /// the empty-state copy honest about which window the user is on.
    private var emptyStateScope: String {
        switch viewModel.selectedMode {
        case .season:  return String(viewModel.selectedYear)
        case .allTime: return "all time"
        case .career:  return "career"
        }
    }

    /// Display format for the trailing value cell. Picks the right number
    /// of decimal places per stat. WAR and IP both render one decimal,
    /// but IP uses the grouped variant so career IP reads as
    /// "7,356.0" — career WAR stays plain since it never crosses
    /// 999.
    private var rowFormat: LeaderboardRow.ValueFormat {
        switch viewModel.selectedStat {
        case "AVG", "OBP", "SLG", "OPS": return .threeDecimal
        case "ERA", "WHIP", "FIP":       return .twoDecimal
        case "WAR":                      return .oneDecimal
        case "IP":                       return .oneDecimalGrouped
        default:                         return .integer
        }
    }
}

#Preview {
    LeaderboardsView()
}
