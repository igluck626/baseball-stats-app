//
//  GameLogsView.swift
//  BaseballStats
//
//  Game-by-game log for a player, with a vertical splits table and a
//  monthly-grouped game list. Lives inside PlayerProfileView's Game
//  Logs tab.
//
//  Owns its own GameLogsViewModel — the parent profile VM stays focused
//  on bio + season totals. The parent passes `isPitcher` (derived from
//  the role toggle) and applies `.id(isPitcher)` so flipping roles
//  recreates this view and its VM with fresh data.
//

import Combine
import SwiftUI

// MARK: - ViewModel

@MainActor
final class GameLogsViewModel: ObservableObject {
    let playerId: Int
    let isPitcher: Bool

    @Published var gameLogResponse: GameLogResponse?
    @Published var isLoading = false
    @Published var error: String?
    @Published var selectedSeason: Int

    private let api: APIClient

    /// Year to default to on first appearance. Matches the season picker's
    /// upper bound so a fresh load lands on "this year."
    static var currentYear: Int {
        Calendar.current.component(.year, from: Date())
    }

    init(playerId: Int, isPitcher: Bool, api: APIClient = .shared) {
        self.playerId = playerId
        self.isPitcher = isPitcher
        self.api = api
        self.selectedSeason = Self.currentYear
    }

    func load() async {
        isLoading = true
        error = nil
        do {
            gameLogResponse = isPitcher
                ? try await api.getPitchingGameLogs(playerId: playerId, season: selectedSeason)
                : try await api.getBattingGameLogs(playerId: playerId, season: selectedSeason)
        } catch {
            self.error = error.localizedDescription
            gameLogResponse = nil
        }
        isLoading = false
    }
}

// MARK: - Split row identifier

/// Rows in the splits table. Maps directly to a games-table filter.
private enum SplitRow: Hashable {
    case last(Int)   // 5, 10, 15, 30
    case season
    case custom

    var label: String {
        switch self {
        case .last(let n): return "Last \(n)"
        case .season:      return "Season"
        case .custom:      return "Custom"
        }
    }
}

// MARK: - View

struct GameLogsView: View {
    let playerId: Int
    let isPitcher: Bool

    @StateObject private var vm: GameLogsViewModel
    @State private var selectedRow: SplitRow = .season
    @State private var customInput: String = ""

    init(playerId: Int, isPitcher: Bool) {
        self.playerId = playerId
        self.isPitcher = isPitcher
        _vm = StateObject(wrappedValue: GameLogsViewModel(
            playerId: playerId, isPitcher: isPitcher
        ))
    }

    var body: some View {
        VStack(spacing: 16) {
            splitsTable
            seasonPicker
            gamesTable
        }
        .task { await vm.load() }
        .onChange(of: vm.selectedSeason) { _, _ in
            Task { await vm.load() }
        }
    }

    // MARK: - Splits table

    /// Five fixed windows (Last 5/10/15/30, Season) + a Custom row with a
    /// number field. Tapping a row filters the games table below; tapping
    /// the Custom row applies the typed N (live as you type).
    private var splitsTable: some View {
        VStack(spacing: 0) {
            splitsHeader
            Divider()
            splitsBodyRow(.last(5))
            Divider().opacity(0.5)
            splitsBodyRow(.last(10))
            Divider().opacity(0.5)
            splitsBodyRow(.last(15))
            Divider().opacity(0.5)
            splitsBodyRow(.last(30))
            Divider().opacity(0.5)
            splitsBodyRow(.season)
            Divider().opacity(0.5)
            customSplitRow
        }
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 16))
    }

    private var splitsHeader: some View {
        HStack(spacing: 0) {
            Text("Window").frame(width: SplitsLayout.label, alignment: .leading)
            Spacer(minLength: 4)
            Text("G").frame(width: SplitsLayout.g, alignment: .trailing)
            if isPitcher {
                Text("ERA").frame(width: SplitsLayout.cell, alignment: .trailing)
                Text("SO").frame(width: SplitsLayout.cell, alignment: .trailing)
                Text("WHIP").frame(width: SplitsLayout.cell, alignment: .trailing)
                Text("K/9").frame(width: SplitsLayout.cell, alignment: .trailing)
            } else {
                Text("AVG").frame(width: SplitsLayout.cell, alignment: .trailing)
                Text("HR").frame(width: SplitsLayout.cell, alignment: .trailing)
                Text("RBI").frame(width: SplitsLayout.cell, alignment: .trailing)
                Text("OPS").frame(width: SplitsLayout.cell, alignment: .trailing)
            }
        }
        .font(.caption.weight(.semibold))
        .foregroundStyle(.secondary)
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
    }

    /// One row of the splits table for a fixed window or the season.
    private func splitsBodyRow(_ row: SplitRow) -> some View {
        let snapshot = snapshot(for: row)
        let isSelected = selectedRow == row
        return HStack(spacing: 0) {
            Text(row.label)
                .frame(width: SplitsLayout.label, alignment: .leading)
                .fontWeight(isSelected ? .semibold : .regular)
            Spacer(minLength: 4)
            statCells(snapshot)
        }
        .font(.caption)
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(isSelected ? Color.accentColor.opacity(0.18) : Color.clear)
        .contentShape(Rectangle())
        .onTapGesture {
            withAnimation(.easeInOut(duration: 0.15)) {
                // Tapping an already-active Last N row clears the
                // filter back to Season. Season itself is the default
                // — tapping it just stays put.
                if case .last = row, selectedRow == row {
                    selectedRow = .season
                } else {
                    selectedRow = row
                }
            }
        }
    }

    /// Custom row — label sits in the Window column (full label width
    /// so it never wraps), the number input occupies the G column slot
    /// (the typed N is itself the games-count for this row), and the
    /// remaining stats fill the trailing 4 cells. When the field is
    /// empty all four show "—" (computed snapshot returns `.empty`).
    private var customSplitRow: some View {
        let n = Int(customInput) ?? 0
        let snapshot = customSnapshot(n: n)
        let isSelected = selectedRow == .custom
        return HStack(spacing: 0) {
            Text("Custom")
                .fontWeight(isSelected ? .semibold : .regular)
                .lineLimit(1)
                .fixedSize()
                .frame(width: SplitsLayout.label, alignment: .leading)
            Spacer(minLength: 4)
            customGCell(isSelected: isSelected)
            statCellsExcludingG(snapshot)
        }
        .font(.caption)
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(isSelected ? Color.accentColor.opacity(0.18) : Color.clear)
        .contentShape(Rectangle())
        // Tap on the row chrome (not the field) selects without
        // dismissing the keyboard.
        .onTapGesture {
            withAnimation(.easeInOut(duration: 0.15)) {
                selectedRow = .custom
            }
        }
    }

    /// TextField rendered to look like a tappable cell — borderless,
    /// soft systemGray6 background, right-aligned monospaced digits so
    /// it lines up visually with the integer G values in the rows above.
    private func customGCell(isSelected: Bool) -> some View {
        TextField("#", text: $customInput)
            .keyboardType(.numberPad)
            .multilineTextAlignment(.trailing)
            .textFieldStyle(.plain)
            // Pin the field to .subheadline so the parent's .caption
            // doesn't shrink the input area below comfortable tap size
            // while still keeping the row at the table's font cadence.
            .font(.subheadline)
            .monospacedDigit()
            .lineLimit(1)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .frame(width: SplitsLayout.g, alignment: .trailing)
            .background(
                RoundedRectangle(cornerRadius: 4)
                    .fill(Color(.systemGray6))
            )
            .onSubmit { selectedRow = .custom }
    }

    @ViewBuilder
    private func statCells(_ s: WindowSnapshot) -> some View {
        Text(formatInt(s.g)).frame(width: SplitsLayout.g, alignment: .trailing).monospacedDigit()
        statCellsExcludingG(s)
    }

    /// Just the four post-G stat cells — used by the Custom row, whose
    /// G slot is replaced by the TextField rather than a Text value.
    @ViewBuilder
    private func statCellsExcludingG(_ s: WindowSnapshot) -> some View {
        if isPitcher {
            Text(format2(s.era)).frame(width: SplitsLayout.cell, alignment: .trailing).monospacedDigit()
            Text(formatInt(s.so)).frame(width: SplitsLayout.cell, alignment: .trailing).monospacedDigit()
            Text(format2(s.whip)).frame(width: SplitsLayout.cell, alignment: .trailing).monospacedDigit()
            Text(format2(s.kPer9)).frame(width: SplitsLayout.cell, alignment: .trailing).monospacedDigit()
        } else {
            Text(format3(s.avg)).frame(width: SplitsLayout.cell, alignment: .trailing).monospacedDigit()
            Text(formatInt(s.hr)).frame(width: SplitsLayout.cell, alignment: .trailing).monospacedDigit()
            Text(formatInt(s.rbi)).frame(width: SplitsLayout.cell, alignment: .trailing).monospacedDigit()
            Text(format3(s.ops)).frame(width: SplitsLayout.cell, alignment: .trailing).monospacedDigit()
        }
    }

    /// Pulls the right `GameLogWindow` off the API response for fixed
    /// rows, then funnels into a uniform `WindowSnapshot` for display.
    /// API doesn't carry per-window WAR, so the pitcher column shows
    /// K/9 instead.
    private func snapshot(for row: SplitRow) -> WindowSnapshot {
        switch row {
        case .last(let n): return .from(apiWindow(forLastN: n))
        case .season:      return .from(vm.gameLogResponse?.splits?.season)
        case .custom:      return customSnapshot(n: Int(customInput) ?? 0)
        }
    }

    /// API ships exactly four rolling windows; map N → which one. Anything
    /// outside the four canonical sizes returns nil (the splits table only
    /// asks for 5/10/15/30, so this is defensive).
    private func apiWindow(forLastN n: Int) -> GameLogWindow? {
        switch n {
        case 5:  return vm.gameLogResponse?.splits?.last_5
        case 10: return vm.gameLogResponse?.splits?.last_10
        case 15: return vm.gameLogResponse?.splits?.last_15
        case 30: return vm.gameLogResponse?.splits?.last_30
        default: return nil
        }
    }

    /// Compute on-device for the custom window — backend only ships
    /// 5/10/15/30 + season.
    private func customSnapshot(n: Int) -> WindowSnapshot {
        guard n > 0,
              let games = vm.gameLogResponse?.games,
              !games.isEmpty else { return .empty }
        let prefix = Array(games.prefix(n))
        return isPitcher
            ? .computePitching(games: prefix)
            : .computeBatting(games: prefix)
    }

    // MARK: - Season picker

    private var seasonPicker: some View {
        let years = Array((2008...GameLogsViewModel.currentYear).reversed())
        return HStack {
            Text("Season")
                .font(.subheadline)
                .foregroundStyle(.secondary)
            Spacer()
            Picker("Season", selection: $vm.selectedSeason) {
                ForEach(years, id: \.self) { year in
                    Text(String(year)).tag(year)
                }
            }
            .pickerStyle(.menu)
            .labelsHidden()
        }
        .padding(.horizontal, 4)
    }

    // MARK: - Games table

    private var gamesTable: some View {
        VStack(spacing: 0) {
            if vm.isLoading && vm.gameLogResponse == nil {
                ProgressView().frame(maxWidth: .infinity, minHeight: 140)
            } else if let response = vm.gameLogResponse,
                      let games = response.games, !games.isEmpty {
                gamesContent(allGames: games)
            } else if let error = vm.error {
                tableMessage(systemImage: "exclamationmark.triangle", text: error)
            } else {
                tableMessage(
                    systemImage: "list.bullet.rectangle.portrait",
                    text: "No game logs for \(String(vm.selectedSeason))"
                )
            }
        }
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 16))
    }

    @ViewBuilder
    private func gamesContent(allGames: [GameLog]) -> some View {
        // Season → grouped by month (with month totals).
        // Anything else → flat list of the filtered prefix.
        if selectedRow == .season {
            monthGroupedTable(allGames: allGames)
        } else {
            flatGameTable(games: filteredGames(allGames))
        }
    }

    /// Apply the active row filter. Always operates on the reverse-chrono
    /// games array, taking the prefix.
    private func filteredGames(_ games: [GameLog]) -> [GameLog] {
        switch selectedRow {
        case .last(let n): return Array(games.prefix(n))
        case .season:      return games
        case .custom:
            guard let n = Int(customInput), n > 0 else { return games }
            return Array(games.prefix(n))
        }
    }

    private func flatGameTable(games: [GameLog]) -> some View {
        VStack(spacing: 0) {
            if isPitcher { PitchingGameTableHeader() } else { BattingGameTableHeader() }
            Divider()
            ForEach(Array(games.enumerated()), id: \.offset) { index, game in
                gameRow(game: game, alternate: !index.isMultiple(of: 2))
                if index != games.indices.last { Divider().opacity(0.25) }
            }
        }
    }

    private func monthGroupedTable(allGames: [GameLog]) -> some View {
        let groups = monthGroups(allGames: allGames)
        return VStack(spacing: 0) {
            if isPitcher { PitchingGameTableHeader() } else { BattingGameTableHeader() }
            Divider()
            ForEach(groups) { group in
                MonthHeaderCapsule(label: monthFullName(group.month, year: group.year))
                ForEach(Array(group.games.enumerated()), id: \.offset) { index, game in
                    gameRow(game: game, alternate: !index.isMultiple(of: 2))
                    if index != group.games.indices.last {
                        Divider().opacity(0.25)
                    }
                }
                if isPitcher {
                    PitchingMonthTotalsRow(group: group)
                } else {
                    BattingMonthTotalsRow(group: group)
                }
                // Stronger separator after each month's totals row.
                Divider()
            }
        }
    }

    @ViewBuilder
    private func gameRow(game: GameLog, alternate: Bool) -> some View {
        if isPitcher {
            PitchingGameRow(game: game, alternate: alternate)
        } else {
            BattingGameRow(game: game, alternate: alternate)
        }
    }

    private func tableMessage(systemImage: String, text: String) -> some View {
        VStack(spacing: 8) {
            Image(systemName: systemImage)
                .font(.system(size: 32))
                .foregroundStyle(.secondary)
            Text(text)
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
        .padding(28)
        .frame(maxWidth: .infinity)
    }

    // MARK: - Monthly grouping

    /// Walk `allGames` (reverse-chrono) in chronological order, group by
    /// (year, month), and compute season-to-date stats through the end
    /// of each month so the totals row can show running AVG/ERA. Returns
    /// the groups in display order (most recent month first).
    private func monthGroups(allGames: [GameLog]) -> [MonthGroup] {
        let chrono = Array(allGames.reversed())
        var buckets: [(year: Int, month: Int, games: [GameLog])] = []
        for g in chrono {
            guard let ym = parseYM(g.game_date) else { continue }
            if let last = buckets.last, last.year == ym.0, last.month == ym.1 {
                buckets[buckets.count - 1].games.append(g)
            } else {
                buckets.append((ym.0, ym.1, [g]))
            }
        }

        var cumulative: [GameLog] = []
        var out: [MonthGroup] = []
        for bucket in buckets {
            cumulative.append(contentsOf: bucket.games)
            let through: WindowSnapshot = isPitcher
                ? .computePitching(games: cumulative)
                : .computeBatting(games: cumulative)
            let monthly: WindowSnapshot = isPitcher
                ? .computePitching(games: bucket.games)
                : .computeBatting(games: bucket.games)
            out.append(MonthGroup(
                year: bucket.year,
                month: bucket.month,
                games: bucket.games.reversed(),  // reverse-chrono within month
                monthlyTotals: monthly,
                throughMonth: through
            ))
        }
        return out.reversed()
    }
}

// MARK: - Month header capsule

private struct MonthHeaderCapsule: View {
    let label: String
    var body: some View {
        Text(label)
            .font(.subheadline.weight(.semibold))
            .foregroundStyle(.primary)
            .padding(.horizontal, 12)
            .padding(.vertical, 5)
            .background(Capsule().fill(Color(.systemGray4)))
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

// MARK: - Month group model

private struct MonthGroup: Identifiable {
    let year: Int
    let month: Int
    /// Games in reverse-chrono order within this month.
    let games: [GameLog]
    /// Stats summed over just this month's games.
    let monthlyTotals: WindowSnapshot
    /// Season-to-date stats through the end of this month — used for the
    /// running AVG / ERA cell in the totals row.
    let throughMonth: WindowSnapshot

    var id: String { "\(year)-\(month)" }
}

// MARK: - Splits column widths

private enum SplitsLayout {
    static let label: CGFloat = 110
    /// Bumped from 36 → 44 so the Custom row's TextField has room to
    /// sit in the G column without crowding. All rows + header use
    /// this width, so column alignment stays consistent.
    static let g:     CGFloat = 44
    static let cell:  CGFloat = 50
}

// MARK: - Window snapshot

/// Uniform shape consumed by both the splits table and the monthly
/// totals row. Either source (API window or local games-array agg)
/// produces one of these so the renderer doesn't branch on origin.
private struct WindowSnapshot {
    var g: Int?
    // Batting
    var ab: Int?
    var h: Int?
    var hr: Int?
    var rbi: Int?
    var bb: Int?
    var avg: Double?
    var ops: Double?
    // Pitching
    var ip: Double?
    var er: Int?
    var so: Int?
    var era: Double?
    var whip: Double?
    var kPer9: Double?

    static let empty = WindowSnapshot()

    /// Adapter from the API's GameLogWindow.
    static func from(_ window: GameLogWindow?) -> WindowSnapshot {
        WindowSnapshot(
            g: window?.G,
            ab: window?.AB,
            h: window?.H,
            hr: window?.HR,
            rbi: window?.RBI,
            bb: window?.BB,
            avg: window?.BA,
            ops: window?.OPS,
            ip: window?.IP,
            er: window?.ER,
            so: window?.SO,
            era: window?.ERA,
            whip: window?.WHIP,
            kPer9: window?.K_per9
        )
    }

    /// Aggregate batting stats over a games slice. Mirrors the backend's
    /// `_aggregate_batting_window` formula so our local custom-window
    /// numbers match what the server would have returned.
    static func computeBatting(games: [GameLog]) -> WindowSnapshot {
        guard !games.isEmpty else { return .empty }
        let g = games.count
        let ab  = games.reduce(0) { $0 + ($1.AB ?? 0) }
        let h   = games.reduce(0) { $0 + ($1.H ?? 0) }
        let dbl = games.reduce(0) { $0 + ($1.doubles ?? 0) }
        let trp = games.reduce(0) { $0 + ($1.triples ?? 0) }
        let hr  = games.reduce(0) { $0 + ($1.HR ?? 0) }
        let rbi = games.reduce(0) { $0 + ($1.RBI ?? 0) }
        let bb  = games.reduce(0) { $0 + ($1.BB ?? 0) }
        let hbp = games.reduce(0) { $0 + ($1.HBP ?? 0) }
        let sf  = games.reduce(0) { $0 + ($1.SF ?? 0) }

        let singles = h - dbl - trp - hr
        let avg: Double? = ab > 0 ? Double(h) / Double(ab) : nil
        let obpDen = ab + bb + hbp + sf
        let obp: Double? = obpDen > 0 ? Double(h + bb + hbp) / Double(obpDen) : nil
        let slg: Double? = ab > 0
            ? Double(singles + 2 * dbl + 3 * trp + 4 * hr) / Double(ab) : nil
        let ops: Double? = (obp != nil && slg != nil) ? (obp! + slg!) : nil

        return WindowSnapshot(
            g: g, ab: ab, h: h, hr: hr, rbi: rbi, bb: bb, avg: avg, ops: ops
        )
    }

    /// Aggregate pitching stats over a games slice.
    static func computePitching(games: [GameLog]) -> WindowSnapshot {
        guard !games.isEmpty else { return .empty }
        let g  = games.count
        let ip = games.reduce(0.0) { $0 + ($1.IP ?? 0) }
        let h  = games.reduce(0)   { $0 + ($1.H  ?? 0) }
        let er = games.reduce(0)   { $0 + ($1.ER ?? 0) }
        let bb = games.reduce(0)   { $0 + ($1.BB ?? 0) }
        let so = games.reduce(0)   { $0 + ($1.SO ?? 0) }

        let era:    Double? = ip > 0 ? Double(er) * 9 / ip : nil
        let whip:   Double? = ip > 0 ? Double(bb + h) / ip : nil
        let kPer9:  Double? = ip > 0 ? Double(so) * 9 / ip : nil

        return WindowSnapshot(
            g: g, h: h, bb: bb,
            ip: ip, er: er, so: so,
            era: era, whip: whip, kPer9: kPer9
        )
    }
}

// MARK: - Game table headers and rows

private struct BattingGameTableHeader: View {
    var body: some View {
        HStack(spacing: 0) {
            Text("Date").frame(width: 52, alignment: .leading)
            Text("Opp").frame(width: 64, alignment: .leading)
            Spacer(minLength: 4)
            Text("AB").frame(width: 30, alignment: .trailing)
            Text("H").frame(width: 28, alignment: .trailing)
            Text("HR").frame(width: 30, alignment: .trailing)
            Text("RBI").frame(width: 36, alignment: .trailing)
            Text("BB").frame(width: 30, alignment: .trailing)
            Text("AVG").frame(width: 50, alignment: .trailing)
        }
        .font(.caption.weight(.semibold))
        .foregroundStyle(.secondary)
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }
}

private struct BattingGameRow: View {
    let game: GameLog
    let alternate: Bool
    var body: some View {
        HStack(spacing: 0) {
            Text(formatGameDate(game.game_date))
                .frame(width: 52, alignment: .leading)
                .monospacedDigit()
            opponentLabel(game)
                .frame(width: 64, alignment: .leading)
            Spacer(minLength: 4)
            Text(formatInt(game.AB)).frame(width: 30, alignment: .trailing).monospacedDigit()
            Text(formatInt(game.H)).frame(width: 28, alignment: .trailing).monospacedDigit()
            Text(formatInt(game.HR)).frame(width: 30, alignment: .trailing).monospacedDigit()
            Text(formatInt(game.RBI)).frame(width: 36, alignment: .trailing).monospacedDigit()
            Text(formatInt(game.BB)).frame(width: 30, alignment: .trailing).monospacedDigit()
            Text(format3(perGameAVG(game))).frame(width: 50, alignment: .trailing).monospacedDigit()
        }
        .font(.caption)
        .padding(.horizontal, 12)
        .padding(.vertical, 7)
        .background(alternate ? Color(.systemGray6).opacity(0.5) : Color.clear)
    }

    private func perGameAVG(_ g: GameLog) -> Double? {
        guard let h = g.H, let ab = g.AB, ab > 0 else { return nil }
        return Double(h) / Double(ab)
    }
}

private struct BattingMonthTotalsRow: View {
    let group: MonthGroup
    var body: some View {
        HStack(spacing: 0) {
            Text(monthShortName(group.month))
                .frame(width: 52, alignment: .leading)
            Text("")
                .frame(width: 64, alignment: .leading)
            Spacer(minLength: 4)
            Text(formatInt(group.monthlyTotals.ab)).frame(width: 30, alignment: .trailing).monospacedDigit()
            Text(formatInt(group.monthlyTotals.h)).frame(width: 28, alignment: .trailing).monospacedDigit()
            Text(formatInt(group.monthlyTotals.hr)).frame(width: 30, alignment: .trailing).monospacedDigit()
            Text(formatInt(group.monthlyTotals.rbi)).frame(width: 36, alignment: .trailing).monospacedDigit()
            Text(formatInt(group.monthlyTotals.bb)).frame(width: 30, alignment: .trailing).monospacedDigit()
            // Season AVG through end of this month (cumulative).
            Text(format3(group.throughMonth.avg))
                .frame(width: 50, alignment: .trailing)
                .monospacedDigit()
        }
        .font(.caption.weight(.semibold))
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(Color(.systemGray5).opacity(0.7))
        // Top border to visually separate the totals row from the
        // month's game rows above.
        .overlay(alignment: .top) { Divider() }
    }
}

private struct PitchingGameTableHeader: View {
    var body: some View {
        HStack(spacing: 0) {
            Text("Date").frame(width: 52, alignment: .leading)
            Text("Opp").frame(width: 64, alignment: .leading)
            Spacer(minLength: 4)
            Text("IP").frame(width: 38, alignment: .trailing)
            Text("H").frame(width: 28, alignment: .trailing)
            Text("ER").frame(width: 30, alignment: .trailing)
            Text("BB").frame(width: 30, alignment: .trailing)
            Text("SO").frame(width: 30, alignment: .trailing)
            Text("ERA").frame(width: 50, alignment: .trailing)
        }
        .font(.caption.weight(.semibold))
        .foregroundStyle(.secondary)
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }
}

private struct PitchingGameRow: View {
    let game: GameLog
    let alternate: Bool
    var body: some View {
        HStack(spacing: 0) {
            Text(formatGameDate(game.game_date))
                .frame(width: 52, alignment: .leading)
                .monospacedDigit()
            opponentLabel(game)
                .frame(width: 64, alignment: .leading)
            Spacer(minLength: 4)
            Text(formatIP(game.IP)).frame(width: 38, alignment: .trailing).monospacedDigit()
            Text(formatInt(game.H)).frame(width: 28, alignment: .trailing).monospacedDigit()
            Text(formatInt(game.ER)).frame(width: 30, alignment: .trailing).monospacedDigit()
            Text(formatInt(game.BB)).frame(width: 30, alignment: .trailing).monospacedDigit()
            Text(formatInt(game.SO)).frame(width: 30, alignment: .trailing).monospacedDigit()
            Text(format2(perGameERA(game))).frame(width: 50, alignment: .trailing).monospacedDigit()
        }
        .font(.caption)
        .padding(.horizontal, 12)
        .padding(.vertical, 7)
        .background(alternate ? Color(.systemGray6).opacity(0.5) : Color.clear)
    }

    private func perGameERA(_ g: GameLog) -> Double? {
        guard let er = g.ER, let ip = g.IP, ip > 0 else { return nil }
        return Double(er) * 9.0 / ip
    }
}

private struct PitchingMonthTotalsRow: View {
    let group: MonthGroup
    var body: some View {
        HStack(spacing: 0) {
            Text(monthShortName(group.month))
                .frame(width: 52, alignment: .leading)
            Text("")
                .frame(width: 64, alignment: .leading)
            Spacer(minLength: 4)
            Text(formatIP(group.monthlyTotals.ip)).frame(width: 38, alignment: .trailing).monospacedDigit()
            Text(formatInt(group.monthlyTotals.h)).frame(width: 28, alignment: .trailing).monospacedDigit()
            Text(formatInt(group.monthlyTotals.er)).frame(width: 30, alignment: .trailing).monospacedDigit()
            Text(formatInt(group.monthlyTotals.bb)).frame(width: 30, alignment: .trailing).monospacedDigit()
            Text(formatInt(group.monthlyTotals.so)).frame(width: 30, alignment: .trailing).monospacedDigit()
            // Season ERA through end of this month (cumulative).
            Text(format2(group.throughMonth.era))
                .frame(width: 50, alignment: .trailing)
                .monospacedDigit()
        }
        .font(.caption.weight(.semibold))
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(Color(.systemGray5).opacity(0.7))
        .overlay(alignment: .top) { Divider() }
    }
}

// MARK: - Shared helpers

/// "vs NYM" or "@ LAD" — short H/A prefix plus a 2–3 char team code.
/// We always render a code (never the full team name) so the column
/// fits cleanly without truncation.
private func opponentLabel(_ game: GameLog) -> some View {
    HStack(spacing: 4) {
        if game.home_away == "H" {
            Text("vs").font(.caption2.weight(.medium)).foregroundStyle(.secondary)
        } else if game.home_away == "A" {
            Text("@").font(.caption2.weight(.medium)).foregroundStyle(.secondary)
        }
        Text(opponentShortCode(game.opponent))
            .lineLimit(1)
    }
}

/// Maps the opponent string the API returns (which may already be an
/// abbreviation, a short name, or a full team name) to a 2–3 char code.
/// The MLB Stats API picks `abbreviation` first when available, so most
/// games already arrive short — but some fall back to `name` ("New York
/// Mets") and would otherwise truncate. Three-step resolution:
///   1. Already short (≤3 chars, uppercase) → pass through.
///   2. Full team name in `mlbTeamShortCode` → use mapped code.
///   3. Unknown → first 3 chars of the first word, uppercased.
private func opponentShortCode(_ opponent: String?) -> String {
    guard let raw = opponent, !raw.isEmpty else { return "—" }
    if raw.count <= 3 && raw.uppercased() == raw {
        return raw
    }
    if let mapped = mlbTeamShortCode[raw] {
        return mapped
    }
    let firstWord = raw.split(separator: " ").first.map(String.init) ?? raw
    return String(firstWord.prefix(3)).uppercased()
}

/// Full team name → 2–3 char short code. Covers all 30 active MLB teams
/// plus the "Athletics" rebrand. Stick to the most common short forms
/// (matches what ESPN / Baseball Reference use in tables).
private let mlbTeamShortCode: [String: String] = [
    "Arizona Diamondbacks":   "ARI",
    "Atlanta Braves":         "ATL",
    "Baltimore Orioles":      "BAL",
    "Boston Red Sox":         "BOS",
    "Chicago Cubs":           "CHC",
    "Chicago White Sox":      "CWS",
    "Cincinnati Reds":        "CIN",
    "Cleveland Guardians":    "CLE",
    "Colorado Rockies":       "COL",
    "Detroit Tigers":         "DET",
    "Houston Astros":         "HOU",
    "Kansas City Royals":     "KC",
    "Los Angeles Angels":     "LAA",
    "Los Angeles Dodgers":    "LAD",
    "Miami Marlins":          "MIA",
    "Milwaukee Brewers":      "MIL",
    "Minnesota Twins":        "MIN",
    "New York Mets":          "NYM",
    "New York Yankees":       "NYY",
    "Oakland Athletics":      "OAK",
    "Athletics":              "ATH",
    "Philadelphia Phillies":  "PHI",
    "Pittsburgh Pirates":     "PIT",
    "San Diego Padres":       "SD",
    "San Francisco Giants":   "SF",
    "Seattle Mariners":       "SEA",
    "St. Louis Cardinals":    "STL",
    "Tampa Bay Rays":         "TB",
    "Texas Rangers":          "TEX",
    "Toronto Blue Jays":      "TOR",
    "Washington Nationals":   "WSH",
]

// MARK: - Formatters

/// "9/14" — short month/day from the ISO date string.
private func formatGameDate(_ iso: String?) -> String {
    guard let iso else { return "—" }
    let parts = iso.split(separator: "-")
    guard parts.count == 3,
          let m = Int(parts[1]),
          let d = Int(parts[2]) else { return iso }
    return "\(m)/\(d)"
}

/// Parse "YYYY-MM-DD" → (year, month). Returns nil for malformed input.
private func parseYM(_ iso: String?) -> (Int, Int)? {
    guard let iso else { return nil }
    let parts = iso.split(separator: "-")
    guard parts.count == 3,
          let y = Int(parts[0]),
          let m = Int(parts[1]) else { return nil }
    return (y, m)
}

private func monthFullName(_ m: Int, year: Int) -> String {
    let names = ["", "January", "February", "March", "April", "May", "June",
                 "July", "August", "September", "October", "November", "December"]
    let name = (1...12).contains(m) ? names[m] : "?"
    return "\(name) \(String(year))"
}

private func monthShortName(_ m: Int) -> String {
    let names = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                 "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    return (1...12).contains(m) ? names[m] : "?"
}

private func formatInt(_ value: Int?) -> String {
    guard let value else { return "—" }
    return String(value)
}

private func format2(_ value: Double?) -> String {
    guard let value else { return "—" }
    return String(format: "%.2f", value)
}

private func format3(_ value: Double?) -> String {
    guard let value else { return "—" }
    let s = String(format: "%.3f", value)
    if s.hasPrefix("0.")  { return String(s.dropFirst()) }
    if s.hasPrefix("-0.") { return "-" + String(s.dropFirst(2)) }
    return s
}

/// Innings pitched in baseball notation — same logic as
/// PlayerProfileView's formatIP so per-game / monthly / season IP
/// renders consistently across the app. Decimal input (e.g. 1.667)
/// maps to ".0" / ".1" / ".2" by nearest third. Per-game / monthly
/// values here stay well below 1000 so the thousands-separator
/// branch is effectively a no-op, but using the same formatter
/// keeps formatting identical to the season cards.
private func formatIP(_ value: Double?) -> String {
    guard let value else { return "—" }
    let whole = Int(value)
    let frac = value - Double(whole)

    let suffix: String
    if frac < 0.17 {
        suffix = ".0"
    } else if frac < 0.5 {
        suffix = ".1"
    } else {
        suffix = ".2"
    }

    let wholeStr = ipWholeFormatter.string(from: NSNumber(value: whole)) ?? String(whole)
    return wholeStr + suffix
}

private let ipWholeFormatter: NumberFormatter = {
    let f = NumberFormatter()
    f.numberStyle = .decimal
    return f
}()
