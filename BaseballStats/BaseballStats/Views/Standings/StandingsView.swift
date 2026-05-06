//
//  StandingsView.swift
//  BaseballStats
//
//  Standings tab — year picker + AL/NL switcher + three division cards
//  (East, Central, West) per league. Pulls flat standings from the
//  backend; the VM handles partitioning into divisions.
//

import SwiftUI

struct StandingsView: View {
    @StateObject private var viewModel = StandingsViewModel()
    @State private var selectedLeague: League = .al

    enum League: String, CaseIterable, Identifiable {
        case al = "AL"
        case nl = "NL"
        var id: String { rawValue }

        var fullName: String {
            switch self {
            case .al: return "American League"
            case .nl: return "National League"
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
            .toolbarBackground(.visible, for: .navigationBar)
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

    /// Year picker lives in the toolbar so the body stays focused on
    /// the standings cards. Range: 2000 → current year, newest first.
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
                leaguePicker
                ForEach(Self.divisionOrder, id: \.self) { div in
                    if let teams = currentLeagueStandings()[div], !teams.isEmpty {
                        DivisionCard(
                            title: "\(selectedLeague.rawValue) \(Self.divisionName[div] ?? div)",
                            teams: teams
                        )
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

    private var leaguePicker: some View {
        Picker("League", selection: $selectedLeague) {
            ForEach(League.allCases) { league in
                Text(league.rawValue).tag(league)
            }
        }
        .pickerStyle(.segmented)
    }

    private func currentLeagueStandings() -> [String: [TeamStanding]] {
        selectedLeague == .al ? viewModel.alStandings : viewModel.nlStandings
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

private struct DivisionCard: View {
    let title: String
    let teams: [TeamStanding]

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

            DivisionHeaderRow()
            Divider()

            ForEach(Array(teams.enumerated()), id: \.offset) { index, team in
                TeamRow(team: team, leader: teams.first, isLeader: index == 0)
                if index != teams.indices.last {
                    Divider().opacity(0.4)
                }
            }
        }
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 16))
    }
}

private enum StandingsLayout {
    static let team:  CGFloat = 130
    static let cell:  CGFloat = 44
    static let gb:    CGFloat = 48
}

private struct DivisionHeaderRow: View {
    var body: some View {
        HStack(spacing: 0) {
            Text("Team").frame(width: StandingsLayout.team, alignment: .leading)
            Spacer(minLength: 4)
            Text("W").frame(width: StandingsLayout.cell, alignment: .trailing)
            Text("L").frame(width: StandingsLayout.cell, alignment: .trailing)
            Text("PCT").frame(width: StandingsLayout.cell, alignment: .trailing)
            Text("GB").frame(width: StandingsLayout.gb, alignment: .trailing)
        }
        .font(.caption.weight(.bold))
        .foregroundStyle(.secondary)
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
    }
}

private struct TeamRow: View {
    let team: TeamStanding
    let leader: TeamStanding?
    /// Subtle highlight for the division leader (top row).
    let isLeader: Bool

    var body: some View {
        HStack(spacing: 0) {
            HStack(spacing: 6) {
                if isLeader {
                    Image(systemName: "star.fill")
                        .font(.caption2)
                        .foregroundStyle(.yellow)
                }
                Text(team.team_name ?? "—")
                    .lineLimit(1)
                    .truncationMode(.tail)
            }
            .frame(width: StandingsLayout.team, alignment: .leading)

            Spacer(minLength: 4)

            Text(formatInt(team.W))
                .frame(width: StandingsLayout.cell, alignment: .trailing)
                .monospacedDigit()
            Text(formatInt(team.L))
                .frame(width: StandingsLayout.cell, alignment: .trailing)
                .monospacedDigit()
            Text(formatPct(team.win_pct))
                .frame(width: StandingsLayout.cell, alignment: .trailing)
                .monospacedDigit()
            Text(formatGB(team: team, leader: leader, isLeader: isLeader))
                .frame(width: StandingsLayout.gb, alignment: .trailing)
                .monospacedDigit()
                .foregroundStyle(isLeader ? .secondary : .primary)
        }
        .font(.subheadline)
        .fontWeight(isLeader ? .semibold : .regular)
        .padding(.horizontal, 14)
        .padding(.vertical, 9)
        .background(isLeader ? Color.accentColor.opacity(0.10) : Color.clear)
        .contentShape(Rectangle())
        .onTapGesture {
            // Placeholder for team-detail navigation later.
        }
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

/// Games-back: ((leader.W − team.W) + (team.L − leader.L)) / 2.
/// Display "—" for the leader (always 0 GB), "X.X" for everyone else.
/// Half-game values are common (one team has played more), hence the .X.
private func formatGB(team: TeamStanding, leader: TeamStanding?, isLeader: Bool) -> String {
    if isLeader { return "—" }
    guard let lw = leader?.W, let ll = leader?.L,
          let tw = team.W, let tl = team.L else { return "—" }
    let gb = (Double(lw - tw) + Double(tl - ll)) / 2.0
    if gb == 0 { return "—" }
    return String(format: "%.1f", gb)
}

#Preview {
    StandingsView()
}
