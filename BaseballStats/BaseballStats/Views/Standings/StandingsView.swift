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
            .navigationBarTitleDisplayMode(.large)
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
        let years = Array((2000...StandingsViewModel.currentYear).reversed())
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
                    case .al: divisionList(for: viewModel.alStandings, leagueLabel: "AL")
                    case .nl: divisionList(for: viewModel.nlStandings, leagueLabel: "NL")
                    case .wc: wildcardContent
                    }
                }
                if let footer = updatedFooter {
                    Text(footer)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .padding(.top, 4)
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 12)
        }
    }

    private var tabPicker: some View {
        // Wildcard tab hidden if neither league has WC data (e.g. historical
        // years where division_leader is null across the board — there's no
        // signal to compute the WC race from).
        let hasWildcardData = !viewModel.alWildcard.isEmpty || !viewModel.nlWildcard.isEmpty
        let available: [TabSelection] = hasWildcardData
            ? TabSelection.allCases
            : [.al, .nl]
        return Picker("Tab", selection: $selectedTab) {
            ForEach(available) { tab in
                Text(tab.label).tag(tab)
            }
        }
        .pickerStyle(.segmented)
        .onChange(of: hasWildcardData) { _, has in
            if !has && selectedTab == .wc { selectedTab = .al }
        }
    }

    @ViewBuilder
    private func divisionList(for buckets: [String: [TeamStanding]], leagueLabel: String) -> some View {
        ForEach(Self.divisionOrder, id: \.self) { div in
            if let teams = buckets[div], !teams.isEmpty {
                DivisionCard(
                    title: "\(leagueLabel) \(Self.divisionName[div] ?? div)",
                    teams: teams,
                    style: .division
                )
            }
        }
    }

    @ViewBuilder
    private var wildcardContent: some View {
        VStack(spacing: 16) {
            if !viewModel.alWildcard.isEmpty {
                DivisionCard(
                    title: "AL Wildcard",
                    teams: viewModel.alWildcard,
                    style: .wildcard
                )
            }
            if !viewModel.nlWildcard.isEmpty {
                DivisionCard(
                    title: "NL Wildcard",
                    teams: viewModel.nlWildcard,
                    style: .wildcard
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

private enum CardStyle {
    case division   // Highlight the top row as division leader.
    case wildcard   // Highlight the top 3 as wild-card holders.
}

private struct DivisionCard: View {
    let title: String
    let teams: [TeamStanding]
    let style: CardStyle

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

            ForEach(Array(teams.enumerated()), id: \.offset) { index, team in
                TeamRow(
                    team: team,
                    leader: teams.first,
                    isLeader: index == 0,
                    style: style,
                    rankInList: index
                )
                if index != teams.indices.last {
                    Divider().opacity(0.4)
                }
            }
        }
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 16))
    }
}

/// Column widths for the scrollable stat tail. Tuned to keep the
/// full row within ~520pt total so an iPhone 17 Pro shows ~3/4 of
/// the stats without scrolling, and the rest pulls in via swipe.
private enum StandingsLayout {
    static let cell:   CGFloat = 38
    static let pct:    CGFloat = 42
    static let gb:     CGFloat = 44
    static let record: CGFloat = 48   // "54-27" style
    static let streak: CGFloat = 36
    static let diff:   CGFloat = 44
    static let magic:  CGFloat = 38
}

private struct TeamRow: View {
    let team: TeamStanding
    let leader: TeamStanding?
    /// Top row of a division card.
    let isLeader: Bool
    let style: CardStyle
    /// Zero-based position in the list — used for wildcard "in / out"
    /// highlighting (top 3 are in).
    let rankInList: Int

    var body: some View {
        HStack(spacing: 12) {
            // Fixed-width team name + clinch tag area. Wide enough for
            // "Cleveland Guardians" + a single-letter clinch badge.
            teamNameColumn
                .frame(width: 170, alignment: .leading)

            // Everything stat-side lives in a horizontal scroller so
            // narrower phones still get all the columns; on wider
            // phones the user usually doesn't have to scroll.
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 0) {
                    statCell(formatInt(team.W),  width: StandingsLayout.cell)
                    statCell(formatInt(team.L),  width: StandingsLayout.cell)
                    statCell(formatPct(team.win_pct), width: StandingsLayout.pct)
                    statCell(formatGB(team: team, leader: leader, isLeader: isLeader),
                             width: StandingsLayout.gb, dim: isLeader)
                    // Wildcard GB only meaningful on the WC tab; division
                    // tab still renders it (low cost) so the column stays
                    // present and aligned across both views.
                    statCell(formatGBString(team.wild_card_games_back),
                             width: StandingsLayout.gb, dim: true)
                    statCell(formatRecord(w: team.last_ten_w, l: team.last_ten_l),
                             width: StandingsLayout.record)
                    statCell(team.hasStreak ? (team.streak_code ?? "—") : "—",
                             width: StandingsLayout.streak)
                    statCell(formatRecord(w: team.home_w, l: team.home_l),
                             width: StandingsLayout.record)
                    statCell(formatRecord(w: team.away_w, l: team.away_l),
                             width: StandingsLayout.record)
                    statCell(formatDiff(team.runDifferential),
                             width: StandingsLayout.diff)
                    // Magic number only shown for the division leader,
                    // and only when the API gave us a real value (not
                    // "-" which means too early to compute).
                    if isLeader, let magic = team.magic_number,
                       !magic.isEmpty, magic != "-" {
                        statCell("Mg \(magic)", width: StandingsLayout.magic,
                                 emphasized: true)
                    }
                }
                .padding(.trailing, 14)
            }
            .scrollContentBackground(.hidden)
        }
        .font(.subheadline)
        .fontWeight(isLeader ? .semibold : .regular)
        .padding(.leading, 14)
        .padding(.vertical, 9)
        .background(rowBackground)
        .contentShape(Rectangle())
        .onTapGesture {
            // Placeholder for team-detail navigation later.
        }
    }

    /// Star prefix for division leader, team name, single-character
    /// clinch badge to the right of the name.
    private var teamNameColumn: some View {
        HStack(spacing: 6) {
            if isLeader && style == .division {
                Image(systemName: "star.fill")
                    .font(.caption2)
                    .foregroundStyle(.yellow)
            }
            Text(team.team_name ?? "—")
                .lineLimit(1)
                .minimumScaleFactor(0.8)
            if let badge = clinchBadge {
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
    }

    /// Single-character clinch tag mapping per MLB / bbref convention.
    private var clinchBadge: (letter: String, color: Color)? {
        guard let raw = team.clinch_indicator?.lowercased(),
              !raw.isEmpty else { return nil }
        switch raw {
        case "z": return ("z", .yellow)   // clinched home-field advantage
        case "y": return ("y", .green)    // clinched division
        case "x": return ("x", .blue)     // clinched playoff berth
        case "w": return ("w", .teal)     // clinched wild card
        case "e": return ("e", .red)      // eliminated
        default:  return (raw, .secondary)
        }
    }

    /// Subtle row background — division leader gets the accent tint;
    /// wildcard top-3 gets a light green tint to signal "in the
    /// playoffs as of right now."
    private var rowBackground: Color {
        switch style {
        case .division:
            return isLeader ? Color.accentColor.opacity(0.10) : .clear
        case .wildcard:
            return rankInList < 3
                ? Color.green.opacity(0.12)
                : .clear
        }
    }

    private func statCell(_ text: String, width: CGFloat,
                          dim: Bool = false, emphasized: Bool = false) -> some View {
        Text(text)
            .frame(width: width, alignment: .trailing)
            .monospacedDigit()
            .foregroundStyle(emphasized ? Color.accentColor : (dim ? Color.secondary : Color.primary))
            .fontWeight(emphasized ? .semibold : nil)
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

/// MLB Stats API renders an absent games-back as the bare string "-".
/// Normalize to our em-dash so the rest of the row stays visually
/// consistent.
private func formatGBString(_ value: String?) -> String {
    guard let v = value, !v.isEmpty else { return "—" }
    if v == "-" { return "—" }
    return v
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

#Preview {
    StandingsView()
}
