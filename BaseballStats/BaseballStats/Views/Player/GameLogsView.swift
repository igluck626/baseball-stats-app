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

    /// Five fixed splits (Last 5/10/15/30, Season) + a Custom row with a
    /// number field. Tapping a row filters the games table below; tapping
    /// the Custom row applies the typed N (live as you type).
    ///
    /// Layout mirrors the games table: the split label is frozen on the
    /// left while the stat columns scroll horizontally on the right. Rate
    /// stats (AVG/OBP/SLG/OPS, ERA) come from each window's own counting
    /// stats — never cumulative season figures.
    private var splitsTable: some View {
        let rows = splitRows
        return HStack(spacing: 0) {
            // Frozen pane — split label only.
            VStack(spacing: 0) {
                splitsFrozenHeader
                Divider()
                ForEach(Array(rows.enumerated()), id: \.offset) { idx, row in
                    splitsFrozenLabelRow(row)
                    if idx != rows.indices.last { Divider().opacity(0.5) }
                }
            }
            .background(.ultraThinMaterial)
            .shadow(color: .black.opacity(0.08), radius: 4, x: 2, y: 0)
            .zIndex(1)

            // Scrollable pane — counting + rate stats.
            ScrollView(.horizontal, showsIndicators: false) {
                VStack(spacing: 0) {
                    splitsScrollableHeader
                    Divider()
                    ForEach(Array(rows.enumerated()), id: \.offset) { idx, row in
                        splitsScrollableStatRow(row)
                        if idx != rows.indices.last { Divider().opacity(0.5) }
                    }
                }
            }
        }
        // Stretch to parent width so the card aligns left/right with the
        // Overview cards (which also use .frame(maxWidth: .infinity)
        // before their material background).
        .frame(maxWidth: .infinity)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 16))
    }

    /// Order in which split rows are rendered.
    private var splitRows: [SplitRow] {
        [.last(5), .last(10), .last(15), .last(30), .season, .custom]
    }

    private var splitsFrozenHeader: some View {
        HStack(spacing: 0) {
            Text("Splits")
                .frame(width: SplitsLayout.label, alignment: .leading)
        }
        .font(.caption.weight(.semibold))
        .foregroundStyle(.secondary)
        .padding(.leading, 12)
        .frame(height: SplitsLayout.rowHeight)
    }

    @ViewBuilder
    private var splitsScrollableHeader: some View {
        HStack(spacing: 0) {
            if isPitcher {
                splitsHeaderCell("G",   width: SplitsLayout.g)
                splitsHeaderCell("IP",  width: SplitsLayout.ip)
                splitsHeaderCell("H",   width: SplitsLayout.h)
                splitsHeaderCell("R",   width: SplitsLayout.r)
                splitsHeaderCell("ER",  width: SplitsLayout.er)
                splitsHeaderCell("BB",  width: SplitsLayout.bb)
                splitsHeaderCell("SO",  width: SplitsLayout.so)
                splitsHeaderCell("HR",  width: SplitsLayout.hr)
                splitsHeaderCell("ERA", width: SplitsLayout.era)
            } else {
                splitsHeaderCell("G",   width: SplitsLayout.g)
                splitsHeaderCell("AB",  width: SplitsLayout.ab)
                splitsHeaderCell("H",   width: SplitsLayout.h)
                splitsHeaderCell("HR",  width: SplitsLayout.hr)
                splitsHeaderCell("RBI", width: SplitsLayout.rbi)
                splitsHeaderCell("BB",  width: SplitsLayout.bb)
                splitsHeaderCell("SO",  width: SplitsLayout.so)
                splitsHeaderCell("SB",  width: SplitsLayout.sb)
                splitsHeaderCell("AVG", width: SplitsLayout.rate)
                splitsHeaderCell("OBP", width: SplitsLayout.rate)
                splitsHeaderCell("SLG", width: SplitsLayout.rate)
                splitsHeaderCell("OPS", width: SplitsLayout.rate)
            }
        }
        .font(.caption.weight(.semibold))
        .foregroundStyle(.secondary)
        .padding(.trailing, 12)
        .frame(height: SplitsLayout.rowHeight)
    }

    private func splitsHeaderCell(_ label: String, width: CGFloat) -> some View {
        Text(label)
            .frame(width: width, alignment: .trailing)
            .padding(.horizontal, 2)
    }

    /// Frozen-side label cell for one split row. Selectable; the matching
    /// scrollable side picks up the same selection background so the row
    /// reads as one continuous tap target across both panes.
    private func splitsFrozenLabelRow(_ row: SplitRow) -> some View {
        let isSelected = selectedRow == row
        return HStack(spacing: 0) {
            Text(row.label)
                .fontWeight(isSelected ? .semibold : .regular)
                .lineLimit(1)
                .fixedSize()
                .frame(width: SplitsLayout.label, alignment: .leading)
        }
        .font(.caption)
        .padding(.leading, 12)
        .frame(height: SplitsLayout.rowHeight)
        .background(isSelected ? Color.accentColor.opacity(0.18) : Color.clear)
        .contentShape(Rectangle())
        .onTapGesture { handleSplitTap(row) }
    }

    /// Scrollable-side stat cells for one split row.
    @ViewBuilder
    private func splitsScrollableStatRow(_ row: SplitRow) -> some View {
        let snapshot = snapshot(for: row)
        let isSelected = selectedRow == row
        HStack(spacing: 0) {
            // For Custom, the G slot is a TextField the user types into.
            if row == .custom {
                customGCell()
            } else {
                statCell(formatInt(snapshot.g), width: SplitsLayout.g)
            }
            scrollableStatCellsAfterG(snapshot)
        }
        .font(.caption)
        .padding(.trailing, 12)
        .frame(height: SplitsLayout.rowHeight)
        .background(isSelected ? Color.accentColor.opacity(0.18) : Color.clear)
        .contentShape(Rectangle())
        .onTapGesture { handleSplitTap(row) }
    }

    @ViewBuilder
    private func scrollableStatCellsAfterG(_ s: WindowSnapshot) -> some View {
        if isPitcher {
            statCell(formatIP(s.ip),    width: SplitsLayout.ip)
            statCell(formatInt(s.h),    width: SplitsLayout.h)
            statCell(formatInt(s.r),    width: SplitsLayout.r)
            statCell(formatInt(s.er),   width: SplitsLayout.er)
            statCell(formatInt(s.bb),   width: SplitsLayout.bb)
            statCell(formatInt(s.so),   width: SplitsLayout.so)
            statCell(formatInt(s.hr),   width: SplitsLayout.hr)
            statCell(format2(s.era),    width: SplitsLayout.era)
        } else {
            statCell(formatInt(s.ab),   width: SplitsLayout.ab)
            statCell(formatInt(s.h),    width: SplitsLayout.h)
            statCell(formatInt(s.hr),   width: SplitsLayout.hr)
            statCell(formatInt(s.rbi),  width: SplitsLayout.rbi)
            statCell(formatInt(s.bb),   width: SplitsLayout.bb)
            statCell(formatInt(s.so),   width: SplitsLayout.so)
            statCell(formatInt(s.sb),   width: SplitsLayout.sb)
            statCell(format3(s.avg),    width: SplitsLayout.rate)
            statCell(format3(s.obp),    width: SplitsLayout.rate)
            statCell(format3(s.slg),    width: SplitsLayout.rate)
            statCell(format3(s.ops),    width: SplitsLayout.rate)
        }
    }

    private func statCell(_ text: String, width: CGFloat) -> some View {
        Text(text)
            .frame(width: width, alignment: .trailing)
            .monospacedDigit()
            .padding(.horizontal, 2)
    }

    /// TextField rendered to look like a tappable cell — borderless,
    /// soft systemGray6 background, right-aligned monospaced digits so
    /// it lines up visually with the integer G values in the rows above.
    private func customGCell() -> some View {
        TextField("#", text: $customInput)
            .keyboardType(.numberPad)
            .multilineTextAlignment(.trailing)
            .textFieldStyle(.plain)
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

    /// Tap behaviour: tapping an already-active Last-N row clears back
    /// to Season; tapping any other row selects it.
    private func handleSplitTap(_ row: SplitRow) {
        withAnimation(.easeInOut(duration: 0.15)) {
            if case .last = row, selectedRow == row {
                selectedRow = .season
            } else {
                selectedRow = row
            }
        }
    }

    /// Pulls the right `GameLogWindow` off the API response for fixed
    /// rows, then funnels into a uniform `WindowSnapshot` for display.
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
        // Stretch to parent width — same reasoning as splitsTable.
        .frame(maxWidth: .infinity)
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

    @ViewBuilder
    private func flatGameTable(games: [GameLog]) -> some View {
        // Filtered windows (Last N / Custom) are a subset, not a
        // chronological prefix of the season — a cumulative rate through
        // that game would be misleading. Pass `.empty` / nil so the rate
        // columns render "—".
        if isPitcher {
            PitchingGameLogTable(
                rows: games.enumerated().map { index, game in
                    GameWithCumulative(game: game, battingRates: .empty, cumulativeERA: nil)
                },
                groups: nil
            )
        } else {
            BattingGameLogTable(
                rows: games.enumerated().map { index, game in
                    GameWithCumulative(game: game, battingRates: .empty, cumulativeERA: nil)
                },
                groups: nil
            )
        }
    }

    @ViewBuilder
    private func monthGroupedTable(allGames: [GameLog]) -> some View {
        let groups = monthGroups(allGames: allGames)
        if isPitcher {
            PitchingGameLogTable(rows: nil, groups: groups)
        } else {
            BattingGameLogTable(rows: nil, groups: groups)
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

    /// Walk `allGames` (reverse-chrono) in chronological order, pair each
    /// game with the running cumulative AVG (batters) or ERA (pitchers)
    /// through that game, group by (year, month), and compute the
    /// season-to-date totals snapshot through the end of each month for
    /// the totals row. Returns the groups in display order (most recent
    /// month first), with games inside each group in reverse-chrono order.
    private func monthGroups(allGames: [GameLog]) -> [MonthGroup] {
        let chrono = Array(allGames.reversed())

        // Walk chronologically once, accumulating the rate-stat
        // numerator/denominator components and emitting each game's
        // cumulative rate(s). Baseball Reference's game logs show the
        // rate stats (AVG/OBP/SLG/OPS, ERA) as "season-to-date through
        // this game", which is what this loop computes — not the
        // per-game H/AB or ER*9/IP.
        //
        // For batters we track the four standard slash-line components
        // and derive AVG/OBP/SLG/OPS from running totals. For pitchers
        // we just need cumulative ERA (ER × 9 / IP).
        var sumAB  = 0
        var sumH   = 0
        var sumDbl = 0
        var sumTrp = 0
        var sumHR  = 0
        var sumBB  = 0
        var sumHBP = 0
        var sumSF  = 0
        var sumIP  = 0.0
        var sumER  = 0
        var chronoWithCumulative: [GameWithCumulative] = []
        for g in chrono {
            if isPitcher {
                sumIP += g.IP ?? 0
                sumER += g.ER ?? 0
                let era: Double? = sumIP > 0 ? Double(sumER) * 9.0 / sumIP : nil
                chronoWithCumulative.append(GameWithCumulative(
                    game: g, battingRates: .empty, cumulativeERA: era
                ))
            } else {
                sumAB  += g.AB ?? 0
                sumH   += g.H  ?? 0
                sumDbl += g.doubles ?? 0
                sumTrp += g.triples ?? 0
                sumHR  += g.HR ?? 0
                sumBB  += g.BB ?? 0
                sumHBP += g.HBP ?? 0
                sumSF  += g.SF ?? 0

                let singles = sumH - sumDbl - sumTrp - sumHR
                let avg: Double? = sumAB > 0 ? Double(sumH) / Double(sumAB) : nil
                let obpDen = sumAB + sumBB + sumHBP + sumSF
                let obp: Double? = obpDen > 0
                    ? Double(sumH + sumBB + sumHBP) / Double(obpDen) : nil
                let slg: Double? = sumAB > 0
                    ? Double(singles + 2 * sumDbl + 3 * sumTrp + 4 * sumHR) / Double(sumAB)
                    : nil
                let ops: Double? = (obp != nil && slg != nil) ? (obp! + slg!) : nil
                chronoWithCumulative.append(GameWithCumulative(
                    game: g,
                    battingRates: CumulativeBattingRates(avg: avg, obp: obp, slg: slg, ops: ops),
                    cumulativeERA: nil
                ))
            }
        }

        var buckets: [(year: Int, month: Int, games: [GameWithCumulative])] = []
        for gc in chronoWithCumulative {
            guard let ym = parseYM(gc.game.game_date) else { continue }
            if let last = buckets.last, last.year == ym.0, last.month == ym.1 {
                buckets[buckets.count - 1].games.append(gc)
            } else {
                buckets.append((ym.0, ym.1, [gc]))
            }
        }

        var out: [MonthGroup] = []
        for bucket in buckets {
            let bucketGames = bucket.games.map { $0.game }
            let monthly: WindowSnapshot = isPitcher
                ? .computePitching(games: bucketGames)
                : .computeBatting(games: bucketGames)
            out.append(MonthGroup(
                year: bucket.year,
                month: bucket.month,
                games: bucket.games.reversed(),  // reverse-chrono within month
                monthlyTotals: monthly
            ))
        }
        return out.reversed()
    }
}

// MARK: - Month section label

/// Lightweight per-month section divider — plain muted text, no capsule
/// or background. The section label only renders in the frozen pane;
/// the scrollable pane uses an equally-tall spacer (`MonthSectionSpacer`)
/// so rows below stay aligned across the two sides.
private struct MonthSectionLabel: View {
    let label: String
    var body: some View {
        Text(label)
            .font(.caption.weight(.semibold))
            .foregroundStyle(.secondary)
            .padding(.leading, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .frame(height: monthSectionLabelHeight)
    }
}

/// Empty spacer that occupies the same row height as `MonthSectionLabel`
/// so the scrollable pane's per-month sections start at the same Y as
/// the frozen pane's.
private struct MonthSectionSpacer: View {
    var body: some View {
        Color.clear.frame(height: monthSectionLabelHeight)
    }
}

private let monthSectionLabelHeight: CGFloat = 26

// MARK: - Month group model

private struct MonthGroup: Identifiable {
    let year: Int
    let month: Int
    /// Games in reverse-chrono order within this month, each paired with
    /// the season-to-date AVG (batters) or ERA (pitchers) through that
    /// individual game.
    let games: [GameWithCumulative]
    /// Stats summed over just this month's games — including AVG/OBP/SLG/
    /// OPS (batters) or ERA (pitchers) computed from this month's
    /// counting stats. Drives the monthly totals row.
    let monthlyTotals: WindowSnapshot

    var id: String { "\(year)-\(month)" }
}

/// Cumulative batting rate stats through a single game. Each value is
/// the full season-to-date rate up to and including that game — what
/// Baseball Reference and MLB.com show in their game-log rate columns.
private struct CumulativeBattingRates {
    let avg: Double?
    let obp: Double?
    let slg: Double?
    let ops: Double?
    static let empty = CumulativeBattingRates(avg: nil, obp: nil, slg: nil, ops: nil)
}

/// One row of the games table — the raw game paired with the cumulative
/// rate stats through that game. Filtered subset views (Last N / Custom)
/// pass the `.empty` rates / `nil` ERA so the rate columns render "—".
private struct GameWithCumulative {
    let game: GameLog
    let battingRates: CumulativeBattingRates
    let cumulativeERA: Double?
}

// MARK: - Splits column widths

// Frozen pane = label only; everything else scrolls horizontally to the
// right. Same header/cell pattern as the games table (career-table
// style frozen + scrollable split).
//
// Batting scrollable intrinsic (with 4pt h-padding per cell):
//   G 36 + AB 30 + H 26 + HR 26 + RBI 32 + BB 26 + SO 26 + SB 26 +
//   AVG 44 + OBP 44 + SLG 44 + OPS 44 = 404 → wider than screen, scrolls.
// Pitching scrollable intrinsic:
//   G 36 + IP 38 + H 26 + R 26 + ER 28 + BB 26 + SO 26 + HR 26 + ERA 44
//   = 276 → fits without scrolling on iPhone.
private enum SplitsLayout {
    static let rowHeight: CGFloat = 36
    static let label:     CGFloat = 80

    // Counting (shared)
    static let g:   CGFloat = 36
    static let ab:  CGFloat = 30
    static let h:   CGFloat = 26
    static let hr:  CGFloat = 26
    static let rbi: CGFloat = 32
    static let bb:  CGFloat = 26
    static let so:  CGFloat = 26
    static let sb:  CGFloat = 26
    // Pitching extras
    static let ip:  CGFloat = 38
    static let r:   CGFloat = 26
    static let er:  CGFloat = 28
    // Rate stats
    static let rate: CGFloat = 44
    static let era:  CGFloat = 44
}

// MARK: - Window snapshot

/// Uniform shape consumed by both the splits table and the monthly
/// totals row. Either source (API window or local games-array agg)
/// produces one of these so the renderer doesn't branch on origin.
private struct WindowSnapshot {
    var g: Int?
    // Batting
    var ab: Int?
    var r: Int?
    var h: Int?
    var doubles: Int?
    var triples: Int?
    var tb: Int?
    var hr: Int?
    var rbi: Int?
    var bb: Int?
    var ibb: Int?
    var so: Int?
    var sb: Int?
    var cs: Int?
    var avg: Double?
    var obp: Double?
    var slg: Double?
    var ops: Double?
    // Pitching
    var ip: Double?
    var er: Int?
    var hbp: Int?
    var era: Double?
    var whip: Double?
    var kPer9: Double?

    static let empty = WindowSnapshot()

    /// Adapter from the API's GameLogWindow. The backend currently
    /// surfaces a subset of batting fields per window; the rest stay nil.
    static func from(_ window: GameLogWindow?) -> WindowSnapshot {
        WindowSnapshot(
            g: window?.G,
            ab: window?.AB,
            r: window?.R,
            h: window?.H,
            hr: window?.HR,
            rbi: window?.RBI,
            bb: window?.BB,
            so: window?.SO,
            avg: window?.BA,
            obp: window?.OBP,
            slg: window?.SLG,
            ops: window?.OPS,
            ip: window?.IP,
            er: window?.ER,
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
        let r   = games.reduce(0) { $0 + ($1.R ?? 0) }
        let h   = games.reduce(0) { $0 + ($1.H ?? 0) }
        let dbl = games.reduce(0) { $0 + ($1.doubles ?? 0) }
        let trp = games.reduce(0) { $0 + ($1.triples ?? 0) }
        let hr  = games.reduce(0) { $0 + ($1.HR ?? 0) }
        let rbi = games.reduce(0) { $0 + ($1.RBI ?? 0) }
        let bb  = games.reduce(0) { $0 + ($1.BB ?? 0) }
        let ibb = games.reduce(0) { $0 + ($1.IBB ?? 0) }
        let so  = games.reduce(0) { $0 + ($1.SO ?? 0) }
        let sb  = games.reduce(0) { $0 + ($1.SB ?? 0) }
        let cs  = games.reduce(0) { $0 + ($1.CS ?? 0) }
        let hbp = games.reduce(0) { $0 + ($1.HBP ?? 0) }
        let sf  = games.reduce(0) { $0 + ($1.SF ?? 0) }

        let singles = h - dbl - trp - hr
        let tb = singles + 2 * dbl + 3 * trp + 4 * hr
        let avg: Double? = ab > 0 ? Double(h) / Double(ab) : nil
        let obpDen = ab + bb + hbp + sf
        let obp: Double? = obpDen > 0 ? Double(h + bb + hbp) / Double(obpDen) : nil
        let slg: Double? = ab > 0 ? Double(tb) / Double(ab) : nil
        let ops: Double? = (obp != nil && slg != nil) ? (obp! + slg!) : nil

        return WindowSnapshot(
            g: g,
            ab: ab, r: r, h: h, doubles: dbl, triples: trp, tb: tb,
            hr: hr, rbi: rbi, bb: bb, ibb: ibb, so: so, sb: sb, cs: cs,
            avg: avg, obp: obp, slg: slg, ops: ops
        )
    }

    /// Aggregate pitching stats over a games slice.
    static func computePitching(games: [GameLog]) -> WindowSnapshot {
        guard !games.isEmpty else { return .empty }
        let g  = games.count
        let ip = games.reduce(0.0) { $0 + ($1.IP ?? 0) }
        let h  = games.reduce(0)   { $0 + ($1.H  ?? 0) }
        let r  = games.reduce(0)   { $0 + ($1.R  ?? 0) }
        let er = games.reduce(0)   { $0 + ($1.ER ?? 0) }
        let bb = games.reduce(0)   { $0 + ($1.BB ?? 0) }
        let so = games.reduce(0)   { $0 + ($1.SO ?? 0) }
        let hr = games.reduce(0)   { $0 + ($1.HR ?? 0) }
        let hbp = games.reduce(0)  { $0 + ($1.HBP ?? 0) }

        let era:    Double? = ip > 0 ? Double(er) * 9 / ip : nil
        let whip:   Double? = ip > 0 ? Double(bb + h) / ip : nil
        let kPer9:  Double? = ip > 0 ? Double(so) * 9 / ip : nil

        return WindowSnapshot(
            g: g,
            r: r, h: h, hr: hr, bb: bb, so: so,
            ip: ip, er: er, hbp: hbp,
            era: era, whip: whip, kPer9: kPer9
        )
    }
}

// MARK: - Game table layout

// Frozen-pane layout: Date / Opp / Result on the left stay put; the
// remaining counting + rate columns scroll horizontally. Mirrors the
// career table's split-pane structure in PlayerProfileView.
//
// Column widths for the batting game table. Frozen pane = Date + Opp;
// the rest scroll horizontally. Result/IBB/CS are intentionally absent
// for now: Result isn't reliably populated, IBB/CS are still backfilling
// after their column was added to batting_gamelogs.
private enum BattingGameColumn {
    static let date: CGFloat = 48
    static let opp:  CGFloat = 56
    static let ab:   CGFloat = 26
    static let r:    CGFloat = 24
    static let h:    CGFloat = 24
    static let tb:   CGFloat = 28
    static let dbl:  CGFloat = 26
    static let trp:  CGFloat = 26
    static let hr:   CGFloat = 26
    static let rbi:  CGFloat = 30
    static let bb:   CGFloat = 26
    static let so:   CGFloat = 26
    static let sb:   CGFloat = 26
    static let avg:  CGFloat = 44
    static let obp:  CGFloat = 44
    static let slg:  CGFloat = 44
    static let ops:  CGFloat = 44
}

// Same shape for pitching. Result column is intentionally absent (not
// reliably populated yet).
private enum PitchingGameColumn {
    static let date: CGFloat = 48
    static let opp:  CGFloat = 56
    static let ip:   CGFloat = 36
    static let h:    CGFloat = 24
    static let r:    CGFloat = 24
    static let er:   CGFloat = 26
    static let bb:   CGFloat = 26
    static let so:   CGFloat = 26
    static let hr:   CGFloat = 26
    static let hbp:  CGFloat = 30
    static let era:  CGFloat = 44
}

// MARK: - Batting game-log table

/// Batting game-log table — frozen Date/Opp on the left, the counting
/// and rate columns scrolling horizontally on the right. Drives either
/// a flat list (when `rows` is set, used for Last N / Custom) or the
/// season-grouped layout (when `groups` is set). Exactly one input is
/// non-nil — the caller picks based on the active filter row.
///
/// Section layout (groups mode): every month gets its own column-header
/// row above its games so the user always sees what column they're
/// looking at without scrolling back to the top.
private struct BattingGameLogTable: View {
    let rows: [GameWithCumulative]?
    let groups: [MonthGroup]?

    var body: some View {
        HStack(spacing: 0) {
            // Frozen pane.
            VStack(spacing: 0) {
                if let groups {
                    ForEach(Array(groups.enumerated()), id: \.element.id) { gIdx, group in
                        if gIdx > 0 { Divider() }
                        MonthSectionLabel(label: monthFullName(group.month, year: group.year))
                        BattingFrozenHeader()
                        Divider()
                        ForEach(Array(group.games.enumerated()), id: \.offset) { idx, gc in
                            BattingFrozenGameRow(game: gc.game, alternate: !idx.isMultiple(of: 2))
                            if idx != group.games.indices.last { Divider().opacity(0.25) }
                        }
                        BattingFrozenMonthTotalsRow(group: group)
                    }
                }
                if let rows {
                    BattingFrozenHeader()
                    Divider()
                    ForEach(Array(rows.enumerated()), id: \.offset) { idx, gc in
                        BattingFrozenGameRow(game: gc.game, alternate: !idx.isMultiple(of: 2))
                        if idx != rows.indices.last { Divider().opacity(0.25) }
                    }
                }
            }
            .background(.ultraThinMaterial)
            .shadow(color: .black.opacity(0.08), radius: 4, x: 2, y: 0)
            .zIndex(1)

            // Scrollable pane — must mirror the frozen pane row-for-row
            // so heights line up across both sides.
            ScrollView(.horizontal, showsIndicators: false) {
                VStack(spacing: 0) {
                    if let groups {
                        ForEach(Array(groups.enumerated()), id: \.element.id) { gIdx, group in
                            if gIdx > 0 { Divider() }
                            MonthSectionSpacer()
                            BattingScrollableHeader()
                            Divider()
                            ForEach(Array(group.games.enumerated()), id: \.offset) { idx, gc in
                                BattingScrollableGameRow(
                                    game: gc.game,
                                    rates: gc.battingRates,
                                    alternate: !idx.isMultiple(of: 2)
                                )
                                if idx != group.games.indices.last { Divider().opacity(0.25) }
                            }
                            BattingScrollableMonthTotalsRow(group: group)
                        }
                    }
                    if let rows {
                        BattingScrollableHeader()
                        Divider()
                        ForEach(Array(rows.enumerated()), id: \.offset) { idx, gc in
                            BattingScrollableGameRow(
                                game: gc.game,
                                rates: gc.battingRates,
                                alternate: !idx.isMultiple(of: 2)
                            )
                            if idx != rows.indices.last { Divider().opacity(0.25) }
                        }
                    }
                }
            }
        }
    }
}

private struct BattingFrozenHeader: View {
    var body: some View {
        HStack(spacing: 0) {
            Text("Date").frame(width: BattingGameColumn.date, alignment: .leading)
            Text("Opp") .frame(width: BattingGameColumn.opp,  alignment: .leading)
        }
        .font(.caption.weight(.semibold))
        .foregroundStyle(.secondary)
        .padding(.leading, 12)
        .padding(.vertical, 8)
        .frame(height: 32)
    }
}

private struct BattingScrollableHeader: View {
    var body: some View {
        HStack(spacing: 0) {
            Text("AB") .frame(width: BattingGameColumn.ab,  alignment: .trailing).padding(.horizontal, 2)
            Text("R")  .frame(width: BattingGameColumn.r,   alignment: .trailing).padding(.horizontal, 2)
            Text("H")  .frame(width: BattingGameColumn.h,   alignment: .trailing).padding(.horizontal, 2)
            Text("TB") .frame(width: BattingGameColumn.tb,  alignment: .trailing).padding(.horizontal, 2)
            Text("2B") .frame(width: BattingGameColumn.dbl, alignment: .trailing).padding(.horizontal, 2)
            Text("3B") .frame(width: BattingGameColumn.trp, alignment: .trailing).padding(.horizontal, 2)
            Text("HR") .frame(width: BattingGameColumn.hr,  alignment: .trailing).padding(.horizontal, 2)
            Text("RBI").frame(width: BattingGameColumn.rbi, alignment: .trailing).padding(.horizontal, 2)
            Text("BB") .frame(width: BattingGameColumn.bb,  alignment: .trailing).padding(.horizontal, 2)
            Text("SO") .frame(width: BattingGameColumn.so,  alignment: .trailing).padding(.horizontal, 2)
            Text("SB") .frame(width: BattingGameColumn.sb,  alignment: .trailing).padding(.horizontal, 2)
            Text("AVG").frame(width: BattingGameColumn.avg, alignment: .trailing).padding(.horizontal, 2)
            Text("OBP").frame(width: BattingGameColumn.obp, alignment: .trailing).padding(.horizontal, 2)
            Text("SLG").frame(width: BattingGameColumn.slg, alignment: .trailing).padding(.horizontal, 2)
            Text("OPS").frame(width: BattingGameColumn.ops, alignment: .trailing).padding(.horizontal, 2)
        }
        .font(.caption.weight(.semibold))
        .foregroundStyle(.secondary)
        .padding(.trailing, 12)
        .padding(.vertical, 8)
        .frame(height: 32)
    }
}

private struct BattingFrozenGameRow: View {
    let game: GameLog
    let alternate: Bool
    var body: some View {
        HStack(spacing: 0) {
            Text(formatGameDate(game.game_date))
                .frame(width: BattingGameColumn.date, alignment: .leading)
                .monospacedDigit()
            opponentLabel(game)
                .frame(width: BattingGameColumn.opp, alignment: .leading)
        }
        .font(.caption)
        .padding(.leading, 12)
        .padding(.vertical, 7)
        .frame(height: 30)
        .background(alternate ? Color(.systemGray6).opacity(0.5) : Color.clear)
    }
}

private struct BattingScrollableGameRow: View {
    let game: GameLog
    let rates: CumulativeBattingRates
    let alternate: Bool
    var body: some View {
        HStack(spacing: 0) {
            Text(formatInt(game.AB))        .frame(width: BattingGameColumn.ab,  alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(game.R))         .frame(width: BattingGameColumn.r,   alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(game.H))         .frame(width: BattingGameColumn.h,   alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(perGameTB(game))).frame(width: BattingGameColumn.tb,  alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(game.doubles))   .frame(width: BattingGameColumn.dbl, alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(game.triples))   .frame(width: BattingGameColumn.trp, alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(game.HR))        .frame(width: BattingGameColumn.hr,  alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(game.RBI))       .frame(width: BattingGameColumn.rbi, alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(game.BB))        .frame(width: BattingGameColumn.bb,  alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(game.SO))        .frame(width: BattingGameColumn.so,  alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(game.SB))        .frame(width: BattingGameColumn.sb,  alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(format3(rates.avg))        .frame(width: BattingGameColumn.avg, alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(format3(rates.obp))        .frame(width: BattingGameColumn.obp, alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(format3(rates.slg))        .frame(width: BattingGameColumn.slg, alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(format3(rates.ops))        .frame(width: BattingGameColumn.ops, alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
        }
        .font(.caption)
        .padding(.trailing, 12)
        .padding(.vertical, 7)
        .frame(height: 30)
        .background(alternate ? Color(.systemGray6).opacity(0.5) : Color.clear)
    }
}

private struct BattingFrozenMonthTotalsRow: View {
    let group: MonthGroup
    var body: some View {
        HStack(spacing: 0) {
            Text(monthShortName(group.month))
                .frame(width: BattingGameColumn.date, alignment: .leading)
            Text("")
                .frame(width: BattingGameColumn.opp, alignment: .leading)
        }
        .font(.caption.weight(.semibold))
        .padding(.leading, 12)
        .padding(.vertical, 8)
        .frame(height: 32)
        .background(Color(.systemGray5).opacity(0.7))
        .overlay(alignment: .top) { Divider() }
    }
}

private struct BattingScrollableMonthTotalsRow: View {
    let group: MonthGroup
    var body: some View {
        let m = group.monthlyTotals
        HStack(spacing: 0) {
            Text(formatInt(m.ab))     .frame(width: BattingGameColumn.ab,  alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(m.r))      .frame(width: BattingGameColumn.r,   alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(m.h))      .frame(width: BattingGameColumn.h,   alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(m.tb))     .frame(width: BattingGameColumn.tb,  alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(m.doubles)).frame(width: BattingGameColumn.dbl, alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(m.triples)).frame(width: BattingGameColumn.trp, alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(m.hr))     .frame(width: BattingGameColumn.hr,  alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(m.rbi))    .frame(width: BattingGameColumn.rbi, alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(m.bb))     .frame(width: BattingGameColumn.bb,  alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(m.so))     .frame(width: BattingGameColumn.so,  alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(m.sb))     .frame(width: BattingGameColumn.sb,  alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            // Rates computed from this month's counting stats only — not
            // season-to-date through the month.
            Text(format3(m.avg)).frame(width: BattingGameColumn.avg, alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(format3(m.obp)).frame(width: BattingGameColumn.obp, alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(format3(m.slg)).frame(width: BattingGameColumn.slg, alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(format3(m.ops)).frame(width: BattingGameColumn.ops, alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
        }
        .font(.caption.weight(.semibold))
        .padding(.trailing, 12)
        .padding(.vertical, 8)
        .frame(height: 32)
        .background(Color(.systemGray5).opacity(0.7))
        .overlay(alignment: .top) { Divider() }
    }
}

// MARK: - Pitching game-log table

/// Pitching equivalent of `BattingGameLogTable`. Same per-month
/// section-label + repeated-column-header pattern as batting.
private struct PitchingGameLogTable: View {
    let rows: [GameWithCumulative]?
    let groups: [MonthGroup]?

    var body: some View {
        HStack(spacing: 0) {
            VStack(spacing: 0) {
                if let groups {
                    ForEach(Array(groups.enumerated()), id: \.element.id) { gIdx, group in
                        if gIdx > 0 { Divider() }
                        MonthSectionLabel(label: monthFullName(group.month, year: group.year))
                        PitchingFrozenHeader()
                        Divider()
                        ForEach(Array(group.games.enumerated()), id: \.offset) { idx, gc in
                            PitchingFrozenGameRow(game: gc.game, alternate: !idx.isMultiple(of: 2))
                            if idx != group.games.indices.last { Divider().opacity(0.25) }
                        }
                        PitchingFrozenMonthTotalsRow(group: group)
                    }
                }
                if let rows {
                    PitchingFrozenHeader()
                    Divider()
                    ForEach(Array(rows.enumerated()), id: \.offset) { idx, gc in
                        PitchingFrozenGameRow(game: gc.game, alternate: !idx.isMultiple(of: 2))
                        if idx != rows.indices.last { Divider().opacity(0.25) }
                    }
                }
            }
            .background(.ultraThinMaterial)
            .shadow(color: .black.opacity(0.08), radius: 4, x: 2, y: 0)
            .zIndex(1)

            ScrollView(.horizontal, showsIndicators: false) {
                VStack(spacing: 0) {
                    if let groups {
                        ForEach(Array(groups.enumerated()), id: \.element.id) { gIdx, group in
                            if gIdx > 0 { Divider() }
                            MonthSectionSpacer()
                            PitchingScrollableHeader()
                            Divider()
                            ForEach(Array(group.games.enumerated()), id: \.offset) { idx, gc in
                                PitchingScrollableGameRow(
                                    game: gc.game,
                                    cumulativeERA: gc.cumulativeERA,
                                    alternate: !idx.isMultiple(of: 2)
                                )
                                if idx != group.games.indices.last { Divider().opacity(0.25) }
                            }
                            PitchingScrollableMonthTotalsRow(group: group)
                        }
                    }
                    if let rows {
                        PitchingScrollableHeader()
                        Divider()
                        ForEach(Array(rows.enumerated()), id: \.offset) { idx, gc in
                            PitchingScrollableGameRow(
                                game: gc.game,
                                cumulativeERA: gc.cumulativeERA,
                                alternate: !idx.isMultiple(of: 2)
                            )
                            if idx != rows.indices.last { Divider().opacity(0.25) }
                        }
                    }
                }
            }
        }
    }
}

private struct PitchingFrozenHeader: View {
    var body: some View {
        HStack(spacing: 0) {
            Text("Date").frame(width: PitchingGameColumn.date, alignment: .leading)
            Text("Opp") .frame(width: PitchingGameColumn.opp,  alignment: .leading)
        }
        .font(.caption.weight(.semibold))
        .foregroundStyle(.secondary)
        .padding(.leading, 12)
        .padding(.vertical, 8)
        .frame(height: 32)
    }
}

private struct PitchingScrollableHeader: View {
    var body: some View {
        HStack(spacing: 0) {
            Text("IP") .frame(width: PitchingGameColumn.ip,  alignment: .trailing).padding(.horizontal, 2)
            Text("H")  .frame(width: PitchingGameColumn.h,   alignment: .trailing).padding(.horizontal, 2)
            Text("R")  .frame(width: PitchingGameColumn.r,   alignment: .trailing).padding(.horizontal, 2)
            Text("ER") .frame(width: PitchingGameColumn.er,  alignment: .trailing).padding(.horizontal, 2)
            Text("BB") .frame(width: PitchingGameColumn.bb,  alignment: .trailing).padding(.horizontal, 2)
            Text("SO") .frame(width: PitchingGameColumn.so,  alignment: .trailing).padding(.horizontal, 2)
            Text("HR") .frame(width: PitchingGameColumn.hr,  alignment: .trailing).padding(.horizontal, 2)
            Text("HBP").frame(width: PitchingGameColumn.hbp, alignment: .trailing).padding(.horizontal, 2)
            Text("ERA").frame(width: PitchingGameColumn.era, alignment: .trailing).padding(.horizontal, 2)
        }
        .font(.caption.weight(.semibold))
        .foregroundStyle(.secondary)
        .padding(.trailing, 12)
        .padding(.vertical, 8)
        .frame(height: 32)
    }
}

private struct PitchingFrozenGameRow: View {
    let game: GameLog
    let alternate: Bool
    var body: some View {
        HStack(spacing: 0) {
            Text(formatGameDate(game.game_date))
                .frame(width: PitchingGameColumn.date, alignment: .leading)
                .monospacedDigit()
            opponentLabel(game)
                .frame(width: PitchingGameColumn.opp, alignment: .leading)
        }
        .font(.caption)
        .padding(.leading, 12)
        .padding(.vertical, 7)
        .frame(height: 30)
        .background(alternate ? Color(.systemGray6).opacity(0.5) : Color.clear)
    }
}

private struct PitchingScrollableGameRow: View {
    let game: GameLog
    let cumulativeERA: Double?
    let alternate: Bool
    var body: some View {
        HStack(spacing: 0) {
            Text(formatIP(game.IP)) .frame(width: PitchingGameColumn.ip,  alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(game.H)) .frame(width: PitchingGameColumn.h,   alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(game.R)) .frame(width: PitchingGameColumn.r,   alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(game.ER)).frame(width: PitchingGameColumn.er,  alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(game.BB)).frame(width: PitchingGameColumn.bb,  alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(game.SO)).frame(width: PitchingGameColumn.so,  alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(game.HR)).frame(width: PitchingGameColumn.hr,  alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(game.HBP)).frame(width: PitchingGameColumn.hbp, alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(format2(cumulativeERA)).frame(width: PitchingGameColumn.era, alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
        }
        .font(.caption)
        .padding(.trailing, 12)
        .padding(.vertical, 7)
        .frame(height: 30)
        .background(alternate ? Color(.systemGray6).opacity(0.5) : Color.clear)
    }
}

private struct PitchingFrozenMonthTotalsRow: View {
    let group: MonthGroup
    var body: some View {
        HStack(spacing: 0) {
            Text(monthShortName(group.month))
                .frame(width: PitchingGameColumn.date, alignment: .leading)
            Text("")
                .frame(width: PitchingGameColumn.opp, alignment: .leading)
        }
        .font(.caption.weight(.semibold))
        .padding(.leading, 12)
        .padding(.vertical, 8)
        .frame(height: 32)
        .background(Color(.systemGray5).opacity(0.7))
        .overlay(alignment: .top) { Divider() }
    }
}

private struct PitchingScrollableMonthTotalsRow: View {
    let group: MonthGroup
    var body: some View {
        let m = group.monthlyTotals
        HStack(spacing: 0) {
            Text(formatIP(m.ip))     .frame(width: PitchingGameColumn.ip,  alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(m.h))     .frame(width: PitchingGameColumn.h,   alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(m.r))     .frame(width: PitchingGameColumn.r,   alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(m.er))    .frame(width: PitchingGameColumn.er,  alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(m.bb))    .frame(width: PitchingGameColumn.bb,  alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(m.so))    .frame(width: PitchingGameColumn.so,  alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(m.hr))    .frame(width: PitchingGameColumn.hr,  alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatInt(m.hbp))   .frame(width: PitchingGameColumn.hbp, alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            // ERA from this month's ER × 9 / IP only — not cumulative.
            Text(format2(m.era))
                .frame(width: PitchingGameColumn.era, alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
        }
        .font(.caption.weight(.semibold))
        .padding(.trailing, 12)
        .padding(.vertical, 8)
        .frame(height: 32)
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

/// Per-game total bases. TB = singles + 2·2B + 3·3B + 4·HR, expressed
/// in available stats as H + 2B + 2·3B + 3·HR. Returns nil when H is
/// missing — better to render "—" than a wrong zero.
private func perGameTB(_ g: GameLog) -> Int? {
    guard let h = g.H else { return nil }
    let dbl = g.doubles ?? 0
    let trp = g.triples ?? 0
    let hr  = g.HR ?? 0
    return h + dbl + 2 * trp + 3 * hr
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
