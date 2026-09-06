//
//  InjuryReportSheet.swift
//  BaseballStats
//
//  See-All sheet for the Home tab's Injury Report section. Players
//  are bucketed by IL designation — least severe first (Day-To-Day
//  → 10-Day → 15-Day → 60-Day) — and rendered in the same row idiom
//  as `LeaderboardRow` (prominent name, secondary subtitle, trailing
//  value cell). Tapping a row pushes the player's profile onto the
//  sheet's internal nav stack so back returns to the list rather
//  than dismissing the sheet.
//

import SwiftUI

struct InjuryReportSheet: View {
    let entry: MLBTeamCatalog.Entry
    let players: [InjuredPlayer]
    /// `{bdl_id → resolved PlayerSearchResult}`. Rows whose `bdl_id`
    /// isn't in this dict render without a tap target.
    let resolved: [Int: PlayerSearchResult]
    let isLoading: Bool

    /// Passed explicitly (not `@EnvironmentObject`) because environment objects
    /// don't reliably cross the `.sheet` boundary — the same reason
    /// `ScheduleSheet` takes them.
    @ObservedObject var navigation: AppNavigation
    @ObservedObject var liveStore: LiveGameStore
    /// This stack's own path. Value-based `NavigationLink`s append to it
    /// whether or not a binding was supplied, so supplying one changes nothing
    /// about them — it only makes a programmatic push possible, which is what
    /// a box score needs.
    @State private var path = NavigationPath()
    @Environment(\.dismiss) private var dismiss

    /// Section order — least severe first. Each entry pairs the
    /// uppercase display label with the BDL hyphenated wire string
    /// that matches into it. Adding a new bucket (e.g.,
    /// "Bereavement-List") only needs an entry here.
    private static let sections: [(label: String, status: String)] = [
        ("DAY-TO-DAY", "Day-To-Day"),
        ("10-DAY IL",  "10-Day-IL"),
        ("15-DAY IL",  "15-Day-IL"),
        ("60-DAY IL",  "60-Day-IL"),
    ]

    var body: some View {
        NavigationStack(path: $path) {
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
                .stackDestinations(BoxScoreContext(
                    path: $path,
                    owningTab: .home,
                    navigation: navigation,
                    liveStore: liveStore,
                ))
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
                            ForEach(Array(bucket.enumerated()), id: \.offset) { _, player in
                                rowButton(player)
                            }
                        }
                    }
                }
                .padding(.bottom, 24)
            }
        }
    }

    /// Plain bold uppercase label + Divider underneath. Matches the
    /// Leaders tab's chrome — no capsule accent, no shaded band; the
    /// underline carries the visual separation.
    private func sectionHeader(_ label: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(label)
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

    /// Row chrome borrowed from `LeaderboardRow` — name in
    /// `.title3.weight(.semibold)`, subtitle in `.subheadline
    /// .secondary`, trailing status badge. No navigation chevron —
    /// rows are still tappable via the `NavigationLink` wrapper.
    private func row(_ player: InjuredPlayer) -> some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(player.name)
                    .font(.title3.weight(.semibold))
                    .lineLimit(1)
                    .minimumScaleFactor(0.75)
                    .frame(maxWidth: .infinity, alignment: .leading)
                let pos = Self.abbreviatePosition(player.position)
                if !pos.isEmpty {
                    Text(pos)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            statusBadge(for: player.status)
        }
        .padding(.vertical, 6)
        .padding(.horizontal, 16)
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

    /// IL severity → fill color. Same palette as the previous design:
    /// red / orange / yellow / gray, falling through to gray for any
    /// unrecognized BDL status string. Match keys are BDL's
    /// hyphenated wire format.
    private static func color(for status: String) -> Color {
        switch status {
        case "60-Day-IL":  return .red
        case "15-Day-IL":  return .orange
        case "10-Day-IL":  return .yellow
        case "Day-To-Day": return Color(.systemGray)
        default:           return Color(.systemGray)
        }
    }

    /// Normalize a BDL position string ("Starting Pitcher" / "Center
    /// Field" / "Shortstop" / …) to its short baseball abbreviation
    /// ("SP" / "CF" / "SS"). Returns the raw string when no mapping
    /// exists so an unfamiliar value still renders legibly, and the
    /// empty string when the input is nil.
    /// Full position name → abbreviation. `static` (not private) so
    /// the Team History sheet's Awards section can reuse the exact
    /// same mapping. Already-abbreviated / unknown inputs pass through
    /// unchanged via the default branch.
    static func abbreviatePosition(_ pos: String?) -> String {
        guard let pos = pos else { return "" }
        switch pos.lowercased() {
        case "starting pitcher", "starter":      return "SP"
        case "relief pitcher", "reliever":       return "RP"
        case "closing pitcher", "closer":        return "CL"
        case "catcher":                          return "C"
        case "first base", "first baseman":      return "1B"
        case "second base", "second baseman":    return "2B"
        case "third base", "third baseman":      return "3B"
        case "shortstop":                        return "SS"
        case "left field", "left fielder":       return "LF"
        case "center field", "center fielder":   return "CF"
        case "right field", "right fielder":     return "RF"
        case "outfield", "outfielder":           return "OF"
        case "designated hitter":                return "DH"
        case "pitcher":                          return "P"
        default:                                 return pos
        }
    }
}
