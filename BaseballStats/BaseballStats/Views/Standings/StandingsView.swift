//
//  StandingsView.swift
//  BaseballStats
//
//  Standings tab — year picker + AL / NL / WC segmented control + the
//  division cards (E, C, W) for the league tabs or a single wildcard
//  card for WC. Each team row carries the live MLB Stats API fields
//  (streak, L10, home/away, run differential, clinch indicators, magic
//  number) inside a horizontally scrolling tail beyond the fixed
//  Team / W / L columns.
//

import SwiftUI

/// Recomputed standings columns for a team when today's not-yet-
/// official results shift the table — GB / WCGB / PCT plus STRK,
/// home/away splits, and run totals. Matches
/// `TodayRecordAdjustments.recalculateGB`'s value shape (keyed by
/// Lahman team_id). A typealias keeps the 10-field tuple from being
/// re-spelled at every call site.
typealias AdjustedStandingColumns = (
    gb: String, wcgb: String, pct: Double?, strk: String?,
    homeW: Int?, homeL: Int?, awayW: Int?, awayL: Int?,
    runsScored: Int?, runsAllowed: Int?
)

struct StandingsView: View {
    @StateObject private var viewModel = StandingsViewModel()
    @State private var selectedTab: TabSelection = .al

    /// Tab selection: AL division view, NL division view, or the
    /// Wildcard race across both leagues.
    enum TabSelection: String, CaseIterable, Identifiable {
        case al = "AL"
        case nl = "NL"
        case wc = "WC"
        var id: String { rawValue }
        var label: String {
            switch self {
            case .al: return "AL"
            case .nl: return "NL"
            case .wc: return "Wildcard"
            }
        }
    }

    /// Display order for divisions. Lahman uses single-letter codes.
    private static let divisionOrder = ["E", "C", "W"]
    private static let divisionName: [String: String] = [
        "E": "East", "C": "Central", "W": "West",
    ]

    var body: some View {
        NavigationStack {
            ZStack {
                backgroundGradient
                content
            }
            .navigationTitle("Standings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbarBackground(.ultraThinMaterial, for: .navigationBar)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    yearMenu
                }
            }
        }
        .task { await viewModel.loadStandings() }
        .onChange(of: viewModel.selectedYear) { _, _ in
            Task { await viewModel.loadStandings() }
        }
        // The Scores tab posts this when a game it's watching
        // flips Live → Final; re-fetch the current-year slate so
        // W/L columns and division standings reflect the result
        // the moment the user switches over.
        .onReceive(NotificationCenter.default.publisher(for: .standingsShouldRefresh)) { _ in
            Task { await viewModel.loadStandings() }
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

    private var yearMenu: some View {
        // Floor at 1871 — Lahman's first season (National Association,
        // 9 teams). Pre-1969 rows have no division so the current
        // divisional render still shows empty cards back there, but
        // the user asked for the picker to cover whatever the DB
        // actually carries.
        let years = Array((1871...StandingsViewModel.currentYear).reversed())
        return Picker("Year", selection: $viewModel.selectedYear) {
            ForEach(years, id: \.self) { year in
                Text(String(year)).tag(year)
            }
        }
        .pickerStyle(.menu)
        .labelsHidden()
    }

    /// How many wild-card slots existed per league in a given year.
    /// 0 = pre-1994, no wild-card era — tab stays hidden.
    /// 1 = original 1994–2011 single-wild-card format.
    /// 3 = modern expanded format (2012+ — covers the 2 / 3 wild-
    ///     card era variants; close enough for the green-tint
    ///     "in the picture" highlight count).
    private static func wildcardSpots(for year: Int) -> Int {
        switch year {
        case ..<1994:     return 0
        case 1994...2011: return 1
        default:          return 3
        }
    }

    // MARK: - Content

    @ViewBuilder
    private var content: some View {
        if viewModel.isLoading && viewModel.alStandings.isEmpty && viewModel.nlStandings.isEmpty {
            ProgressView()
                .controlSize(.large)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if let error = viewModel.error,
                  viewModel.alStandings.isEmpty && viewModel.nlStandings.isEmpty {
            errorState(error)
        } else if viewModel.alStandings.isEmpty && viewModel.nlStandings.isEmpty {
            emptyState
        } else {
            standingsScroll
        }
    }

    private var standingsScroll: some View {
        ScrollView {
            VStack(spacing: 16) {
                tabPicker
                Group {
                    switch selectedTab {
                    case .al:
                        LeagueTable(
                            buckets: viewModel.alStandings,
                            leagueLabel: "AL",
                            isHistorical: isHistoricalView,
                            adjustments: viewModel.todayAdjustments,
                            adjustedGB: viewModel.adjustedGB,
                            wildcardContenders: viewModel.alWildcard
                        )
                    case .nl:
                        LeagueTable(
                            buckets: viewModel.nlStandings,
                            leagueLabel: "NL",
                            isHistorical: isHistoricalView,
                            adjustments: viewModel.todayAdjustments,
                            adjustedGB: viewModel.adjustedGB,
                            wildcardContenders: viewModel.nlWildcard
                        )
                    case .wc:
                        wildcardContent
                    }
                }
                if let footer = updatedFooter {
                    Text(footer)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .padding(.top, 4)
                }
                if !viewModel.todayAdjustments.isEmpty {
                    Text("† Includes results not yet reflected in official standings")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                        .padding(.bottom, 8)
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 12)
        }
    }

    /// True when the loaded standings carry none of the live-only
    /// fields (L10 / STRK / Home / Away). Historical seasons come
    /// from Lahman, which doesn't ship those columns — only the
    /// nightly MLB Stats API pull populates them, and only for the
    /// current season. When historical, the four columns collapse
    /// out of the table entirely rather than rendering a row of
    /// em-dashes.
    ///
    /// Probe any team — current-season responses populate the four
    /// fields on every team uniformly, and historical responses
    /// leave them null on every team. The first team we can grab
    /// across the league buckets answers the question for the
    /// whole view.
    private var isHistoricalView: Bool {
        let probe = viewModel.alStandings.values.first?.first
                 ?? viewModel.nlStandings.values.first?.first
                 ?? viewModel.alWildcard.first
                 ?? viewModel.nlWildcard.first
        guard let team = probe else { return false }
        return team.last_ten_w == nil
            && team.home_w == nil
            && team.away_w == nil
    }

    private var tabPicker: some View {
        // Wild-card tab hidden in two cases:
        //   • Pre-1994 — the wild-card era hadn't started.
        //   • Modern years where neither league has any WC data
        //     (e.g. early-season cold-start before standings load).
        let hasWildcardData = !viewModel.alWildcard.isEmpty || !viewModel.nlWildcard.isEmpty
        let showWildcard = Self.wildcardSpots(for: viewModel.selectedYear) > 0
            && hasWildcardData
        let available: [TabSelection] = showWildcard
            ? TabSelection.allCases
            : [.al, .nl]
        return Picker("Tab", selection: $selectedTab) {
            ForEach(available) { tab in
                Text(tab.label).tag(tab)
            }
        }
        .pickerStyle(.segmented)
        .onChange(of: showWildcard) { _, show in
            // If the user was on the WC tab and we just dropped it
            // (e.g. they swiped the year picker into the 1980s),
            // bounce back to AL so the picker selection stays valid.
            if !show && selectedTab == .wc { selectedTab = .al }
        }
    }

    @ViewBuilder
    private var wildcardContent: some View {
        let spots = Self.wildcardSpots(for: viewModel.selectedYear)
        VStack(spacing: 16) {
            if !viewModel.alWildcard.isEmpty {
                DivisionCard(
                    title: "AL Wildcard",
                    teams: viewModel.alWildcard,
                    style: .wildcard(spots: spots),
                    isHistorical: isHistoricalView,
                    adjustments: viewModel.todayAdjustments,
                    adjustedGB: viewModel.adjustedGB,
                    wildcardContenders: viewModel.alWildcard
                )
            }
            if !viewModel.nlWildcard.isEmpty {
                DivisionCard(
                    title: "NL Wildcard",
                    teams: viewModel.nlWildcard,
                    style: .wildcard(spots: spots),
                    isHistorical: isHistoricalView,
                    adjustments: viewModel.todayAdjustments,
                    adjustedGB: viewModel.adjustedGB,
                    wildcardContenders: viewModel.nlWildcard
                )
            }
        }
    }

    /// "Updated May 4, 2026" derived from the response's last_updated.
    /// Falls back to nil for older years where the backend doesn't have
    /// a timestamp (Lahman-only data).
    private var updatedFooter: String? {
        guard let iso = viewModel.lastUpdated,
              let date = Self.parseISO(iso) else { return nil }
        let formatter = DateFormatter()
        formatter.dateFormat = "MMMM d, yyyy"
        return "Updated " + formatter.string(from: date)
    }

    private static func parseISO(_ iso: String) -> Date? {
        let withFraction = ISO8601DateFormatter()
        withFraction.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = withFraction.date(from: iso) { return date }
        return ISO8601DateFormatter().date(from: iso)
    }

    // MARK: - Error / empty

    private func errorState(_ message: String) -> some View {
        ContentUnavailableView {
            Label("Couldn't load standings", systemImage: "exclamationmark.triangle")
        } description: {
            Text(message)
        } actions: {
            Button("Try Again") { Task { await viewModel.loadStandings() } }
                .buttonStyle(.borderedProminent)
        }
    }

    private var emptyState: some View {
        ContentUnavailableView {
            Label("No standings", systemImage: "list.bullet.rectangle")
        } description: {
            Text("No team data found for \(String(viewModel.selectedYear)).")
        }
    }
}

// MARK: - Division card

private enum CardStyle: Equatable {
    case division              // Highlight the top row as division leader.
    case wildcard(spots: Int)  // Highlight the top `spots` as wild-card holders.
}

/// One DivisionCard renders one card's worth of standings as a frozen
/// team-identity column on the left plus a horizontally scrolling stat
/// pane on the right. Used by the Wildcard tab, which renders one card
/// per league (AL Wildcard / NL Wildcard) without the AL/NL three-
/// division grouping. Mirrors `LeagueTable`'s frozen-pane pattern so
/// both tabs read consistently — the team name stays put while the
/// stat columns can be swiped to reveal WCGB / L10 / STRK / HOME /
/// AWAY / RDIFF on narrower screens.
private struct DivisionCard: View {
    let title: String
    let teams: [TeamStanding]
    let style: CardStyle
    /// When true, drop the WCGB / L10 / STRK / HOME / AWAY columns —
    /// they're always null for pre-current-season rows (Lahman-
    /// loaded), so rendering em-dashes adds visual noise without
    /// information.
    let isHistorical: Bool
    /// Today's not-yet-official W/L bumps keyed by BDL team id.
    let adjustments: [Int: (wDelta: Int, lDelta: Int)]
    /// Recomputed columns keyed by Lahman team_id (empty → use base).
    let adjustedGB: [String: AdjustedStandingColumns]
    /// This league's non-division-leaders sorted best→worst — the
    /// ranking the WCGB reformatter anchors on.
    let wildcardContenders: [TeamStanding]

    /// Resolve a row's adjustment by bridging its Lahman team code to a
    /// BDL id (TeamStanding carries no BDL id directly).
    private func adjustment(for team: TeamStanding) -> (wDelta: Int, lDelta: Int)? {
        guard let code = team.team_id, let bdlId = lahmanToBDLTeamId[code] else { return nil }
        return adjustments[bdlId]
    }

    private func gbOverride(for team: TeamStanding) -> AdjustedStandingColumns? {
        team.team_id.flatMap { adjustedGB[$0] }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text(title)
                    .font(.headline)
                Spacer()
            }
            .padding(.horizontal, 14)
            .padding(.top, 12)
            .padding(.bottom, 8)

            HStack(spacing: 0) {
                frozenPane
                    .frame(width: StandingsLayout.identityWidth)
                    .background(.ultraThinMaterial)
                    .shadow(color: .black.opacity(0.08), radius: 4, x: 2, y: 0)
                    .zIndex(1)

                ScrollView(.horizontal, showsIndicators: false) {
                    scrollablePane
                }
            }
        }
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 16))
        .shadow(color: .black.opacity(0.06), radius: 8, y: 3)
    }

    private var frozenPane: some View {
        VStack(spacing: 0) {
            FrozenIdentityHeader()
            Divider().opacity(0.4)
            ForEach(Array(teams.enumerated()), id: \.offset) { index, team in
                FrozenIdentityCell(team: team, isLeader: team.rank == 1)
                    .background(rowBackground(
                        style: style, isLeader: index == 0, rankInList: index,
                    ))
                if index != teams.indices.last {
                    Divider().opacity(0.4)
                }
            }
        }
    }

    private var scrollablePane: some View {
        VStack(spacing: 0) {
            ScrollableStatsHeader(isHistorical: isHistorical)
            Divider().opacity(0.4)
            ForEach(Array(teams.enumerated()), id: \.offset) { index, team in
                // GB here is games back from the team's DIVISION leader
                // (the raw backend `games_back`), not from the top of
                // this wildcard list — so leadership is the backend's
                // rank (1 = division leader), never list position, and
                // there is no first-row "—". Rank-1 teams don't appear
                // in wildcard lists, so the check is effectively always
                // false; `leader: nil` skips formatGB's derive-from-
                // leader fallback, which would compute against the
                // wrong team in this card.
                ScrollableStatsRow(
                    team: team,
                    leader: nil,
                    isLeader: team.rank == 1,
                    isHistorical: isHistorical,
                    adjustment: adjustment(for: team),
                    gbOverride: gbOverride(for: team),
                    wildcardContenders: wildcardContenders,
                )
                .background(rowBackground(
                    style: style, isLeader: index == 0, rankInList: index,
                ))
                if index != teams.indices.last {
                    Divider().opacity(0.4)
                }
            }
        }
    }
}

/// Column widths + row heights for the unified standings table.
/// Identity column holds logo + 3-letter abbreviation + optional
/// clinch badge + optional magic-number pill. Stat columns stay
/// tight so the historical 5-column layout fits a single screen
/// without scrolling; the modern 10-column layout overflows and
/// pulls in via horizontal swipe.
private enum StandingsLayout {
    static let identityWidth:     CGFloat = 130
    static let logoSize:          CGFloat = 22
    static let cell:              CGFloat = 38   // W, L
    static let pct:               CGFloat = 46   // ".571"
    static let gb:                CGFloat = 46   // "12.5"
    static let record:            CGFloat = 50   // "54-27"
    static let streak:            CGFloat = 40   // "W4"
    static let diff:              CGFloat = 48   // "+125"
    static let rowHeight:         CGFloat = 40
    static let headerHeight:      CGFloat = 28
    /// Height of the "AL East" / "AL Central" / "AL West" section
    /// label between groups of teams. The frozen pane renders the
    /// label and the scrollable pane renders a same-height empty
    /// spacer so the row alignment stays intact across the
    /// frozen/scrolling boundary.
    static let sectionLabelHeight: CGFloat = 26
    /// Vertical breathing room above each subsequent division
    /// header — clear space, then the 2pt separator line, then the
    /// label. Both panes render the same break sequence so the
    /// separator stretches as one continuous line across the
    /// column boundary.
    static let divisionGapHeight: CGFloat = 14
    static let divisionRuleHeight: CGFloat = 2
}

// MARK: - Row helpers

/// Single-character clinch tag mapping per MLB / bbref convention.
/// Shared between the frozen cell and any future detail surfaces.
private func clinchBadgeFor(_ raw: String?) -> (letter: String, color: Color)? {
    guard let raw = raw?.lowercased(), !raw.isEmpty else { return nil }
    switch raw {
    case "z": return ("z", .yellow)   // clinched home-field advantage
    case "y": return ("y", .green)    // clinched division
    case "x": return ("x", .blue)     // clinched playoff berth
    case "w": return ("w", .teal)     // clinched wild card
    case "e": return ("e", .red)      // eliminated
    default:  return (raw, .secondary)
    }
}

/// Subtle row background. Division view leaves every row plain —
/// the leader is obvious from being first, so no accent fill or
/// star (MLB app convention). Wild-card view tints the top-N rows
/// green to signal "in the playoffs as of right now," where N is
/// the era's slot count (1 for 1994–2011, 3 for 2012+).
private func rowBackground(style: CardStyle, isLeader: Bool, rankInList: Int) -> Color {
    switch style {
    case .division:
        return .clear
    case .wildcard(let spots):
        return rankInList < spots ? Color.green.opacity(0.12) : .clear
    }
}

// MARK: - Formatters

private func formatInt(_ value: Int?) -> String {
    guard let value else { return "—" }
    return String(value)
}

/// ".571" — three-decimal win percentage with the leading "0" stripped.
private func formatPct(_ value: Double?) -> String {
    guard let value else { return "—" }
    let s = String(format: "%.3f", value)
    if s.hasPrefix("0.")  { return String(s.dropFirst()) }
    if s.hasPrefix("-0.") { return "-" + String(s.dropFirst(2)) }
    return s
}

/// Games-back from the division leader: derived on-device for
/// historical years (where the backend doesn't ship `games_back`),
/// otherwise echoes the MLB Stats API's pre-computed string verbatim.
private func formatGB(team: TeamStanding, leader: TeamStanding?, isLeader: Bool) -> String {
    if isLeader { return "—" }
    if let gb = team.games_back, !gb.isEmpty, gb != "-" { return gb }
    guard let lw = leader?.W, let ll = leader?.L,
          let tw = team.W, let tl = team.L else { return "—" }
    let gb = (Double(lw - tw) + Double(tl - ll)) / 2.0
    if gb == 0 { return "—" }
    return String(format: "%.1f", gb)
}

/// Normalize a games-back string for display: absent / "-" → em-dash,
/// and any (possibly signed) numeric value is reformatted to one
/// decimal place so the backend's "2" reads as "2.0" — matching the
/// overlay's formatting. Non-numeric values (already an em-dash, etc.)
/// pass through unchanged.
private func formatGBString(_ value: String?) -> String {
    guard let v = value, !v.isEmpty, v != "-" else { return "—" }
    let hasPlus = v.hasPrefix("+")
    let numeric = hasPlus ? String(v.dropFirst()) : v
    guard let num = Double(numeric) else { return v }
    if num == 0 { return "—" }
    let formatted = String(format: "%.1f", (num * 2).rounded() / 2)
    return hasPlus ? "+\(formatted)" : formatted
}

/// Reformat the backend's MLB-convention WCGB — every team measured
/// from the FIRST wild card ("-" for that leader, "2.5" behind it) —
/// into the display convention the overlay path
/// (`TodayRecordAdjustments.recalculateGB`) uses: anchored on the LAST
/// in-spot (3rd) wild card. Spot-holders above the anchor show their
/// lead over it ("+1.5"), the anchor itself shows "-", and teams below
/// show games back from it. Division leaders (in via their division,
/// absent from `contenders`) show "-", matching the overlay.
///
/// `contenders` is the league's non-division-leaders sorted best→worst
/// — position in that list decides which of the three cases applies.
/// Returns a raw string for `formatGBString` to normalize; falls back
/// to the backend value when the list or its WCGB values can't be
/// resolved.
private func reformatWCGB(team: TeamStanding, contenders: [TeamStanding]) -> String? {
    // Modern wild-card spots. Current season only — the WCGB column is
    // hidden for historical years. Matches the overlay's constant.
    let spots = 3

    // Backend WCGB → games back from the 1st wild card. "-" is the 1st
    // itself (0 back); a "+" prefix (ahead of the 1st — shouldn't occur
    // for non-division-leaders, but tolerated) reads as negative back.
    func backFromFirst(_ s: TeamStanding) -> Double? {
        guard let v = s.wild_card_games_back, !v.isEmpty else { return nil }
        if v == "-" { return 0 }
        if v.hasPrefix("+") { return Double(v.dropFirst()).map { -$0 } }
        return Double(v)
    }

    guard let id = team.team_id,
          let index = contenders.firstIndex(where: { $0.team_id == id }) else {
        // Not in the contender list — a division leader (rank 1) when
        // the list is populated; otherwise (no contender data, e.g.
        // the backend hasn't ranked teams) pass the backend value
        // through untouched.
        return contenders.isEmpty ? team.wild_card_games_back : "-"
    }
    // Everyone holds a spot — no out-team to measure leads against.
    guard contenders.count > spots else { return "-" }
    guard let anchorBack = backFromFirst(contenders[spots - 1]),
          let teamBack = backFromFirst(team) else {
        return team.wild_card_games_back
    }
    if index < spots - 1 {
        let lead = anchorBack - teamBack
        return lead > 0 ? String(format: "+%.1f", lead) : "-"
    } else if index == spots - 1 {
        return "-"
    } else {
        return String(format: "%.1f", teamBack - anchorBack)
    }
}

/// "54-27" formatted record, "—" when either side is nil.
private func formatRecord(w: Int?, l: Int?) -> String {
    guard let w = w, let l = l else { return "—" }
    return "\(w)-\(l)"
}

/// Signed run differential — "+77" / "−12" / "0" / "—".
private func formatDiff(_ value: Int?) -> String {
    guard let value else { return "—" }
    if value == 0 { return "0" }
    return value > 0 ? "+\(value)" : "\(value)"
}

// MARK: - LeagueTable (AL / NL combined-divisions layout)

/// Combined three-division table for an AL or NL tab. Single glass
/// card wrapping a frozen team-identity column on the left and one
/// horizontally scrolling stat pane on the right that covers all
/// three divisions at once. Division section labels live on the
/// frozen side as lightweight separators between groups of teams;
/// the scrollable side renders an equal-height spacer at each
/// section boundary so rows stay aligned across the boundary.
///
/// Mirrors the frozen-pane pattern used by the Game Logs monthly
/// view — the row structures on the two sides must match index-
/// for-index so heights line up across the column gap.
private struct LeagueTable: View {
    let buckets: [String: [TeamStanding]]
    let leagueLabel: String
    let isHistorical: Bool
    /// Today's not-yet-official W/L bumps keyed by BDL team id.
    let adjustments: [Int: (wDelta: Int, lDelta: Int)]
    /// Recomputed columns keyed by Lahman team_id (empty → use base).
    let adjustedGB: [String: AdjustedStandingColumns]
    /// This league's non-division-leaders sorted best→worst — the
    /// ranking the WCGB reformatter anchors on.
    let wildcardContenders: [TeamStanding]

    /// Resolve a row's adjustment by bridging its Lahman team code to a
    /// BDL id (TeamStanding carries no BDL id directly).
    private func adjustment(for team: TeamStanding) -> (wDelta: Int, lDelta: Int)? {
        guard let code = team.team_id, let bdlId = lahmanToBDLTeamId[code] else { return nil }
        return adjustments[bdlId]
    }

    private func gbOverride(for team: TeamStanding) -> AdjustedStandingColumns? {
        team.team_id.flatMap { adjustedGB[$0] }
    }

    private static let divisionOrder = ["E", "C", "W"]
    private static let divisionName: [String: String] = [
        "E": "East", "C": "Central", "W": "West",
    ]

    /// Only divisions that actually carry teams in the response, in
    /// canonical E / C / W order. Pre-divisional years produce zero
    /// groups (the table renders just the headers + an empty body,
    /// matching the existing behavior).
    private var divisions: [(code: String, teams: [TeamStanding])] {
        Self.divisionOrder.compactMap { d in
            guard let teams = buckets[d], !teams.isEmpty else { return nil }
            return (d, teams)
        }
    }

    var body: some View {
        HStack(spacing: 0) {
            frozenPane
                .frame(width: StandingsLayout.identityWidth)
                .background(.ultraThinMaterial)
                .shadow(color: .black.opacity(0.08), radius: 4, x: 2, y: 0)
                .zIndex(1)

            ScrollView(.horizontal, showsIndicators: false) {
                scrollablePane
            }
        }
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 16))
        .shadow(color: .black.opacity(0.06), radius: 8, y: 3)
    }

    // MARK: Frozen + scrollable panes

    private var frozenPane: some View {
        VStack(spacing: 0) {
            ForEach(Array(divisions.enumerated()), id: \.element.code) { idx, group in
                if idx > 0 {
                    DivisionBreak()
                }
                DivisionSectionLabel(
                    text: "\(leagueLabel) \(Self.divisionName[group.code] ?? group.code)"
                )
                // Per-division header row. Repeated above every group
                // (rather than once at the top of the league) so the
                // column labels stay visible as the user scrolls
                // through NL East → NL Central → NL West.
                FrozenIdentityHeader()
                Divider().opacity(0.4)
                ForEach(Array(group.teams.enumerated()), id: \.offset) { tIdx, team in
                    FrozenIdentityCell(team: team, isLeader: team.rank == 1)
                    if tIdx != group.teams.indices.last {
                        Divider().opacity(0.25)
                    }
                }
            }
        }
    }

    private var scrollablePane: some View {
        VStack(spacing: 0) {
            ForEach(Array(divisions.enumerated()), id: \.element.code) { idx, group in
                if idx > 0 {
                    DivisionBreak()
                }
                DivisionSectionSpacer()
                // Mirror of `FrozenIdentityHeader` on the scrolling
                // side — must follow the spacer (which pairs with
                // the frozen pane's `DivisionSectionLabel`) so the
                // two panes' rows stay aligned at every Y position.
                ScrollableStatsHeader(isHistorical: isHistorical)
                Divider().opacity(0.4)
                ForEach(Array(group.teams.enumerated()), id: \.offset) { tIdx, team in
                    ScrollableStatsRow(
                        team: team,
                        leader: group.teams.first,
                        isLeader: team.rank == 1,
                        isHistorical: isHistorical,
                        adjustment: adjustment(for: team),
                        gbOverride: gbOverride(for: team),
                        wildcardContenders: wildcardContenders
                    )
                    if tIdx != group.teams.indices.last {
                        Divider().opacity(0.25)
                    }
                }
            }
        }
    }
}

// MARK: - Frozen-pane chrome

/// Top-left "Team" cell that pairs with `ScrollableStatsHeader`. Same
/// height as the stat headers so the two panes' top rows line up.
private struct FrozenIdentityHeader: View {
    var body: some View {
        Text("Team")
            .font(.caption.weight(.semibold))
            .foregroundStyle(.secondary)
            .padding(.leading, 14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .frame(height: StandingsLayout.headerHeight)
    }
}

/// Section header above each division group. Footnote-weight,
/// secondary tone — reads as a clean section label, no filled
/// background. The separator line above (rendered by
/// `DivisionBreak` between groups) carries the visual division
/// rather than a colored box around the text.
private struct DivisionSectionLabel: View {
    let text: String
    var body: some View {
        Text(text)
            .font(.footnote.weight(.semibold))
            .foregroundStyle(.secondary)
            .padding(.leading, 14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .frame(height: StandingsLayout.sectionLabelHeight)
    }
}

/// Right-side companion at the same height as the label so the team
/// rows below the break stay aligned across the frozen/scrolling
/// boundary. Transparent — the card glass background shows through.
private struct DivisionSectionSpacer: View {
    var body: some View {
        Color.clear
            .frame(height: StandingsLayout.sectionLabelHeight)
    }
}

/// Section break — vertical breathing room + a 2pt separator line.
/// Rendered on both panes between divisions so the rule stretches
/// as one continuous line across the column boundary. Replaces the
/// prior tinted-banner approach: a subtle rule + label reads as a
/// clean section divider rather than a colored box.
private struct DivisionBreak: View {
    var body: some View {
        VStack(spacing: 0) {
            Color.clear
                .frame(height: StandingsLayout.divisionGapHeight)
            Rectangle()
                .fill(Color(.separator))
                .frame(height: StandingsLayout.divisionRuleHeight)
        }
    }
}

/// Frozen-side row — team logo + 3-letter abbreviation + optional
/// clinch badge + optional magic-number pill. No stats, no leader
/// highlight (per the MLB-app convention the surrounding table
/// adopts). Magic only shown for the division leader at the top of
/// each group.
private struct FrozenIdentityCell: View {
    let team: TeamStanding
    let isLeader: Bool

    var body: some View {
        HStack(spacing: 6) {
            teamLogo
            Text(abbreviation)
                .font(.subheadline.weight(.semibold))
                .monospacedDigit()
                .lineLimit(1)
            if let badge = clinchBadge {
                clinchBadgeView(badge)
            }
            if isLeader, let magic = magicText {
                Text("M\(magic)")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(Color.accentColor)
            }
            Spacer(minLength: 0)
        }
        .padding(.leading, 14)
        .padding(.trailing, 4)
        .frame(height: StandingsLayout.rowHeight)
    }

    private var teamLogo: some View {
        AsyncImage(url: teamLogoURL(for: team.team_id)) { phase in
            if case .success(let image) = phase {
                image.resizable().scaledToFit()
            } else {
                RoundedRectangle(cornerRadius: 4)
                    .fill(Color(.tertiarySystemFill))
            }
        }
        .frame(width: StandingsLayout.logoSize, height: StandingsLayout.logoSize)
    }

    private var abbreviation: String {
        guard let code = team.team_id, !code.isEmpty else { return "—" }
        return teamAbbreviation(for: code)
    }

    private var clinchBadge: (letter: String, color: Color)? {
        clinchBadgeFor(team.clinch_indicator)
    }

    private var magicText: String? {
        guard let m = team.magic_number, !m.isEmpty, m != "-",
              let intVal = Int(m), intVal <= 25 else { return nil }
        return m
    }

    private func clinchBadgeView(_ badge: (letter: String, color: Color)) -> some View {
        Text(badge.letter)
            .font(.caption2.weight(.bold))
            .foregroundStyle(badge.color)
            .padding(.horizontal, 4)
            .padding(.vertical, 1)
            .overlay(
                RoundedRectangle(cornerRadius: 3)
                    .stroke(badge.color.opacity(0.6), lineWidth: 0.5)
            )
    }
}

// MARK: - Scrollable-pane chrome

/// Stat column headers — one row at the top of the scrollable pane,
/// shared across every division (not repeated per group). In
/// historical mode the five live-only columns (WCGB / L10 / STRK /
/// HOME / AWAY) collapse out, leaving W / L / PCT / GB / RDIFF.
private struct ScrollableStatsHeader: View {
    let isHistorical: Bool

    var body: some View {
        HStack(spacing: 0) {
            cell("W",     width: StandingsLayout.cell)
            cell("L",     width: StandingsLayout.cell)
            cell("PCT",   width: StandingsLayout.pct)
            cell("GB",    width: StandingsLayout.gb)
            if !isHistorical {
                cell("WCGB", width: StandingsLayout.gb)
                cell("L10",  width: StandingsLayout.record)
                cell("STRK", width: StandingsLayout.streak)
                cell("HOME", width: StandingsLayout.record)
                cell("AWAY", width: StandingsLayout.record)
            }
            cell("RDIFF", width: StandingsLayout.diff)
        }
        .padding(.trailing, 14)
        .frame(height: StandingsLayout.headerHeight)
    }

    private func cell(_ text: String, width: CGFloat) -> some View {
        Text(text)
            .font(.caption.weight(.semibold))
            .foregroundStyle(.secondary)
            .frame(width: width, alignment: .trailing)
    }
}

/// Scrollable-side row — pure stat columns, no team identity. Stays
/// in lockstep with `FrozenIdentityCell` on the frozen side via
/// matching `StandingsLayout.rowHeight`.
private struct ScrollableStatsRow: View {
    let team: TeamStanding
    let leader: TeamStanding?
    let isLeader: Bool
    let isHistorical: Bool
    /// Today's not-yet-official bump for this team (nil when none).
    var adjustment: (wDelta: Int, lDelta: Int)? = nil
    /// Recomputed columns for this team when today's results shift the
    /// standings; nil → keep the backend / leader-derived values.
    var gbOverride: AdjustedStandingColumns? = nil
    /// League's non-division-leaders sorted best→worst, for converting
    /// the backend's first-WC-anchored WCGB to the display convention.
    var wildcardContenders: [TeamStanding] = []

    var body: some View {
        let wDelta = adjustment?.wDelta ?? 0
        let lDelta = adjustment?.lDelta ?? 0
        let adjusted = wDelta > 0 || lDelta > 0
        let adjW = team.W.map { $0 + wDelta }
        let adjL = team.L.map { $0 + lDelta }
        // GB / WCGB: use the recomputed override when present (routed
        // through `formatGBString` so a "-" renders as the table's em-
        // dash), else the leader-derived / backend values — with the
        // backend's WCGB re-anchored from the 1st wild card to the 3rd
        // so both paths read the same way.
        let gbText = gbOverride.map { formatGBString($0.gb) }
            ?? formatGBString(formatGB(team: team, leader: leader, isLeader: isLeader))
        let wcgbText = gbOverride.map { formatGBString($0.wcgb) }
            ?? formatGBString(reformatWCGB(team: team, contenders: wildcardContenders))
        // PCT: adjusted win% when today's results shifted it, else base.
        let pctText = formatPct(gbOverride?.pct ?? team.win_pct)
        // STRK: rolled-forward streak when adjusted, else base. Blank /
        // "-" collapses to the em-dash like the base path.
        let strkValue = gbOverride?.strk ?? team.streak_code
        let strkText = (strkValue?.isEmpty == false && strkValue != "-") ? strkValue! : "—"
        // Home / Away splits + run diff: prefer the adjusted values.
        let homeText = formatRecord(w: gbOverride?.homeW ?? team.home_w,
                                    l: gbOverride?.homeL ?? team.home_l)
        let awayText = formatRecord(w: gbOverride?.awayW ?? team.away_w,
                                    l: gbOverride?.awayL ?? team.away_l)
        let rs = gbOverride?.runsScored  ?? team.runs_scored
        let ra = gbOverride?.runsAllowed ?? team.runs_allowed
        let diff: Int? = (rs != nil || ra != nil) ? ((rs ?? 0) - (ra ?? 0)) : nil
        return HStack(spacing: 0) {
            statCell(formatInt(adjW), width: StandingsLayout.cell)
            wlLossCell(adjL, dagger: adjusted, width: StandingsLayout.cell)
            statCell(pctText, width: StandingsLayout.pct)
            statCell(gbText, width: StandingsLayout.gb, dim: gbText == "—")
            if !isHistorical {
                statCell(wcgbText, width: StandingsLayout.gb, dim: true)
                statCell(formatRecord(w: team.last_ten_w, l: team.last_ten_l),
                         width: StandingsLayout.record)
                statCell(strkText, width: StandingsLayout.streak)
                statCell(homeText, width: StandingsLayout.record)
                statCell(awayText, width: StandingsLayout.record)
            }
            statCell(formatDiff(diff), width: StandingsLayout.diff)
        }
        .font(.subheadline)
        .padding(.trailing, 14)
        .frame(height: StandingsLayout.rowHeight)
    }

    private func statCell(_ text: String, width: CGFloat, dim: Bool = false) -> some View {
        Text(text)
            .frame(width: width, alignment: .trailing)
            .monospacedDigit()
            .foregroundStyle(dim ? Color.secondary : Color.primary)
    }

    /// Losses cell. When `dagger` is set (this team has a not-yet-
    /// official adjustment), a small secondary "†" floats as a
    /// superscript at the top-trailing corner — overlaid outside the
    /// fixed-width frame so it never shifts the L value off its
    /// right-aligned baseline. Explained by the legend at the bottom.
    private func wlLossCell(_ value: Int?, dagger: Bool, width: CGFloat) -> some View {
        Text(formatInt(value))
            .monospacedDigit()
            .foregroundStyle(.primary)
            .frame(width: width, alignment: .trailing)
            .overlay(alignment: .topTrailing) {
                if dagger {
                    Text("†")
                        .font(.system(size: 8))
                        .foregroundStyle(.secondary)
                        .offset(x: 6, y: -2)
                }
            }
    }
}

#Preview {
    StandingsView()
}
