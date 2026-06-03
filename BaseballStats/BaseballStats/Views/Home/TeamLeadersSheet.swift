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
        NavigationStack {
            VStack(spacing: 0) {
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
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .principal) {
                    HStack(spacing: 6) {
                        TeamLogoView(team: entry.teamInfo, size: 22)
                        Text("Team Leaders")
                            .font(.headline.weight(.semibold))
                    }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Close") { dismiss() }
                }
            }
            // Profiles push onto the sheet's own nav stack — back
            // returns to the leaderboard instead of dismissing.
            .navigationDestination(for: PlayerSearchResult.self) { player in
                PlayerProfileView(player: player)
            }
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

    private var statPillRow: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(currentStats, id: \.self) { s in
                    let selected = (stat == s)
                    Button {
                        stat = s
                    } label: {
                        Text(s)
                            .font(.footnote.weight(.semibold))
                            .padding(.horizontal, 12)
                            .padding(.vertical, 6)
                            .background(
                                RoundedRectangle(cornerRadius: 8, style: .continuous)
                                    .fill(selected ? tint : Color(.systemFill).opacity(0.35))
                            )
                            .overlay(
                                RoundedRectangle(cornerRadius: 8, style: .continuous)
                                    .strokeBorder(
                                        selected ? Color.clear : Color(.separator).opacity(0.5),
                                        lineWidth: 0.5,
                                    )
                            )
                            .foregroundStyle(selected ? .white : .primary)
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 6)
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
                    NavigationLink(value: card.player) {
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
            Text(card.player.name)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.primary)
                .lineLimit(1)
                .minimumScaleFactor(0.8)
            Spacer()
            Text(Self.formatValue(card.value, stat: card.stat))
                .font(.title3.weight(.bold))
                .monospacedDigit()
                .foregroundStyle(.primary)
        }
        .contentShape(Rectangle())
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
        case "IP":
            // Baseball-notation innings: the decimal is OUTS, not
            // a true fraction. 120.333 → "120.1", 120.667 → "120.2",
            // 120.0 → "120.0". Same conversion as the Home-tab
            // leader strip.
            let whole = Int(v.rounded(.down))
            let frac  = v - Double(whole)
            let outs: String
            if      frac < 0.17 { outs = ".0" }
            else if frac < 0.5  { outs = ".1" }
            else                { outs = ".2" }
            return "\(whole)\(outs)"
        default:
            return String(format: "%.1f", v)
        }
    }
}
