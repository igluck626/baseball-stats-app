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

    @Environment(\.dismiss) private var dismiss

    enum RosterMode: String, Hashable { case hitters, pitchers }
    @State private var mode: RosterMode = .hitters

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
        NavigationStack {
            VStack(spacing: 0) {
                Picker("Mode", selection: $mode) {
                    Text("Hitters").tag(RosterMode.hitters)
                    Text("Pitchers").tag(RosterMode.pitchers)
                }
                .pickerStyle(.segmented)
                .padding(.horizontal, 16)
                .padding(.top, 10)
                .padding(.bottom, 8)

                Divider()

                tableContent
            }
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .principal) {
                    HStack(spacing: 6) {
                        TeamLogoView(team: entry.teamInfo, size: 22)
                        Text(seasonLabel)
                            .font(.headline.weight(.semibold))
                    }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Close") { dismiss() }
                }
            }
            // Profiles push onto the sheet's own nav stack — back
            // returns to the table instead of dismissing the sheet.
            .navigationDestination(for: PlayerSearchResult.self) { player in
                PlayerProfileView(player: player)
            }
        }
        .presentationBackground(.ultraThinMaterial)
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
                    ForEach(sections, id: \.self) { section in
                        let players = sortPlayers(playersIn(section), in: section)
                        if !players.isEmpty {
                            sectionHeader(section)
                            ForEach(Array(players.enumerated()), id: \.offset) { _, player in
                                rowButton(player: player)
                            }
                        }
                    }
                }
                .padding(.bottom, 24)
            }
        }
    }

    /// Plain bold uppercase label + Divider underneath. Matches the
    /// Leaders tab + the updated InjuryReportSheet chrome — no
    /// capsule accent, no shaded band; the underline carries the
    /// visual separation.
    private func sectionHeader(_ group: RosterPositionGroup) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(sectionTitle(group))
                .font(.caption.weight(.bold))
                .tracking(0.8)
                .foregroundStyle(.primary)
            Divider()
        }
        .padding(.horizontal, 16)
        .padding(.top, 18)
        .padding(.bottom, 4)
    }

    @ViewBuilder
    private func rowButton(player: RosterPlayer) -> some View {
        if let resolved = player.resolved {
            NavigationLink(value: resolved) {
                row(player, tappable: true)
            }
            .buttonStyle(.plain)
        } else {
            row(player, tappable: false)
        }
    }

    /// Row chrome borrowed from `LeaderboardRow`. Left side stacks
    /// name (prominent) over position abbrev (subtitle); right side
    /// is a compact dot-separated stat line; trailing chevron when
    /// the row is tappable. No fixed column widths, no zebra — the
    /// section header underline + intrinsic row spacing are the only
    /// separators.
    private func row(_ player: RosterPlayer, tappable: Bool) -> some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(player.name)
                    .font(.title3.weight(.semibold))
                    .lineLimit(1)
                    .minimumScaleFactor(0.75)
                    .frame(maxWidth: .infinity, alignment: .leading)
                if !player.position.isEmpty {
                    Text(player.position.uppercased())
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            Text(statLineText(for: player))
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .monospacedDigit()
                .lineLimit(1)

            if tappable {
                Image(systemName: "chevron.right")
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(.tertiary)
            }
        }
        .padding(.vertical, 6)
        .padding(.horizontal, 16)
        .contentShape(Rectangle())
    }

    /// Compact stat string — labeled with the column key so the user
    /// can parse "AVG .305" without an external column-header row.
    /// `·`-separated and rendered on one line; long values shrink via
    /// the row's `lineLimit(1)`.
    private func statLineText(for player: RosterPlayer) -> String {
        let labels = statColumns
        let values = statValues(for: player)
        return zip(labels, values).map { "\($0.0) \($0.1)" }.joined(separator: " · ")
    }

    /// Per-section sort. Position-player buckets sort by conventional
    /// batting-order position (1B → 2B → SS → 3B for IF; LF → CF →
    /// RF for OF). Pitcher buckets sort by usage signal: SP by ERA
    /// ascending (best ERA on top); RP by games-pitched descending
    /// (highest workload on top). nil keys fall to the end.
    private func sortPlayers(
        _ players: [RosterPlayer], in group: RosterPositionGroup,
    ) -> [RosterPlayer] {
        switch group {
        case .infield:
            return players.sorted {
                infieldRank($0.position) < infieldRank($1.position)
            }
        case .outfield:
            return players.sorted {
                outfieldRank($0.position) < outfieldRank($1.position)
            }
        case .sp:
            return players.sorted {
                let a = $0.currentStats?.era ?? .greatestFiniteMagnitude
                let b = $1.currentStats?.era ?? .greatestFiniteMagnitude
                return a < b
            }
        case .rp:
            return players.sorted {
                let a = $0.currentStats?.g ?? Int.min
                let b = $1.currentStats?.g ?? Int.min
                return a > b
            }
        case .c, .dh:
            return players
        }
    }

    /// Conventional infield order: C lands first if a catcher slips
    /// into the IF bucket (rare — `RosterPositionGroup.from` puts C
    /// in its own bucket), then 1B → 2B → SS → 3B. Generic "IF" /
    /// unknown sort to the end.
    private func infieldRank(_ pos: String) -> Int {
        switch pos.uppercased() {
        case "C":  return 0
        case "1B": return 1
        case "2B": return 2
        case "SS": return 3
        case "3B": return 4
        case "IF": return 5
        default:   return 6
        }
    }

    /// Conventional outfield order: LF → CF → RF; generic "OF" /
    /// unknown sort to the end.
    private func outfieldRank(_ pos: String) -> Int {
        switch pos.uppercased() {
        case "LF": return 0
        case "CF": return 1
        case "RF": return 2
        case "OF": return 3
        default:   return 4
        }
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
