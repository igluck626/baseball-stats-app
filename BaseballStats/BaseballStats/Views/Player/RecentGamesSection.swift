//
//  RecentGamesSection.swift
//  BaseballStats
//
//  Overview-tab mini table: a pill selector (Last 5 / 10 / 15 / 30)
//  above a one-row table summarizing the player's rolling window for
//  the current season. Reuses the game-log endpoint and the same
//  `WindowSnapshot` adapter the Game Logs tab uses, so values match
//  cell-for-cell between the two surfaces.
//
//  The view owns its own VM and one-shot fetch — the parent profile
//  VM stays focused on bio + season totals so this section can fail
//  independently without affecting the rest of the overview.
//

import Combine
import SwiftUI

// MARK: - ViewModel

@MainActor
final class RecentGamesViewModel: ObservableObject {
    let playerId: Int
    let isPitcher: Bool
    let season: Int

    @Published var splits: GameLogSplits?
    @Published var isLoading = false
    @Published var error: String?

    private let api: APIClient

    init(playerId: Int, isPitcher: Bool, season: Int, api: APIClient = .shared) {
        self.playerId = playerId
        self.isPitcher = isPitcher
        self.season = season
        self.api = api
    }

    func load() async {
        // Re-fetching is cheap — backend serves from the cached current
        // season — so we don't gate on a previously-loaded value.
        isLoading = true
        error = nil
        do {
            let response = isPitcher
                ? try await api.getPitchingGameLogs(playerId: playerId, season: season)
                : try await api.getBattingGameLogs(playerId: playerId, season: season)
            splits = response?.splits
        } catch {
            self.error = error.localizedDescription
            splits = nil
        }
        isLoading = false
    }
}

// MARK: - View

/// Drop-in section for the Overview tab. The parent passes a stable
/// `season` (current year for active players) and the player's role —
/// flipping role recreates the VM via `.id(isPitcher)` so a two-way
/// player's recent-games window matches their selected role.
struct RecentGamesSection: View {
    let playerId: Int
    let isPitcher: Bool
    let season: Int

    @StateObject private var vm: RecentGamesViewModel
    /// User-selected window. Defaults to Last 10 — the same default the
    /// Game Logs splits row falls back to when the user hasn't tapped
    /// anything yet would be "Season", but for a *recent* games card the
    /// product wants a finer window front-and-center.
    @State private var window: WindowSize = .last10

    enum WindowSize: Int, CaseIterable, Identifiable {
        case last5 = 5
        case last10 = 10
        case last15 = 15
        case last30 = 30
        var id: Int { rawValue }
        var label: String { "Last \(rawValue)" }
    }

    init(playerId: Int, isPitcher: Bool, season: Int) {
        self.playerId = playerId
        self.isPitcher = isPitcher
        self.season = season
        _vm = StateObject(wrappedValue: RecentGamesViewModel(
            playerId: playerId, isPitcher: isPitcher, season: season
        ))
    }

    var body: some View {
        VStack(spacing: 10) {
            header
            windowPicker
            content
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity)
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 20))
        .shadow(color: .black.opacity(0.06), radius: 8, x: 0, y: 2)
        .task { await vm.load() }
    }

    private var header: some View {
        HStack(spacing: 8) {
            Text("Recent Games").font(.headline)
            Spacer()
        }
    }

    private var windowPicker: some View {
        Picker("Window", selection: $window) {
            ForEach(WindowSize.allCases) { w in
                Text(w.label).tag(w)
            }
        }
        .pickerStyle(.segmented)
    }

    @ViewBuilder
    private var content: some View {
        if vm.isLoading && vm.splits == nil {
            ProgressView()
                .frame(maxWidth: .infinity, minHeight: SplitsLayout.rowHeight * 2)
        } else if let error = vm.error, vm.splits == nil {
            placeholderRow(error)
        } else if let splits = vm.splits {
            statsTable(for: snapshot(from: splits))
        } else {
            placeholderRow("No recent games for \(String(season))")
        }
    }

    /// Pick the right rolling window off the API response. Backend
    /// returns nil for windows the player hasn't logged enough games
    /// for; we render an em-dash row in that case.
    private func snapshot(from splits: GameLogSplits) -> WindowSnapshot {
        switch window {
        case .last5:  return WindowSnapshot.from(splits.last_5)
        case .last10: return WindowSnapshot.from(splits.last_10)
        case .last15: return WindowSnapshot.from(splits.last_15)
        case .last30: return WindowSnapshot.from(splits.last_30)
        }
    }

    // MARK: - Mini table

    /// Horizontally scrolling stat table for the selected window. The
    /// pill selector above already communicates which window is in
    /// view, so there's no frozen "Last N" left pane — column headers
    /// on top, single stat row beneath.
    private func statsTable(for snapshot: WindowSnapshot) -> some View {
        ScrollView(.horizontal, showsIndicators: false) {
            VStack(spacing: 0) {
                scrollableHeader
                Divider()
                scrollableStatRow(snapshot)
            }
        }
        .frame(maxWidth: .infinity)
        .clipShape(RoundedRectangle(cornerRadius: 12))
        .overlay(
            RoundedRectangle(cornerRadius: 12)
                .stroke(Color(.separator).opacity(0.4), lineWidth: 0.5)
        )
    }

    @ViewBuilder
    private var scrollableHeader: some View {
        HStack(spacing: 0) {
            if isPitcher {
                headerCell("G",   width: SplitsLayout.g)
                headerCell("IP",  width: SplitsLayout.ip)
                headerCell("H",   width: SplitsLayout.h)
                headerCell("R",   width: SplitsLayout.r)
                headerCell("ER",  width: SplitsLayout.er)
                headerCell("BB",  width: SplitsLayout.bb)
                headerCell("SO",  width: SplitsLayout.so)
                headerCell("HR",  width: SplitsLayout.hr)
                headerCell("ERA", width: SplitsLayout.era)
            } else {
                headerCell("G",   width: SplitsLayout.g)
                headerCell("AB",  width: SplitsLayout.ab)
                headerCell("H",   width: SplitsLayout.h)
                headerCell("HR",  width: SplitsLayout.hr)
                headerCell("RBI", width: SplitsLayout.rbi)
                headerCell("BB",  width: SplitsLayout.bb)
                headerCell("SO",  width: SplitsLayout.so)
                headerCell("SB",  width: SplitsLayout.sb)
                headerCell("AVG", width: SplitsLayout.rate)
                headerCell("OBP", width: SplitsLayout.rate)
                headerCell("SLG", width: SplitsLayout.rate)
                headerCell("OPS", width: SplitsLayout.rate)
            }
        }
        .font(.caption.weight(.semibold))
        .foregroundStyle(.secondary)
        .frame(height: SplitsLayout.rowHeight)
    }

    private func headerCell(_ label: String, width: CGFloat) -> some View {
        Text(label)
            .frame(width: width, alignment: .trailing)
            .padding(.horizontal, 2)
    }

    @ViewBuilder
    private func scrollableStatRow(_ s: WindowSnapshot) -> some View {
        HStack(spacing: 0) {
            if isPitcher {
                statCell(formatIntLocal(s.g),    width: SplitsLayout.g)
                statCell(formatIPLocal(s.ip),    width: SplitsLayout.ip)
                statCell(formatIntLocal(s.h),    width: SplitsLayout.h)
                statCell(formatIntLocal(s.r),    width: SplitsLayout.r)
                statCell(formatIntLocal(s.er),   width: SplitsLayout.er)
                statCell(formatIntLocal(s.bb),   width: SplitsLayout.bb)
                statCell(formatIntLocal(s.so),   width: SplitsLayout.so)
                statCell(formatIntLocal(s.hr),   width: SplitsLayout.hr)
                statCell(format2Local(s.era),    width: SplitsLayout.era)
            } else {
                statCell(formatIntLocal(s.g),    width: SplitsLayout.g)
                statCell(formatIntLocal(s.ab),   width: SplitsLayout.ab)
                statCell(formatIntLocal(s.h),    width: SplitsLayout.h)
                statCell(formatIntLocal(s.hr),   width: SplitsLayout.hr)
                statCell(formatIntLocal(s.rbi),  width: SplitsLayout.rbi)
                statCell(formatIntLocal(s.bb),   width: SplitsLayout.bb)
                statCell(formatIntLocal(s.so),   width: SplitsLayout.so)
                statCell(formatIntLocal(s.sb),   width: SplitsLayout.sb)
                statCell(format3Local(s.avg),    width: SplitsLayout.rate)
                statCell(format3Local(s.obp),    width: SplitsLayout.rate)
                statCell(format3Local(s.slg),    width: SplitsLayout.rate)
                statCell(format3Local(s.ops),    width: SplitsLayout.rate)
            }
        }
        .font(.caption)
        .frame(height: SplitsLayout.rowHeight)
    }

    private func statCell(_ text: String, width: CGFloat) -> some View {
        Text(text)
            .frame(width: width, alignment: .trailing)
            .monospacedDigit()
            .padding(.horizontal, 2)
    }

    private func placeholderRow(_ text: String) -> some View {
        Text(text)
            .font(.caption)
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, minHeight: SplitsLayout.rowHeight * 2)
            .multilineTextAlignment(.center)
    }
}

// MARK: - Local formatters

// Kept file-private to avoid colliding with the format helpers in
// GameLogsView / PlayerProfileView. They're tiny — duplicating them
// is cheaper than coupling three files through a shared namespace.

private func formatIntLocal(_ value: Int?) -> String {
    guard let value else { return "—" }
    return String(value)
}

private func format2Local(_ value: Double?) -> String {
    guard let value else { return "—" }
    return String(format: "%.2f", value)
}

private func format3Local(_ value: Double?) -> String {
    guard let value else { return "—" }
    // ".571" style — strip the leading zero on a sub-1.000 rate.
    let s = String(format: "%.3f", value)
    if s.hasPrefix("0.")  { return String(s.dropFirst()) }
    if s.hasPrefix("-0.") { return "-" + String(s.dropFirst(2)) }
    return s
}

private func formatIPLocal(_ value: Double?) -> String {
    guard let value else { return "—" }
    // Innings pitched uses the .1 / .2 convention (= 1/3, 2/3 of an
    // inning). Pull whole + fractional, snap fraction to {.0, .1, .2}.
    let whole = Int(value)
    let frac = value - Double(whole)
    let third: String
    if frac < 0.166 { third = ".0" }
    else if frac < 0.5 { third = ".1" }
    else { third = ".2" }
    return "\(whole)\(third)"
}
