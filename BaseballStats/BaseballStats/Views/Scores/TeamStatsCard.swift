//
//  TeamStatsCard.swift
//  BaseballStats
//
//  Team Stats: the two teams side by side — AVG, xBA, extra-base hits, HR, RISP,
//  LOB, BB, SO, SB, double plays turned — after the batting and pitching tables,
//  live and final. Every number and the choice of rows are the backend's
//  (`TeamStats`); this view only lays them out.
//

import SwiftUI

struct TeamStatsCard: View {
    let stats: TeamStats
    let awayAbbr: String
    let homeAbbr: String

    @Environment(\.dynamicTypeSize) private var typeSize
    @State private var showingInfo = false

    var body: some View {
        let rows = stats.displayRows
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Text("TEAM STATS")
                    .font(.caption.weight(.bold))
                    .tracking(0.8)
                    .foregroundStyle(.secondary)
                if rows.contains(where: { $0.key == "xba" }) {
                    Button { showingInfo = true } label: {
                        Image(systemName: "info.circle")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("About xBA")
                    .accessibilityIdentifier("teamStats.info")
                    // A popover on a regular-width screen; a sheet on a phone,
                    // where a popover anchored low on the screen had no room.
                    .popover(isPresented: $showingInfo) {
                        TeamStatsInfo()
                            .presentationCompactAdaptation(.sheet)
                            .presentationDetents([.medium, .large])
                            .presentationDragIndicator(.visible)
                    }
                }
            }
            if typeSize.isAccessibilitySize {
                // One Text per row, so it wraps rather than breaking a column
                // apart (the house pattern for metric rows).
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(rows) { row in
                        Text("\(row.label) · \(awayAbbr) \(row.away) · \(homeAbbr) \(row.home)")
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .font(.subheadline)
            } else {
                table(rows)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 16))
        .shadow(color: .black.opacity(0.06), radius: 8, x: 0, y: 3)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("teamStats")
    }

    /// away | stat | home, mirrored around the centred labels. The two team
    /// columns share the width equally and centre their values, so a long value
    /// ("2-for-7") and a short one ("4") sit on the same axis, and the header
    /// abbreviations sit over them. One Grid, so every row has the same column
    /// widths whichever rows the server sends. Capped and centred in the card: on
    /// a regular-width screen the values stay near their labels rather than at
    /// the card's edges.
    private func table(_ rows: [TeamStats.Row]) -> some View {
        Grid(horizontalSpacing: 12, verticalSpacing: 7) {
            GridRow {
                Text(awayAbbr).frame(maxWidth: .infinity)
                Color.clear.gridCellUnsizedAxes([.horizontal, .vertical])
                Text(homeAbbr).frame(maxWidth: .infinity)
            }
            .font(.subheadline.weight(.semibold))
            .accessibilityHidden(true)
            Divider().opacity(0.4)
            ForEach(rows) { row in
                GridRow {
                    Text(row.away).monospacedDigit().frame(maxWidth: .infinity)
                    Text(row.label)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .fixedSize()
                    Text(row.home).monospacedDigit().frame(maxWidth: .infinity)
                }
                .font(.subheadline)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("\(row.label): \(awayAbbr) \(row.away), \(homeAbbr) \(row.home)")
            }
        }
        .frame(maxWidth: Self.tableMaxWidth)
        .frame(maxWidth: .infinity)
    }

    /// The table's widest extent — a phone's card is narrower, so this binds only
    /// on a regular-width screen.
    private static let tableMaxWidth: CGFloat = 420
}

/// The ⓘ explanation of xBA, at a FIXED width: a popover
/// measures the text's height at the width it proposes, and with only a maximum
/// width the two disagreed and the last lines were cut off. (A popover's contents
/// are compact width too, so the size class can't tell it from the phone's sheet.)
/// It scrolls only if it doesn't fit — the sheet at the accessibility sizes.
private struct TeamStatsInfo: View {
    var body: some View {
        ViewThatFits(in: .vertical) {
            info
            ScrollView { info }
        }
        .frame(width: 340, alignment: .leading)
    }

    private var info: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("xBA (expected batting average)")
                .font(.subheadline.weight(.semibold))
            Text("The average a team's contact would usually produce. Each ball in play is rated by how often balls hit at that speed and angle fall for hits; strikeouts count as outs. An xBA above the team's AVG means its contact deserved more hits than it got. Early in a game, a single ball in play can move xBA a lot.")
            Text("Statcast data via balldontlie.")
                .foregroundStyle(.secondary)
        }
        .font(.footnote)
        .fixedSize(horizontal: false, vertical: true)
        .padding()
    }
}
