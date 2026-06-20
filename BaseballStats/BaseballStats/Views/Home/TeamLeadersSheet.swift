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
    @State private var stat: String = LeaderboardsViewModel.defaultBattingStat
    @State private var leaders: [LeaderCard] = []
    @State private var isLoading: Bool = false

    /// Season / All-Time / Career, reusing the main Leaders tab's enum so
    /// the two screens stay identical (same labels, same API mode strings).
    @State private var mode: LeaderboardsViewModel.Mode = .season
    /// Single-season selection (Season mode only). Defaults to current year.
    @State private var year: Int = LeaderboardsViewModel.currentYear

    /// Season-mode year menu range — current year down to 1900, newest
    /// first. Mirrors the main Leaders tab's single-year picker floor.
    private static let seasonYears: [Int] =
        Array((1900...LeaderboardsViewModel.currentYear).reversed())

    /// Reuse the main Leaders tab's stat lists + defaults so the two
    /// screens offer the exact same stats in the same order.
    private var currentStats: [String] {
        role == .batting
            ? LeaderboardsViewModel.battingStats
            : LeaderboardsViewModel.pitchingStats
    }

    private var defaultStatForRole: String {
        role == .batting
            ? LeaderboardsViewModel.defaultBattingStat
            : LeaderboardsViewModel.defaultPitchingStat
    }

    private var tint: Color {
        TeamColors.color(for: entry.lahmanCode) ?? .accentColor
    }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                topControls

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
                // Single-year picker — only in Season mode, mirroring the
                // main Leaders tab's toolbar control (bare menu, no label).
                ToolbarItem(placement: .topBarTrailing) {
                    if mode.usesYear { yearMenu }
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
        // Glass sheet — matches the app-wide sheet treatment.
        .presentationBackground(.ultraThinMaterial)
        .task {
            // Initial fetch — restore the user's last tapped stat
            // (if any). Otherwise use the role's default.
            if let saved = vm.selectedLeaderStat,
               currentStats.contains(saved) {
                stat = saved
            } else {
                stat = defaultStatForRole
            }
            await load()
        }
        .onChange(of: role) { _, _ in
            // Clear stale rows immediately so the role flip doesn't
            // briefly show the previous side's numbers while the new
            // fetch is in flight. Reset to the role's default stat
            // (WAR), mirroring the main tab's resetStatForCurrentKind.
            leaders = []
            let newDefault = defaultStatForRole
            if stat == newDefault {
                // Same stat key on both sides (WAR heads both): assigning
                // `stat` is a no-op so `.onChange(of: stat)` won't fire —
                // load directly so the new role's numbers actually land.
                Task { await load() }
            } else {
                stat = newDefault
            }
        }
        .onChange(of: stat) { _, newValue in
            vm.selectedLeaderStat = newValue
            Task { await load() }
        }
        .onChange(of: mode) { _, _ in
            // Clear stale rows so the mode flip doesn't briefly show the
            // previous mode's numbers while the new fetch is in flight.
            leaders = []
            Task { await load() }
        }
        .onChange(of: year) { _, _ in
            leaders = []
            Task { await load() }
        }
    }

    /// Mirrors the main Leaders tab's stacked controls: the Season /
    /// All-Time / Career mode picker on top, then a row of
    /// [Batting/Pitching segmented | stat menu pill]. The single-year
    /// picker lives in the toolbar (Season mode only), same as the main tab.
    private var topControls: some View {
        VStack(spacing: 10) {
            modePicker
            HStack(spacing: 10) {
                Picker("Role", selection: $role) {
                    Text("Batting").tag(Role.batting)
                    Text("Pitching").tag(Role.pitching)
                }
                .pickerStyle(.segmented)
                .frame(maxWidth: .infinity)

                statMenu
            }
        }
        .padding(.horizontal, 16)
        .padding(.top, 10)
        .padding(.bottom, 8)
    }

    private var modePicker: some View {
        Picker("Mode", selection: $mode) {
            ForEach(LeaderboardsViewModel.Mode.allCases) { m in
                Text(m.label).tag(m)
            }
        }
        .pickerStyle(.segmented)
    }

    /// Glass-capsule stat menu, identical to the main Leaders tab's
    /// `statMenu`. `displayName` renders "SO/9" as "K/9", etc.
    private var statMenu: some View {
        Menu {
            Picker("Stat", selection: $stat) {
                ForEach(currentStats, id: \.self) { s in
                    Text(LeaderboardsViewModel.displayName(s)).tag(s)
                }
            }
        } label: {
            HStack(spacing: 4) {
                Text(LeaderboardsViewModel.displayName(stat))
                    .font(.subheadline.weight(.semibold))
                Image(systemName: "chevron.down")
                    .font(.caption2.weight(.semibold))
            }
            .foregroundStyle(.primary)
            .padding(.horizontal, 12)
            .padding(.vertical, 7)
            .glassEffect(.regular, in: Capsule())
        }
    }

    /// Bare single-year menu for the toolbar — newest first, floored at
    /// 1900. Matches the main Leaders tab's year picker exactly.
    private var yearMenu: some View {
        Picker("Year", selection: $year) {
            ForEach(Self.seasonYears, id: \.self) { y in
                Text(String(y)).tag(y)
            }
        }
        .pickerStyle(.menu)
        .labelsHidden()
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
            .scrollContentBackground(.hidden)
        }
    }

    /// Mirrors `LeaderboardRow` minus the headshot and team line (both
    /// redundant for a single franchise): rank · name + heat badge + HOF ·
    /// value. In All-Time mode a secondary line shows the season the mark
    /// was set. Value formatting is shared with the main tab.
    private func row(rank: Int, card: LeaderCard) -> some View {
        HStack(spacing: 12) {
            Text("\(rank)")
                .font(.callout.weight(.semibold))
                .monospacedDigit()
                .foregroundStyle(.secondary)
                .frame(width: 26, alignment: .trailing)

            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(card.player.name)
                        .font(.title3.weight(.semibold))
                        .foregroundStyle(.primary)
                        .lineLimit(1)
                        .minimumScaleFactor(0.75)
                    // Side-specific recent-form accent. Hidden for nil/neutral.
                    HeatTierBadge(tier: card.player.heat_tier)
                    if card.player.is_hof == true { hofBadge }
                }
                // All-Time rows: the season this single-season mark occurred.
                if mode == .allTime, let y = card.year {
                    Text(String(y))
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .monospacedDigit()
                }
            }

            Spacer(minLength: 8)

            Text(LeaderboardRow.formatted(
                card.value,
                as: LeaderboardRow.valueFormat(for: card.stat),
            ))
                .font(.title3.weight(.semibold))
                .monospacedDigit()
                .foregroundStyle(.primary)
        }
        .contentShape(Rectangle())
    }

    /// HOF capsule, identical to the one in `LeaderboardRow` /
    /// `PlayerSearchResultRow` so the indicator reads the same everywhere.
    private var hofBadge: some View {
        Text("HOF")
            .font(.caption2.weight(.bold))
            .foregroundStyle(.white)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(Capsule().fill(Self.baseballRed.gradient))
            .accessibilityLabel("Hall of Fame")
    }

    private static let baseballRed = Color(red: 0.8, green: 0.1, blue: 0.1)

    private func load() async {
        isLoading = true
        let playerType: String = role == .batting ? "batter" : "pitcher"
        leaders = await vm.loadTeamLeaderboard(
            stat: stat, playerType: playerType,
            mode: mode.rawValue,
            // Year applies only in Season mode; all-time / career run
            // unbounded over the full franchise history.
            year: mode.usesYear ? year : nil,
        )
        isLoading = false
    }

    /// Precision rules for the franchise-history sheet (a separate screen
    /// that still consumes this helper). The Team Leaders ROWS above use
    /// `LeaderboardRow.formatted` to match the main Leaders tab; this stays
    /// for `TeamHistorySheet`, which renders IP in baseball notation
    /// (the decimal is OUTS) rather than the main tab's grouped decimal.
    /// Batting rate stats strip the leading "0." per baseball convention.
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
            // Baseball-notation innings: the decimal is OUTS, not a true
            // fraction. 120.333 → "120.1", 120.667 → "120.2", 120.0 → "120.0".
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
