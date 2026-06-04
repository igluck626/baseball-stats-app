//
//  TeamHistorySheet.swift
//  BaseballStats
//
//  Season-by-season history sheet for the Home tab. Single scrollable
//  table, most-recent year first. Each row shows W-L, win %, division
//  finish, and the deepest postseason round the team reached (when
//  any).
//

import SwiftUI

struct TeamHistorySheet: View {
    let entry: MLBTeamCatalog.Entry
    /// Reuses the shared `TeamStanding` shape (already used by the
    /// Standings tab) — a strict superset of what this sheet shows.
    let history: [TeamStanding]
    /// Pre-grouped `{year → [series]}` lookup so each row can render
    /// its postseason cell with one dict access instead of walking
    /// the full array.
    let postseasonByYear: [Int: [TeamPostseasonSeries]]
    let isLoading: Bool

    @Environment(\.dismiss) private var dismiss

    private var tint: Color {
        TeamColors.color(for: entry.lahmanCode) ?? .accentColor
    }

    // Column widths shared by the header row and every data row so
    // values stack into clean columns. POSTSEASON is flex
    // (`maxWidth: .infinity`); the rest are fixed.
    private let colYear:   CGFloat = 52
    private let colW:      CGFloat = 32
    private let colL:      CGFloat = 32
    private let colPct:    CGFloat = 44
    private let colFinish: CGFloat = 52
    private let rowHeight: CGFloat = 36

    var body: some View {
        NavigationStack {
            content
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .principal) {
                        HStack(spacing: 6) {
                            TeamLogoView(team: entry.teamInfo, size: 22)
                            Text("Season History")
                                .font(.headline.weight(.semibold))
                        }
                    }
                    ToolbarItem(placement: .topBarTrailing) {
                        Button("Close") { dismiss() }
                    }
                }
        }
    }

    @ViewBuilder
    private var content: some View {
        if isLoading && history.isEmpty {
            VStack {
                Spacer()
                ProgressView()
                Spacer()
            }
        } else if history.isEmpty {
            VStack(spacing: 6) {
                Spacer()
                Image(systemName: "clock.arrow.circlepath")
                    .font(.title)
                    .foregroundStyle(.tertiary)
                Text("No history available")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                Spacer()
            }
        } else {
            ScrollView {
                VStack(spacing: 0) {
                    columnHeaderRow
                    Divider()
                    ForEach(Array(history.enumerated()), id: \.offset) { idx, row in
                        seasonRow(row, alternate: idx.isMultiple(of: 2))
                    }
                }
                .padding(.bottom, 24)
            }
        }
    }

    private var columnHeaderRow: some View {
        HStack(spacing: 0) {
            Text("YEAR")  .frame(width: colYear,   alignment: .leading)
            Text("W")     .frame(width: colW,      alignment: .trailing)
            Text("L")     .frame(width: colL,      alignment: .trailing)
            Text("PCT")   .frame(width: colPct,    alignment: .trailing)
            Text("FINISH").frame(width: colFinish, alignment: .trailing)
            Text("POSTSEASON")
                .frame(maxWidth: .infinity, alignment: .trailing)
        }
        .font(.caption2.weight(.bold))
        .tracking(0.5)
        .foregroundStyle(.secondary)
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
    }

    private func seasonRow(_ row: TeamStanding, alternate: Bool) -> some View {
        let divisionWinner = (row.division_leader == true) || (row.rank == 1)
        let year = row.year ?? 0
        return HStack(spacing: 0) {
            Text(String(year))
                .font(.subheadline.weight(.bold))
                .foregroundStyle(divisionWinner ? tint : .primary)
                .monospacedDigit()
                .frame(width: colYear, alignment: .leading)

            Text(intCell(row.W))
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(divisionWinner ? Color.green : .primary)
                .monospacedDigit()
                .frame(width: colW, alignment: .trailing)

            Text(intCell(row.L))
                .font(.subheadline.weight(.medium))
                .foregroundStyle(.primary)
                .monospacedDigit()
                .frame(width: colL, alignment: .trailing)

            Text(pctCell(row.win_pct))
                .font(.subheadline.weight(.medium))
                .foregroundStyle(.primary)
                .monospacedDigit()
                .frame(width: colPct, alignment: .trailing)

            Text(finishCell(row.rank))
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(row.rank == 1 ? tint : .secondary)
                .frame(width: colFinish, alignment: .trailing)

            Text(postseasonSummary(forYear: year))
                .font(.subheadline.weight(.medium))
                .foregroundStyle(.primary)
                .lineLimit(1)
                .frame(maxWidth: .infinity, alignment: .trailing)
        }
        .padding(.horizontal, 16)
        .frame(height: rowHeight)
        .background(alternate ? Color(.systemFill).opacity(0.12) : Color.clear)
    }

    // MARK: - Cell formatters

    private func intCell(_ v: Int?) -> String {
        guard let v else { return "—" }
        return String(v)
    }

    /// 3-decimal win-pct with the leading zero stripped — ".605"
    /// not "0.605", matching the batting-rate convention used
    /// across the app.
    private func pctCell(_ v: Double?) -> String {
        guard let v else { return "—" }
        let s = String(format: "%.3f", v)
        if s.hasPrefix("0.")  { return String(s.dropFirst()) }
        if s.hasPrefix("-0.") { return "-" + String(s.dropFirst(2)) }
        return s
    }

    /// "1st" / "2nd" / "3rd" / "4th" / "5th" / "6th" / "—". Used
    /// for division-finish; only ranks 1-6 (modern divisions are
    /// at most 5 teams but a few historical seasons had 6+) get a
    /// dedicated ordinal — anything higher falls through to "Nth".
    private func finishCell(_ rank: Int?) -> String {
        guard let r = rank, r > 0 else { return "—" }
        switch r {
        case 1: return "1st"
        case 2: return "2nd"
        case 3: return "3rd"
        default: return "\(r)th"
        }
    }

    /// Compact postseason summary for a given season. Picks the
    /// DEEPEST round the team reached (WS > LCS > LDS > WC) and
    /// renders:
    ///   • "🏆 WS"  → won the World Series
    ///   • "WS"     → lost the World Series
    ///   • "ALCS" / "NLCS" / "ALDS" / "NLDS"  → lost in that round
    ///   • "WC"     → lost in the wild-card round
    ///   • "—"      → no postseason that year
    private func postseasonSummary(forYear year: Int) -> String {
        let series = postseasonByYear[year] ?? []
        guard let deepest = series.max(by: { Self.depth(of: $0.round) < Self.depth(of: $1.round) }) else {
            return "—"
        }
        if deepest.round == "World Series" {
            return deepest.won ? "🏆 WS" : "WS"
        }
        return Self.compact(round: deepest.round)
    }

    /// Ordinal depth ranking for postseason rounds; higher = deeper.
    private static func depth(of round: String) -> Int {
        switch round {
        case "World Series":                          return 4
        case "ALCS", "NLCS":                          return 3
        case "ALDS", "NLDS":                          return 2
        case "AL Wild Card", "NL Wild Card":          return 1
        default:                                       return 0
        }
    }

    /// Backend's display round name → compact display string used
    /// by the POSTSEASON column.
    private static func compact(round: String) -> String {
        switch round {
        case "World Series":                 return "WS"
        case "ALCS":                          return "ALCS"
        case "NLCS":                          return "NLCS"
        case "ALDS":                          return "ALDS"
        case "NLDS":                          return "NLDS"
        case "AL Wild Card", "NL Wild Card":  return "WC"
        default:                               return round
        }
    }
}
