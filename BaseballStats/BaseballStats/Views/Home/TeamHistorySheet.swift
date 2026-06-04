//
//  TeamHistorySheet.swift
//  BaseballStats
//
//  Season-by-season history for the favorite franchise rendered as a
//  vertical timeline of glass cards, most-recent year first. Each
//  card carries the year, W-L, division finish badge, win %, R/RA,
//  and the deepest postseason round (with series record). Border /
//  tint signal milestone seasons — subtle team-color stroke for
//  division winners or any playoff appearance, stronger stroke for
//  World Series wins.
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
        .presentationBackground(.ultraThinMaterial)
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
                LazyVStack(spacing: 10) {
                    ForEach(history) { row in
                        YearCard(
                            row:        row,
                            postseason: postseasonByYear[row.year ?? 0] ?? [],
                            tint:       tint,
                        )
                    }
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 12)
            }
        }
    }

    /// Ordinal depth ranking for postseason rounds; higher = deeper.
    /// Kept at file scope so `YearCard` can reach it without paying
    /// the cost of a duplicate lookup table.
    static func depth(of round: String) -> Int {
        switch round {
        case "World Series":                          return 4
        case "ALCS", "NLCS":                          return 3
        case "ALDS", "NLDS":                          return 2
        case "AL Wild Card", "NL Wild Card":          return 1
        default:                                       return 0
        }
    }
}

// MARK: - Year card

/// One year's accomplishments. Two rows of info: year + W-L +
/// finish-pill on top, win% + R/RA + postseason on the bottom.
/// Border thickness escalates from "no border" → thin team-tint
/// (any playoff appearance or division crown) → thicker team-tint
/// (World Series winner) so the eye picks out trophy seasons at a
/// glance while scrolling.
private struct YearCard: View {
    let row: TeamStanding
    let postseason: [TeamPostseasonSeries]
    let tint: Color

    private var year: Int { row.year ?? 0 }

    private var isDivisionWinner: Bool {
        row.rank == 1 || row.division_leader == true
    }

    private var deepestRound: TeamPostseasonSeries? {
        postseason.max {
            TeamHistorySheet.depth(of: $0.round) <
            TeamHistorySheet.depth(of: $1.round)
        }
    }

    private var hasPostseason: Bool { !postseason.isEmpty }

    private var wonWorldSeries: Bool {
        deepestRound?.round == "World Series" && deepestRound?.won == true
    }

    var body: some View {
        VStack(spacing: 6) {
            topRow
            bottomRow
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .fill(.ultraThinMaterial)
        )
        .overlay(borderOverlay)
        .shadow(color: .black.opacity(0.08), radius: 6, y: 2)
    }

    @ViewBuilder
    private var borderOverlay: some View {
        if wonWorldSeries {
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .stroke(tint.opacity(0.7), lineWidth: 1.5)
        } else if isDivisionWinner || hasPostseason {
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .stroke(tint.opacity(0.3), lineWidth: 1)
        }
    }

    // MARK: Top row — year + W-L + finish badge

    private var topRow: some View {
        HStack(spacing: 10) {
            Text(String(year))
                .font(.title3.bold())
                .foregroundStyle(isDivisionWinner ? tint : .primary)
                .monospacedDigit()
            Text(wlText)
                .font(.subheadline.weight(.medium))
                .foregroundStyle(.primary)
                .monospacedDigit()
            Spacer(minLength: 0)
            finishPill
        }
    }

    private var wlText: String {
        guard let w = row.W, let l = row.L else { return "—" }
        return "\(w)-\(l)"
    }

    private var finishPill: some View {
        let label = isDivisionWinner ? "🏆 DIV" : ordinalRank
        return Text(label)
            .font(.caption2.weight(.semibold))
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(
                isDivisionWinner ? tint.opacity(0.2) : Color(.systemFill),
                in: Capsule(),
            )
            .foregroundStyle(isDivisionWinner ? tint : .secondary)
    }

    private var ordinalRank: String {
        guard let r = row.rank, r > 0 else { return "—" }
        switch r {
        case 1: return "1st"
        case 2: return "2nd"
        case 3: return "3rd"
        default: return "\(r)th"
        }
    }

    // MARK: Bottom row — pct + R/RA + postseason

    private var bottomRow: some View {
        HStack(spacing: 10) {
            Text(pctText)
                .font(.caption.weight(.medium))
                .foregroundStyle(.secondary)
                .monospacedDigit()
            if !rsRaText.isEmpty {
                Text(rsRaText)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
            Spacer(minLength: 0)
            Text(postseasonText)
                .font(.caption.weight(.semibold))
                .foregroundStyle(postseasonColor)
                .lineLimit(1)
        }
    }

    /// 3-decimal win-pct with the leading zero stripped — ".605"
    /// matches the batting-rate convention used across the app.
    private var pctText: String {
        guard let p = row.win_pct else { return "—" }
        let s = String(format: "%.3f", p)
        if s.hasPrefix("0.")  { return String(s.dropFirst()) }
        if s.hasPrefix("-0.") { return "-" + String(s.dropFirst(2)) }
        return s
    }

    /// "R: 842 RA: 686" — collapses to empty when either field is
    /// missing so the row doesn't render a partial half-line.
    private var rsRaText: String {
        guard let rs = row.runs_scored, let ra = row.runs_allowed else { return "" }
        return "R: \(rs) RA: \(ra)"
    }

    /// Compact postseason summary including series record. Picks the
    /// deepest round reached (WS > LCS > LDS > WC). 🏆 prefix only
    /// when the team won the World Series; everything else just gets
    /// the round name + record.
    private var postseasonText: String {
        guard let series = deepestRound else { return "—" }
        let rec = "(\(series.wins)-\(series.losses))"
        if series.round == "World Series" {
            return series.won
                ? "🏆 World Series \(rec)"
                : "World Series \(rec)"
        }
        return "\(series.round) \(rec)"
    }

    private var postseasonColor: Color {
        if wonWorldSeries          { return tint }
        if !hasPostseason          { return Color(.tertiaryLabel) }
        return .primary
    }
}
