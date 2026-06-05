//
//  InjuryReportSheet.swift
//  BaseballStats
//
//  See-All sheet for the Home tab's Injury Report section. Players
//  are bucketed by IL designation (60-Day → 15-Day → 10-Day →
//  Day-To-Day) and rendered as a clean table with team-color section
//  accents. Tapping a row pushes the player's profile onto the
//  sheet's internal nav stack (same pattern as RosterSheet and
//  TeamLeadersSheet) so back returns to the list rather than
//  dismissing the sheet.
//

import SwiftUI

struct InjuryReportSheet: View {
    let entry: MLBTeamCatalog.Entry
    let players: [InjuredPlayer]
    /// `{bdl_id → resolved PlayerSearchResult}`. Rows whose `bdl_id`
    /// isn't in this dict render without a tap target.
    let resolved: [Int: PlayerSearchResult]
    let isLoading: Bool

    @Environment(\.dismiss) private var dismiss

    private var tint: Color {
        TeamColors.color(for: entry.lahmanCode) ?? .accentColor
    }

    /// Section order — most severe first. Each entry pairs the
    /// uppercase display label with the BDL status string that
    /// matches into it. Adding a new bucket (e.g., "Bereavement
    /// List") only needs an entry here.
    private static let sections: [(label: String, status: String)] = [
        ("60-DAY IL",  "60-Day IL"),
        ("15-DAY IL",  "15-Day IL"),
        ("10-DAY IL",  "10-Day IL"),
        ("DAY-TO-DAY", "Day-To-Day"),
    ]

    var body: some View {
        NavigationStack {
            content
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .principal) {
                        HStack(spacing: 6) {
                            TeamLogoView(team: entry.teamInfo, size: 22)
                            Text("Injury Report")
                                .font(.headline.weight(.semibold))
                        }
                    }
                    ToolbarItem(placement: .topBarTrailing) {
                        Button("Close") { dismiss() }
                    }
                }
                // Profiles push onto the sheet's own nav stack — back
                // returns to the injury list instead of dismissing.
                .navigationDestination(for: PlayerSearchResult.self) { player in
                    PlayerProfileView(player: player)
                }
        }
        .presentationBackground(.ultraThinMaterial)
    }

    @ViewBuilder
    private var content: some View {
        if isLoading && players.isEmpty {
            VStack {
                Spacer()
                ProgressView()
                Spacer()
            }
        } else if players.isEmpty {
            VStack(spacing: 6) {
                Spacer()
                Image(systemName: "cross.case")
                    .font(.title)
                    .foregroundStyle(.tertiary)
                Text("No injured players")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                Spacer()
            }
        } else {
            ScrollView {
                VStack(spacing: 0) {
                    ForEach(Self.sections, id: \.status) { section in
                        let bucket = players.filter { $0.status == section.status }
                        if !bucket.isEmpty {
                            sectionHeader(section.label)
                            ForEach(Array(bucket.enumerated()), id: \.offset) { idx, player in
                                rowButton(player)
                                if idx < bucket.count - 1 {
                                    Divider().opacity(0.4)
                                        .padding(.horizontal, 16)
                                }
                            }
                        }
                    }
                }
                .padding(.bottom, 24)
            }
        }
    }

    /// Team-color capsule accent + bold label, no shaded band —
    /// matches the RosterSheet's section header design.
    private func sectionHeader(_ label: String) -> some View {
        HStack(spacing: 10) {
            Capsule()
                .fill(tint)
                .frame(width: 4, height: 18)
            Text(label)
                .font(.caption.weight(.bold))
                .tracking(0.6)
                .foregroundStyle(.primary)
            Spacer()
        }
        .padding(.horizontal, 16)
        .padding(.top, 16)
        .padding(.bottom, 8)
    }

    @ViewBuilder
    private func rowButton(_ player: InjuredPlayer) -> some View {
        if let pick = resolved[player.bdl_id] {
            NavigationLink(value: pick) {
                row(player)
            }
            .buttonStyle(.plain)
        } else {
            row(player)
        }
    }

    private func row(_ player: InjuredPlayer) -> some View {
        HStack(spacing: 12) {
            Text(player.name)
                .font(.body.weight(.medium))
                .foregroundStyle(.primary)
                .lineLimit(1)
                .minimumScaleFactor(0.85)
            if let pos = player.position, !pos.isEmpty {
                Text(pos.uppercased())
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 8)
            statusBadge(for: player.status)
        }
        .padding(.horizontal, 16)
        .frame(minHeight: 44)
        .contentShape(Rectangle())
    }

    private func statusBadge(for status: String) -> some View {
        Text(status)
            .font(.caption2.weight(.semibold))
            .foregroundStyle(.white)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(Self.color(for: status), in: Capsule())
    }

    /// IL severity → fill color. Same palette as the previous inline
    /// card: red / orange / yellow / gray, falling through to gray
    /// for any unrecognized BDL status string.
    private static func color(for status: String) -> Color {
        switch status {
        case "60-Day IL":  return .red
        case "15-Day IL":  return .orange
        case "10-Day IL":  return .yellow
        case "Day-To-Day": return Color(.systemGray)
        default:           return Color(.systemGray)
        }
    }
}
