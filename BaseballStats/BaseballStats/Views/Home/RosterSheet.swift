//
//  RosterSheet.swift
//  BaseballStats
//
//  See-All sheet for the Home tab's Roster section. Hitters /
//  Pitchers segmented picker at the top; below it a table with
//  team-color-accented section headers (CATCHER / INFIELD / OUTFIELD
//  / DH on the Hitters side; STARTERS / RELIEVERS on the Pitchers
//  side) and one row per player. Tapping a row dismisses the sheet
//  and pushes the player's profile onto the parent's nav stack via
//  `onTapPlayer`.
//

import SwiftUI

struct RosterSheet: View {
    let entry: MLBTeamCatalog.Entry
    let roster: [RosterPlayer]
    let isLoading: Bool
    /// Fired with the resolved `PlayerSearchResult` so the parent can
    /// close the sheet and append the profile to its nav path.
    /// Rows for players whose BDL → MLBAM lookup failed render with
    /// no tap target (`resolved` is nil).
    let onTapPlayer: (PlayerSearchResult) -> Void

    @Environment(\.dismiss) private var dismiss

    enum RosterMode: String, Hashable { case hitters, pitchers }
    @State private var mode: RosterMode = .hitters

    /// Column-width constants shared by the column-header row and
    /// every data row so values stack into clean columns. Name is
    /// flex (`maxWidth: .infinity`); the four stat cells are fixed.
    private let statCellWidth: CGFloat = 50

    private var tint: Color {
        TeamColors.color(for: entry.lahmanCode) ?? .accentColor
    }

    private var seasonLabel: String {
        "\(Calendar.current.component(.year, from: Date())) Roster"
    }

    /// Position-bucket order per mode. Hitters skip SP/RP; Pitchers
    /// skip the position-player buckets. `.dh` lives at the end of
    /// the hitters list because a designated hitter is the rarest
    /// dedicated entry on a modern roster.
    private var sections: [RosterPositionGroup] {
        switch mode {
        case .hitters:  return [.c, .infield, .outfield, .dh]
        case .pitchers: return [.sp, .rp]
        }
    }

    private var statColumns: [String] {
        switch mode {
        case .hitters:  return ["AVG", "HR",  "RBI", "OPS"]
        case .pitchers: return ["ERA", "W",   "SO",  "WHIP"]
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Picker("Mode", selection: $mode) {
                Text("Hitters").tag(RosterMode.hitters)
                Text("Pitchers").tag(RosterMode.pitchers)
            }
            .pickerStyle(.segmented)
            .padding(.horizontal, 16)
            .padding(.top, 8)
            .padding(.bottom, 8)

            Divider()

            tableContent
        }
    }

    private var header: some View {
        HStack(spacing: 10) {
            TeamLogoView(team: entry.teamInfo, size: 32)
            VStack(alignment: .leading, spacing: 0) {
                Text(seasonLabel)
                    .font(.headline.weight(.bold))
                Text(entry.fullName)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Button {
                dismiss()
            } label: {
                Image(systemName: "xmark.circle.fill")
                    .font(.title3)
                    .foregroundStyle(.secondary)
                    .symbolRenderingMode(.hierarchical)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Close")
        }
        .padding(.horizontal, 16)
        .padding(.top, 14)
        .padding(.bottom, 8)
    }

    @ViewBuilder
    private var tableContent: some View {
        if isLoading && roster.isEmpty {
            VStack {
                Spacer()
                ProgressView()
                Spacer()
            }
        } else {
            ScrollView {
                VStack(spacing: 0) {
                    columnHeaderRow
                    Divider()
                    ForEach(sections, id: \.self) { section in
                        let players = playersIn(section)
                        if !players.isEmpty {
                            sectionHeader(section)
                            ForEach(Array(players.enumerated()), id: \.offset) { idx, player in
                                rowButton(player: player, alternate: idx.isMultiple(of: 2))
                            }
                        }
                    }
                }
                .padding(.bottom, 24)
            }
        }
    }

    private var columnHeaderRow: some View {
        HStack(spacing: 0) {
            Text("NAME")
                .frame(maxWidth: .infinity, alignment: .leading)
            ForEach(statColumns, id: \.self) { col in
                Text(col)
                    .frame(width: statCellWidth, alignment: .trailing)
            }
        }
        .font(.caption2.weight(.bold))
        .tracking(0.5)
        .foregroundStyle(.secondary)
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
    }

    private func sectionHeader(_ group: RosterPositionGroup) -> some View {
        HStack(spacing: 8) {
            Capsule()
                .fill(tint)
                .frame(width: 3, height: 12)
            Text(sectionTitle(group))
                .font(.caption.weight(.bold))
                .tracking(0.6)
                .foregroundStyle(.primary)
            Spacer()
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(Color(.systemFill).opacity(0.25))
    }

    @ViewBuilder
    private func rowButton(player: RosterPlayer, alternate: Bool) -> some View {
        if let resolved = player.resolved {
            Button { onTapPlayer(resolved) } label: {
                row(player, alternate: alternate)
            }
            .buttonStyle(.plain)
        } else {
            row(player, alternate: alternate)
        }
    }

    private func row(_ player: RosterPlayer, alternate: Bool) -> some View {
        HStack(spacing: 0) {
            Text(player.name)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.primary)
                .lineLimit(1)
                .minimumScaleFactor(0.8)
                .frame(maxWidth: .infinity, alignment: .leading)
            ForEach(Array(statValues(for: player).enumerated()), id: \.offset) { _, value in
                Text(value)
                    .font(.subheadline.weight(.medium))
                    .monospacedDigit()
                    .foregroundStyle(.primary)
                    .frame(width: statCellWidth, alignment: .trailing)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(alternate ? Color(.systemFill).opacity(0.12) : Color.clear)
        .contentShape(Rectangle())
    }

    /// Long-form label for a position section. SP/RP get "STARTERS"/
    /// "RELIEVERS" to match the user's mental model in the pitcher
    /// table; the position-player buckets get the position name.
    private func sectionTitle(_ group: RosterPositionGroup) -> String {
        switch group {
        case .sp:       return "STARTERS"
        case .rp:       return "RELIEVERS"
        case .c:        return "CATCHER"
        case .infield:  return "INFIELD"
        case .outfield: return "OUTFIELD"
        case .dh:       return "DH"
        }
    }

    /// Players from `roster` that belong to the given position
    /// bucket — uses `RosterPositionGroup.from` so the same mapping
    /// powers the segmented strip and the table.
    private func playersIn(_ group: RosterPositionGroup) -> [RosterPlayer] {
        roster.filter { RosterPositionGroup.from($0.position) == group }
    }

    /// Per-row stat strings in the same order as `statColumns`.
    /// Returns "—" placeholders for stats absent from the player's
    /// current line (early-season, unresolved MLBAM, etc.).
    private func statValues(for player: RosterPlayer) -> [String] {
        let s = player.currentStats
        switch mode {
        case .hitters:
            return [
                formatLeaderValue(s?.avg, stat: "AVG"),
                formatStatInt(s?.hr),
                formatStatInt(s?.rbi),
                formatLeaderValue(s?.ops, stat: "OPS"),
            ]
        case .pitchers:
            return [
                formatLeaderValue(s?.era, stat: "ERA"),
                formatStatInt(s?.w),
                formatStatInt(s?.so),
                formatLeaderValue(s?.whip, stat: "WHIP"),
            ]
        }
    }

    /// "—" for nil, plain integer otherwise. Used for counting stats
    /// where `formatLeaderValue` would need a Double conversion that
    /// muddies the nil case.
    private func formatStatInt(_ v: Int?) -> String {
        guard let v else { return "—" }
        return String(v)
    }
}
