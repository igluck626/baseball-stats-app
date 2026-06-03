//
//  RosterSheet.swift
//  BaseballStats
//
//  See-All sheet for the Home tab's Roster section. Segmented picker
//  buckets the team's active roster into SP / RP / C / IF / OF / DH;
//  each bucket renders as a vertical list of player rows. Tapping a
//  row dismisses the sheet and pushes the player's profile onto the
//  parent's nav stack via the `onTapPlayer` callback.
//

import SwiftUI

struct RosterSheet: View {
    let entry: MLBTeamCatalog.Entry
    let roster: [RosterPlayer]
    let isLoading: Bool
    /// Fired with the resolved `PlayerSearchResult` (the parent
    /// closes the sheet and appends to its own nav path). Players
    /// without a resolved MLBAM id show up as un-tappable rows.
    let onTapPlayer: (PlayerSearchResult) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var group: RosterPositionGroup = .sp

    private var tint: Color {
        TeamColors.color(for: entry.lahmanCode) ?? .accentColor
    }

    private var seasonLabel: String {
        let y = Calendar.current.component(.year, from: Date())
        return "\(y) Roster"
    }

    /// Pre-bucket on every render — small list, cheap to filter.
    private var groupedRoster: [RosterPositionGroup: [RosterPlayer]] {
        Dictionary(grouping: roster) { RosterPositionGroup.from($0.position) }
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Picker("Position", selection: $group) {
                ForEach(RosterPositionGroup.allCases, id: \.self) { g in
                    Text(g.displayName).tag(g)
                }
            }
            .pickerStyle(.segmented)
            .padding(.horizontal, 16)
            .padding(.top, 8)
            .padding(.bottom, 8)

            Divider()

            content
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
    private var content: some View {
        if isLoading && roster.isEmpty {
            VStack {
                Spacer()
                ProgressView()
                Spacer()
            }
        } else {
            let players = groupedRoster[group] ?? []
            if players.isEmpty {
                VStack(spacing: 6) {
                    Spacer()
                    Image(systemName: "person.2.slash")
                        .font(.title)
                        .foregroundStyle(.tertiary)
                    Text("No players in \(group.displayName)")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                    Spacer()
                }
            } else {
                List {
                    ForEach(players) { player in
                        rowButton(player)
                            .listRowInsets(EdgeInsets(top: 6, leading: 16, bottom: 6, trailing: 16))
                    }
                }
                .listStyle(.plain)
            }
        }
    }

    @ViewBuilder
    private func rowButton(_ player: RosterPlayer) -> some View {
        if let resolved = player.resolved {
            Button { onTapPlayer(resolved) } label: {
                row(player)
            }
            .buttonStyle(.plain)
        } else {
            row(player)
        }
    }

    private func row(_ player: RosterPlayer) -> some View {
        HStack(spacing: 12) {
            headshot(url: player.headshotURL)
            VStack(alignment: .leading, spacing: 2) {
                Text(player.name)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                Text(player.position.isEmpty ? "—" : player.position)
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(tint)
            }
            Spacer(minLength: 8)
            statLine(player)
        }
        .contentShape(Rectangle())
    }

    private func headshot(url: URL?) -> some View {
        AsyncImage(url: url) { phase in
            switch phase {
            case .success(let image): image.resizable().scaledToFill()
            default:
                Image(systemName: "person.crop.circle.fill")
                    .resizable().scaledToFit()
                    .foregroundStyle(.tertiary)
            }
        }
        .frame(width: 44, height: 44)
        .background(Circle().fill(.ultraThinMaterial))
        .clipShape(Circle())
        .overlay(Circle().strokeBorder(.quaternary, lineWidth: 0.5))
    }

    /// "AVG · HR · RBI · OPS" for batters; "ERA · W · SO · WHIP" for
    /// pitchers. Each cell formatted by the same shared helper used
    /// by the Home strip + Team Leaders sheet so values render
    /// consistently across surfaces.
    @ViewBuilder
    private func statLine(_ player: RosterPlayer) -> some View {
        let s = player.currentStats
        if HomeViewModel.bdlPositionIsPitcher(player.position) {
            HStack(spacing: 10) {
                stat(label: "ERA",  value: formatRosterValue(s?.era,  stat: "ERA"))
                stat(label: "W",    value: formatRosterValue(Double(s?.w  ?? 0), stat: "W",  hasValue: s?.w  != nil))
                stat(label: "SO",   value: formatRosterValue(Double(s?.so ?? 0), stat: "SO", hasValue: s?.so != nil))
                stat(label: "WHIP", value: formatRosterValue(s?.whip, stat: "WHIP"))
            }
        } else {
            HStack(spacing: 10) {
                stat(label: "AVG", value: formatRosterValue(s?.avg, stat: "AVG"))
                stat(label: "HR",  value: formatRosterValue(Double(s?.hr  ?? 0), stat: "HR",  hasValue: s?.hr  != nil))
                stat(label: "RBI", value: formatRosterValue(Double(s?.rbi ?? 0), stat: "RBI", hasValue: s?.rbi != nil))
                stat(label: "OPS", value: formatRosterValue(s?.ops, stat: "OPS"))
            }
        }
    }

    private func stat(label: String, value: String) -> some View {
        VStack(spacing: 1) {
            Text(value)
                .font(.caption.weight(.bold))
                .monospacedDigit()
                .foregroundStyle(.primary)
            Text(label)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .tracking(0.3)
        }
        .frame(minWidth: 38, alignment: .center)
    }
}

/// Local wrapper around the shared `formatLeaderValue` so the
/// roster sheet can pass Int? sources via the `hasValue` shortcut
/// (the shared helper only knows Double?). Falls back to "—"
/// uniformly when the underlying value is absent.
private func formatRosterValue(_ v: Double?, stat: String, hasValue: Bool = true) -> String {
    guard hasValue else { return "—" }
    return formatLeaderValue(v, stat: stat)
}
