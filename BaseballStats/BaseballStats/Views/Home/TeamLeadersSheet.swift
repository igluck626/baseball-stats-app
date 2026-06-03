//
//  TeamLeadersSheet.swift
//  BaseballStats
//
//  See-All sheet for the Home tab's Team Leaders section. Opens
//  full-height with a Batting/Pitching segmented picker, a row of
//  stat-category pills, and a ranked 1-10 list for the selected
//  category. Tapping a row dismisses the sheet and pushes the
//  player's profile onto the parent's nav stack via the
//  `onTapPlayer` callback.
//

import SwiftUI

struct TeamLeadersSheet: View {
    let entry: MLBTeamCatalog.Entry
    /// Shared with the Home view so the user's last-tapped stat
    /// survives a sheet re-open. Sheet reads on appear, writes on
    /// every stat tap.
    @ObservedObject var vm: HomeViewModel
    /// Fired with the tapped player; the parent is expected to
    /// dismiss the sheet and push the profile onto its own nav
    /// stack. Wired up this way (vs. having the sheet host a
    /// NavigationStack) so the profile lives in the same path as
    /// every other Home-tab navigation destination.
    let onTapPlayer: (PlayerSearchResult) -> Void

    @Environment(\.dismiss) private var dismiss

    enum Role: String, Hashable { case batting, pitching }

    @State private var role: Role = .batting
    @State private var stat: String = "AVG"
    @State private var leaders: [LeaderCard] = []
    @State private var isLoading: Bool = false

    private static let battingStats:  [String] = [
        "AVG", "HR", "RBI", "OPS", "SLG", "OBP", "SB",
        "H", "R", "BB", "WAR", "2B", "3B", "SO",
    ]
    private static let pitchingStats: [String] = [
        "ERA", "W", "SO", "WHIP", "SV", "IP", "WAR",
        "BB", "H", "HR", "SO/9", "CG", "SHO",
    ]

    private var currentStats: [String] {
        role == .batting ? Self.battingStats : Self.pitchingStats
    }

    private var tint: Color {
        TeamColors.color(for: entry.lahmanCode) ?? .accentColor
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Picker("Role", selection: $role) {
                Text("Batting").tag(Role.batting)
                Text("Pitching").tag(Role.pitching)
            }
            .pickerStyle(.segmented)
            .padding(.horizontal, 16)
            .padding(.top, 10)
            .padding(.bottom, 8)

            statPillRow

            Divider().padding(.top, 4)

            content
        }
        .task {
            // Initial fetch — restore the user's last tapped stat
            // (if any). Otherwise use the role's default.
            if let saved = vm.selectedLeaderStat,
               currentStats.contains(saved) {
                stat = saved
            } else {
                stat = currentStats.first ?? "AVG"
            }
            await load()
        }
        .onChange(of: role) { _, _ in
            // Reset to the new role's first stat — the previous
            // role's stat key isn't in the new role's button row.
            stat = currentStats.first ?? "AVG"
        }
        .onChange(of: stat) { _, newValue in
            vm.selectedLeaderStat = newValue
            Task { await load() }
        }
    }

    private var header: some View {
        HStack(spacing: 10) {
            TeamLogoView(team: entry.teamInfo, size: 32)
            VStack(alignment: .leading, spacing: 0) {
                Text("Team Leaders")
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

    private var statPillRow: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(currentStats, id: \.self) { s in
                    Button {
                        stat = s
                    } label: {
                        Text(s)
                            .font(.subheadline.weight(.semibold))
                            .padding(.horizontal, 14)
                            .padding(.vertical, 6)
                            .background(
                                Capsule()
                                    .fill(stat == s ? tint : Color(.systemFill).opacity(0.6))
                            )
                            .foregroundStyle(stat == s ? .white : .primary)
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 4)
        }
    }

    @ViewBuilder
    private var content: some View {
        if isLoading && leaders.isEmpty {
            VStack {
                Spacer()
                ProgressView()
                Spacer()
            }
        } else if leaders.isEmpty {
            VStack(spacing: 6) {
                Spacer()
                Image(systemName: "list.number")
                    .font(.title)
                    .foregroundStyle(.tertiary)
                Text("No qualifying leaders")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                Spacer()
            }
        } else {
            List {
                ForEach(Array(leaders.enumerated()), id: \.offset) { idx, card in
                    Button {
                        onTapPlayer(card.player)
                    } label: {
                        row(rank: idx + 1, card: card)
                    }
                    .buttonStyle(.plain)
                    .listRowInsets(EdgeInsets(top: 6, leading: 16, bottom: 6, trailing: 16))
                }
            }
            .listStyle(.plain)
        }
    }

    private func row(rank: Int, card: LeaderCard) -> some View {
        HStack(spacing: 12) {
            Text("\(rank)")
                .font(.title3.weight(.bold))
                .monospacedDigit()
                .foregroundStyle(rank <= 3 ? tint : .secondary)
                .frame(width: 28, alignment: .center)
            headshot(url: card.player.largeHeadshotURL)
            VStack(alignment: .leading, spacing: 2) {
                Text(card.player.name)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                Text(positionLine(for: card.player))
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Text(Self.formatValue(card.value, stat: card.stat))
                .font(.title3.weight(.bold))
                .monospacedDigit()
                .foregroundStyle(.primary)
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
        .frame(width: 32, height: 32)
        .background(Circle().fill(.ultraThinMaterial))
        .clipShape(Circle())
        .overlay(Circle().strokeBorder(.quaternary, lineWidth: 0.5))
    }

    private func positionLine(for player: PlayerSearchResult) -> String {
        let pos = player.position.flatMap { $0.isEmpty ? nil : $0 }
        let team = player.teamCode.flatMap { $0.isEmpty ? nil : teamAbbreviation(for: $0) }
        switch (pos, team) {
        case let (p?, t?): return "\(p) · \(t)"
        case let (p?, nil): return p
        case let (nil, t?): return t
        default: return "—"
        }
    }

    private func load() async {
        isLoading = true
        let playerType: String = role == .batting ? "batter" : "pitcher"
        leaders = await vm.loadTeamLeaderboard(
            stat: stat, playerType: playerType,
        )
        isLoading = false
    }

    /// Same precision rules the Home Team Leaders strip uses, so the
    /// see-all sheet renders identical numbers to the cards above —
    /// except batting rate stats strip the leading "0." for the
    /// baseball convention (".305" rather than "0.305"). Negative
    /// values are passed through with the minus sign intact.
    static func formatValue(_ v: Double?, stat: String) -> String {
        guard let v else { return "—" }
        switch stat {
        case "AVG", "OBP", "SLG", "OPS":
            let s = String(format: "%.3f", v)
            if s.hasPrefix("0.")  { return String(s.dropFirst()) }
            if s.hasPrefix("-0.") { return "-" + String(s.dropFirst(2)) }
            return s
        case "ERA", "WHIP", "FIP":
            return String(format: "%.2f", v)
        case "HR", "RBI", "SO", "W", "L", "SV", "H", "BB", "R", "SB",
             "2B", "3B", "CG", "SHO":
            return String(Int(v.rounded()))
        default:
            return String(format: "%.1f", v)
        }
    }
}
