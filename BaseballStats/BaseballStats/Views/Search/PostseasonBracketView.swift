//
//  PostseasonBracketView.swift
//  BaseballStats
//
//  Playoff History — a by-year postseason bracket, reached from Search →
//  Baseball History. Phase 2a renders ONLY the modern (1995+) DS → LCS → WS
//  bracket; other eras show a graceful "coming soon" placeholder (the era
//  branch lives in `resolvePostseasonBracket`).
//
//  Reuses the Award Voting toolbar-year pattern: a `.menu` year Picker in the
//  nav bar drives an in-place re-fetch via `.task(id: selectedYear)`.
//

import Combine
import SwiftUI

@MainActor
final class PostseasonBracketViewModel: ObservableObject {
    enum AvailabilityState: Equatable {
        case loading
        case loaded
        case failed(String)
    }
    enum BracketState {
        case loading
        case loaded(PostseasonBracketLayout)
        case failed(String)
    }
    enum ChampionsState {
        case loading
        case loaded([WorldSeriesChampion])
        case failed(String)
    }
    /// Top-level toggle: a single year's bracket vs. the all-years champions list.
    enum Mode: String, CaseIterable, Identifiable {
        case bracket, list
        var id: String { rawValue }
        var label: String { self == .bracket ? "Bracket" : "List" }
    }

    @Published var mode: Mode = .list
    @Published var availabilityState: AvailabilityState = .loading
    @Published private(set) var years: [Int] = []
    @Published var selectedYear: Int = 0
    @Published var bracketState: BracketState = .loading
    @Published var championsState: ChampionsState = .loading

    private let api: APIClient

    init(api: APIClient = .shared) { self.api = api }

    /// Fetch all World Series champions once (List mode).
    func loadChampions() async {
        championsState = .loading
        do {
            let response = try await api.getPostseasonChampions()
            championsState = .loaded(response.champions)
        } catch {
            championsState = .failed(error.localizedDescription)
        }
    }

    /// Tap-through from the champions list: jump to that year's bracket.
    func showBracket(for year: Int) {
        selectedYear = year
        mode = .bracket
    }

    /// Load the year list once; default to the most recent year.
    func loadAvailable() async {
        availabilityState = .loading
        do {
            let response = try await api.getPostseasonAvailable()
            years = response.years
            guard let newest = years.first else {
                availabilityState = .failed("No postseason data is available.")
                return
            }
            selectedYear = newest
            availabilityState = .loaded
        } catch {
            availabilityState = .failed(error.localizedDescription)
        }
    }

    /// Fetch + assemble the bracket for the selected year. Driven by
    /// `.task(id: selectedYear)`, so changing the year re-fetches in place and
    /// cancels any in-flight load.
    func loadBracket() async {
        guard selectedYear != 0 else { return }
        bracketState = .loading
        do {
            let response = try await api.getPostseason(year: selectedYear)
            bracketState = .loaded(
                resolvePostseasonBracket(year: response.year, series: response.series)
            )
        } catch {
            bracketState = .failed(error.localizedDescription)
        }
    }
}

/// Marker value pushed onto Search's navigation path to show the bracket.
struct PostseasonBracketDestination: Hashable {}

struct PostseasonBracketView: View {
    @StateObject private var vm = PostseasonBracketViewModel()

    var body: some View {
        ZStack {
            backgroundGradient
            content
        }
        .navigationTitle("Playoff History")
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(.ultraThinMaterial, for: .navigationBar)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                // Year picker is only meaningful for a single-year bracket — it's
                // hidden in List mode, which spans every year.
                if vm.mode == .bracket, case .loaded = vm.availabilityState {
                    yearMenu
                }
            }
        }
        .task { if vm.years.isEmpty { await vm.loadAvailable() } }
        .task(id: vm.selectedYear) { await vm.loadBracket() }
        // Load the champions list lazily the first time List mode is shown.
        .task(id: vm.mode) {
            if vm.mode == .list, case .loading = vm.championsState {
                await vm.loadChampions()
            }
        }
    }

    private var backgroundGradient: some View {
        LinearGradient(
            colors: [Color(.systemGray6), Color(.systemBackground)],
            startPoint: .top, endPoint: .bottom
        )
        .ignoresSafeArea()
    }

    /// Toolbar year picker — `Picker(.menu).labelsHidden()`, identical to Award
    /// Voting / Leaderboards. Options are the available years, newest first.
    private var yearMenu: some View {
        Picker("Year", selection: $vm.selectedYear) {
            ForEach(vm.years, id: \.self) { year in
                Text(String(year)).tag(year)
            }
        }
        .pickerStyle(.menu)
        .labelsHidden()
    }

    /// Bracket / List toggle — segmented, matching Leaderboards / Award Voting.
    private var modePicker: some View {
        Picker("View", selection: $vm.mode) {
            // List first (left) to match the default-on-open mode.
            ForEach([PostseasonBracketViewModel.Mode.list, .bracket]) { mode in
                Text(mode.label).tag(mode)
            }
        }
        .pickerStyle(.segmented)
    }

    @ViewBuilder
    private var content: some View {
        VStack(spacing: 0) {
            modePicker
                .padding(.horizontal, 16)
                .padding(.top, 12)
                .padding(.bottom, 8)

            switch vm.mode {
            case .bracket: bracketModeContent
            case .list:    championsArea
            }
        }
    }

    /// Single-year bracket (gated on the year list having loaded).
    @ViewBuilder
    private var bracketModeContent: some View {
        switch vm.availabilityState {
        case .loading:
            ProgressView()
                .controlSize(.large)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        case .failed(let message):
            ContentUnavailableView {
                Label("Couldn't Load Playoffs", systemImage: "exclamationmark.triangle")
            } description: {
                Text(message)
            } actions: {
                Button("Retry") { Task { await vm.loadAvailable() } }
                    .buttonStyle(.borderedProminent)
            }
        case .loaded:
            bracketArea
        }
    }

    /// All-years World Series champions list. Tapping a row jumps to that
    /// year's bracket (sets the year + flips the toggle back to Bracket).
    @ViewBuilder
    private var championsArea: some View {
        switch vm.championsState {
        case .loading:
            ProgressView()
                .controlSize(.large)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        case .failed(let message):
            ContentUnavailableView {
                Label("Couldn't Load Champions", systemImage: "exclamationmark.triangle")
            } description: {
                Text(message)
            } actions: {
                Button("Retry") { Task { await vm.loadChampions() } }
                    .buttonStyle(.borderedProminent)
            }
        case .loaded(let champions):
            if champions.isEmpty {
                ContentUnavailableView {
                    Label("No Champions", systemImage: "trophy")
                } description: {
                    Text("No World Series results are available.")
                }
            } else {
                ChampionsListView(champions: champions) { year in
                    vm.showBracket(for: year)
                }
            }
        }
    }

    @ViewBuilder
    private var bracketArea: some View {
        switch vm.bracketState {
        case .loading:
            ProgressView()
                .controlSize(.large)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        case .failed(let message):
            ContentUnavailableView {
                Label("Couldn't Load Bracket", systemImage: "exclamationmark.triangle")
            } description: {
                Text(message)
            } actions: {
                Button("Try Again") { Task { await vm.loadBracket() } }
                    .buttonStyle(.borderedProminent)
            }
        case .loaded(.bracket(let bracket)):
            // Modern (DS→LCS→WS) and LCS-era (LCS→WS) both render here; the
            // layout omits the columns a year doesn't have.
            ModernBracketView(bracket: bracket)
        case .loaded(.classic(let year, let series)):
            ClassicWorldSeriesView(year: year, series: series)
        case .loaded(.list(let year, let series)):
            PostseasonListView(year: year, series: series)
        }
    }
}

// MARK: - Modern bracket

// MARK: Bracket view

/// Playoff History's bracket: Lahman's finished series placed on the shared
/// `BracketTreeLayout`, at the history metrics. Winners are followed forward
/// to connect the rounds (DS → LCS → WS is always provable); a Wild Card box
/// is drawn beside the Division Series its winner reached, and only when that
/// winner is found — never fabricated.
private struct ModernBracketView: View {
    let bracket: ModernBracket

    var body: some View {
        BracketTreeCanvas(layout: Self.layout(bracket)) { item in
            SeriesBox(series: item.cell, isFinal: item.isFinal)
        }
    }

    static func layout(_ b: ModernBracket) -> BracketTreeLayout<PostseasonSeries> {
        let hasWC = !(b.alWildCard.isEmpty && b.nlWildCard.isEmpty)
        let hasDS = !(b.alDivision.isEmpty && b.nlDivision.isEmpty)
        func half(_ division: [PostseasonSeries], _ wildCard: [PostseasonSeries],
                  _ championship: PostseasonSeries) -> BracketHalf<PostseasonSeries> {
            BracketHalf(
                division: division.map { ds in
                    (ds, wildCard.first { PostseasonAdvancement.connects($0, to: ds) })
                },
                championship: championship)
        }
        return .make(al: half(b.alDivision, b.alWildCard, b.alChampionship),
                     nl: half(b.nlDivision, b.nlWildCard, b.nlChampionship),
                     worldSeries: b.worldSeries, hasWC: hasWC, hasDS: hasDS,
                     metrics: .history, id: { $0.id })
    }
}

/// One series box: winner row (emphasized) over loser row (muted), each with a
/// team-color chip + conventional abbreviation + game count. The World Series
/// box gets a trophy badge and an accent border/tint as the bracket's climax.
private struct SeriesBox: View {
    let series: PostseasonSeries
    var isFinal: Bool = false

    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        VStack(spacing: 5) {
            teamRow(code: series.winner, games: series.winnerGames, isWinner: true)
            Divider().opacity(0.4)
            teamRow(code: series.loser, games: series.loserGames, isWinner: false)
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .frame(width: BracketMetrics.history.boxW)
        .background(
            RoundedRectangle(cornerRadius: 11, style: .continuous)
                .fill(isFinal
                      ? AnyShapeStyle(Color.accentColor.opacity(colorScheme == .dark ? 0.18 : 0.10))
                      : AnyShapeStyle(Color(.secondarySystemGroupedBackground)))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 11, style: .continuous)
                .strokeBorder(
                    isFinal ? Color.accentColor.opacity(0.85) : Color(.separator).opacity(0.6),
                    lineWidth: isFinal ? 2 : 1
                )
        )
        .shadow(color: .black.opacity(0.08), radius: 6, y: 2)
    }

    private func teamRow(code: String, games: Int, isWinner: Bool) -> some View {
        HStack(spacing: 6) {
            TeamColorSwatch(code: code, height: 18)
            Text(teamAbbreviation(for: code))
                .font(.subheadline.weight(isWinner ? .bold : .regular))
                .foregroundStyle(isWinner ? .primary : .secondary)
                .lineLimit(1)
            Spacer(minLength: 4)
            Text("\(games)")
                .font(.subheadline.weight(isWinner ? .bold : .regular))
                .monospacedDigit()
                .foregroundStyle(isWinner ? .primary : .secondary)
        }
    }

}

// MARK: - Classic (pre-1969) single World Series

/// Pre-1969: only a World Series existed, so there's no tree — just one
/// prominent WS box (same emphasized styling as the bracket final), centered,
/// under a "{year} World Series" header.
private struct ClassicWorldSeriesView: View {
    let year: Int
    let series: PostseasonSeries

    var body: some View {
        VStack(spacing: 14) {
            VStack(spacing: 4) {
                Image(systemName: "trophy.fill")
                    .font(.title2)
                    .foregroundStyle(.tint)
                Text("\(String(year)) World Series")
                    .font(.headline)
            }
            SeriesBox(series: series, isFinal: true)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(20)
    }
}

// MARK: - List fallback (legacy / odd structures)

/// Years whose structure doesn't fit a bracket tree (1981 split season, pre-1900
/// formats, unmapped round codes): a clean round-by-round vertical list, earliest
/// round first and the World Series last.
private struct PostseasonListView: View {
    let year: Int
    let series: [PostseasonSeries]

    var body: some View {
        if series.isEmpty {
            ContentUnavailableView {
                Label("No Postseason", systemImage: "calendar.badge.exclamationmark")
            } description: {
                Text("No postseason series recorded for \(String(year)).")
            }
        } else {
            List {
                Section("\(String(year)) Postseason") {
                    ForEach(series) { PostseasonListRow(series: $0) }
                }
            }
            .listStyle(.insetGrouped)
            .scrollContentBackground(.hidden)
        }
    }
}

/// One list row: round label, then the matchup (winner emphasized, loser muted)
/// with team-color chips, conventional abbreviations, and the series score.
private struct PostseasonListRow: View {
    let series: PostseasonSeries
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(series.round)
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            HStack(spacing: 10) {
                teamLabel(code: series.winner, games: series.winnerGames, isWinner: true)
                Text("def.")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                teamLabel(code: series.loser, games: series.loserGames, isWinner: false)
                Spacer(minLength: 0)
            }
        }
        .padding(.vertical, 4)
    }

    private func teamLabel(code: String, games: Int, isWinner: Bool) -> some View {
        HStack(spacing: 5) {
            TeamColorSwatch(code: code, height: 16)
            Text(teamAbbreviation(for: code))
                .font(.subheadline.weight(isWinner ? .bold : .regular))
                .foregroundStyle(isWinner ? .primary : .secondary)
            Text("\(games)")
                .font(.subheadline.weight(isWinner ? .bold : .regular))
                .monospacedDigit()
                .foregroundStyle(isWinner ? .primary : .secondary)
        }
    }

}

// MARK: - Champions list (List mode)

/// All-years World Series champions, newest first. Each row taps through to
/// that year's bracket via `onSelect`.
private struct ChampionsListView: View {
    let champions: [WorldSeriesChampion]
    let onSelect: (Int) -> Void

    var body: some View {
        List(champions) { champ in
            Button { onSelect(champ.year) } label: {
                ChampionRow(champion: champ)
            }
            .buttonStyle(.plain)
            .listRowSeparatorTint(Color(.systemGray4))
        }
        .listStyle(.insetGrouped)
        .scrollContentBackground(.hidden)
    }
}

/// "2024 · [chip] LAD def. [chip] NYY · 4–1" — champion bold, runner-up muted.
private struct ChampionRow: View {
    let champion: WorldSeriesChampion
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        HStack(spacing: 10) {
            Text(String(champion.year))
                .font(.subheadline.weight(.semibold))
                .monospacedDigit()
                .foregroundStyle(.secondary)
                .frame(width: 44, alignment: .leading)

            teamLabel(code: champion.winner, isWinner: true)
            Text("def.")
                .font(.caption2)
                .foregroundStyle(.tertiary)
            teamLabel(code: champion.loser, isWinner: false)

            Spacer(minLength: 4)

            Text("\(champion.winnerGames)–\(champion.loserGames)")
                .font(.subheadline.weight(.semibold))
                .monospacedDigit()
            Image(systemName: "chevron.right")
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.tertiary)
        }
        .padding(.vertical, 3)
        .contentShape(Rectangle())
    }

    private func teamLabel(code: String, isWinner: Bool) -> some View {
        HStack(spacing: 5) {
            TeamColorSwatch(code: code, height: 16)
            Text(teamAbbreviation(for: code))
                .font(.subheadline.weight(isWinner ? .bold : .regular))
                .foregroundStyle(isWinner ? .primary : .secondary)
                .lineLimit(1)
        }
    }

}

#Preview {
    NavigationStack {
        PostseasonBracketView()
    }
}
