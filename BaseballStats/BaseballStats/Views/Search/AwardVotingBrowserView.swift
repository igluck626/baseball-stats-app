//
//  AwardVotingBrowserView.swift
//  BaseballStats
//
//  The integrated Award Voting browser, reached from Search → Baseball History.
//  Filters (award / year / league) pinned at the top; the ranked voting list
//  below, updating in place as filters change. Replaces the earlier
//  picker-then-results split.
//
//  Reuse:
//   • Choices come from GET /awards/available (valid combos only — no
//     hardcoded year ranges or league rules; the ML→AL/NL split is data-driven).
//   • Results come from GET /awards/voting and render through the SHARED
//     `AwardVotingResultsList` / `AwardVotingRow`, so rows (including the
//     adaptive batting/pitching/two-way stat lines) look identical to the
//     fixed-combo `AwardVotingView` used by the career page and Team History.
//

import Combine
import SwiftUI

@MainActor
final class AwardVotingBrowserViewModel: ObservableObject {
    /// Gates the whole screen — without the available combos there's nothing
    /// to filter.
    enum AvailabilityState: Equatable {
        case loading
        case loaded
        case failed(String)
    }
    /// Gates just the list area below the filters.
    enum VotingState {
        case loading
        case loaded(AwardVotingResponse)
        case empty
        case failed(String)
    }

    @Published var availabilityState: AvailabilityState = .loading
    @Published private(set) var awards: [AwardAvailability] = []

    // Always-valid current selection.
    @Published var selectedAward: String = ""
    @Published var selectedYear: Int = 0
    @Published var selectedLeague: String = ""

    @Published var votingState: VotingState = .loading

    private let api: APIClient

    init(api: APIClient = .shared) { self.api = api }

    // MARK: Availability

    func loadAvailability() async {
        availabilityState = .loading
        do {
            let response = try await api.getAwardsAvailable()
            awards = response.awards
            guard !awards.isEmpty else {
                availabilityState = .failed("No award voting data is available.")
                return
            }
            applyDefault()
            availabilityState = .loaded
        } catch {
            availabilityState = .failed(error.localizedDescription)
        }
    }

    // MARK: Derived option lists

    var currentAward: AwardAvailability? {
        awards.first { $0.award == selectedAward }
    }

    /// Years for the selected award — newest first (backend order).
    var years: [Int] {
        currentAward?.entries.map(\.year) ?? []
    }

    /// Leagues with data for the selected (award, year).
    var leagues: [String] {
        currentAward?.entries.first { $0.year == selectedYear }?.leagues ?? []
    }

    /// The settled, always-valid combo. Drives the in-place voting fetch (via
    /// `.task(id:)`), and is re-validated here so it can never be empty.
    var destination: AwardVotingDestination? {
        let valid = leagues
        guard !selectedAward.isEmpty, selectedYear != 0, !valid.isEmpty else { return nil }
        let league = valid.contains(selectedLeague) ? selectedLeague : valid.first!
        return AwardVotingDestination(award: selectedAward, year: selectedYear, league: league)
    }

    // MARK: Validity enforcement

    /// Default: first award (MVP), its newest year, the first league that year.
    private func applyDefault() {
        guard let first = awards.first, let newest = first.entries.first else { return }
        selectedAward = first.award
        selectedYear = newest.year
        selectedLeague = newest.leagues.first ?? ""
    }

    /// Award changed → preserve the other selections where still valid, only
    /// adjusting what the new award makes impossible:
    ///   • Keep the current year if the new award has it; otherwise fall back
    ///     to the new award's newest available year.
    ///   • Then re-validate the league for the (new award, resulting year) and
    ///     only change it if the current one isn't offered for that combo.
    func awardChanged() {
        guard let award = currentAward else { return }

        // Year: keep it if the new award covers it; else its newest.
        if !award.entries.contains(where: { $0.year == selectedYear }) {
            selectedYear = award.entries.first?.year ?? selectedYear
        }

        // League: keep it if valid for the resulting (award, year); else first.
        let valid = leagues
        if !valid.contains(selectedLeague) {
            selectedLeague = valid.first ?? ""
        }
    }

    /// Year changed → keep the award; if the current league isn't offered that
    /// year, switch to one that is (e.g. NL → ML when moving into the pre-split
    /// era). The year itself is never changed here.
    func yearChanged() {
        let valid = leagues
        if !valid.contains(selectedLeague) {
            selectedLeague = valid.first ?? ""
        }
    }

    // MARK: Voting

    /// Fetch the ranked voting for the current combo. Driven by `.task(id:
    /// destination)`, so changing any filter cancels an in-flight fetch and
    /// loads the new combo — the screen never shows results for a stale combo.
    func loadVoting() async {
        guard let dest = destination else { return }
        votingState = .loading
        do {
            if let response = try await api.getAwardVoting(
                award: dest.award, year: dest.year, league: dest.league
            ), !response.entries.isEmpty {
                votingState = .loaded(response)
            } else {
                votingState = .empty
            }
        } catch {
            votingState = .failed(error.localizedDescription)
        }
    }
}

/// Marker value pushed onto Search's navigation path to show this browser.
struct AwardVotingBrowserDestination: Hashable {}

struct AwardVotingBrowserView: View {
    @StateObject private var vm = AwardVotingBrowserViewModel()

    var body: some View {
        // ZStack + gradient + opaque nav-bar material, matching Leaderboards:
        // the filter zone is a fixed header above the list, and rows mask
        // cleanly under the nav bar instead of bleeding into the filter area.
        ZStack {
            backgroundGradient
            content
        }
        .navigationTitle("Award Voting")
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(.ultraThinMaterial, for: .navigationBar)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                // Year lives in the toolbar (like Leaderboards). Shown only once
                // the available years are loaded.
                if case .loaded = vm.availabilityState {
                    yearMenu
                }
            }
        }
        // Availability loads once; selections persist across pushes.
        .task { if vm.awards.isEmpty { await vm.loadAvailability() } }
        // Re-fetch voting whenever the settled combo changes. `.task(id:)`
        // cancels any in-flight fetch, so cascading filter resets resolve
        // to a single load of the final combo. The toolbar year and segmented
        // league both feed this same key via `vm.destination`.
        .task(id: vm.destination) { await vm.loadVoting() }
        .onChange(of: vm.selectedAward) { _, _ in vm.awardChanged() }
        .onChange(of: vm.selectedYear) { _, _ in vm.yearChanged() }
    }

    private var backgroundGradient: some View {
        LinearGradient(
            colors: [Color(.systemGray6), Color(.systemBackground)],
            startPoint: .top, endPoint: .bottom
        )
        .ignoresSafeArea()
    }

    @ViewBuilder
    private var content: some View {
        switch vm.availabilityState {
        case .loading:
            ProgressView()
                .controlSize(.large)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        case .failed(let message):
            ContentUnavailableView {
                Label("Couldn't Load Awards", systemImage: "exclamationmark.triangle")
            } description: {
                Text(message)
            } actions: {
                Button("Retry") { Task { await vm.loadAvailability() } }
                    .buttonStyle(.borderedProminent)
            }
        case .loaded:
            // Fixed filter header, then the list below it (10pt gap) — same
            // layout rhythm as Leaderboards' content.
            VStack(spacing: 10) {
                filterZone
                votingArea
            }
        }
    }

    // MARK: - Filter zone (fixed header)

    /// Two stacked segmented controls — League on top, Award below — matching
    /// the Leaderboards filter-zone vocabulary. Year is in the toolbar.
    private var filterZone: some View {
        VStack(spacing: 10) {
            leagueControl

            Picker("Award", selection: $vm.selectedAward) {
                ForEach(vm.awards) { award in
                    Text(awardLabel(award.award)).tag(award.award)
                }
            }
            .pickerStyle(.segmented)
        }
        .padding(.horizontal, 16)
        .padding(.top, 12)
    }

    /// Year picker in the nav-bar toolbar — `Picker(.menu).labelsHidden()`,
    /// identical to Leaderboards' toolbar year. Options are the selected
    /// award's available years (newest first), unchanged.
    private var yearMenu: some View {
        Picker("Year", selection: $vm.selectedYear) {
            ForEach(vm.years, id: \.self) { year in
                Text(String(year)).tag(year)
            }
        }
        .pickerStyle(.menu)
        .labelsHidden()
    }

    @ViewBuilder
    private var leagueControl: some View {
        // Two leagues → AL / NL segmented (like Leaderboards). A single league
        // (ML, or a lone AL/NL) offers no choice, so a one-segment control
        // would look broken — show a quiet static label instead.
        if vm.leagues.count <= 1 {
            Text(vm.leagues.first ?? vm.selectedLeague)   // "AL" / "NL" / "ML"
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .center)
                .padding(.vertical, 6)
        } else {
            Picker("League", selection: $vm.selectedLeague) {
                ForEach(vm.leagues, id: \.self) { league in
                    Text(league).tag(league)   // already "AL" / "NL" / "ML"
                }
            }
            .pickerStyle(.segmented)
        }
    }

    // MARK: - Voting list area

    @ViewBuilder
    private var votingArea: some View {
        switch vm.votingState {
        case .loading:
            ProgressView()
                .controlSize(.large)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        case .failed(let message):
            ContentUnavailableView {
                Label("Couldn't load voting", systemImage: "exclamationmark.triangle")
            } description: {
                Text(message)
            } actions: {
                Button("Try Again") { Task { await vm.loadVoting() } }
                    .buttonStyle(.borderedProminent)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        case .empty:
            ContentUnavailableView {
                Label("No voting results", systemImage: "list.number")
            } description: {
                Text("No \(vm.selectedAward) voting results recorded for \(vm.selectedLeague) \(String(vm.selectedYear)).")
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        case .loaded(let response):
            // Shared rendering — identical to the fixed-combo AwardVotingView.
            AwardVotingResultsList(entries: response.entries)
        }
    }

    // MARK: - Display helpers (tags stay the exact API strings)

    private func awardLabel(_ award: String) -> String {
        award == "CY Young" ? "Cy Young" : award   // "MVP", "ROY" unchanged
    }
}
