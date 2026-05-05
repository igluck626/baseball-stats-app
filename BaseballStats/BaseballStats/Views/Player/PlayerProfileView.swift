//
//  PlayerProfileView.swift
//  BaseballStats
//
//  Player detail screen: cinematic full-bleed header up top, then a
//  segmented control switching between Overview / Career / Game Logs.
//
//  Role handling: batter-only players see batting content directly;
//  pitcher-only players see pitching content directly; two-way players
//  (Ohtani, etc.) get an extra "Batting / Pitching" role picker above
//  the tab bar that drives both Overview and Career. Game Logs is still
//  a placeholder for everyone.
//

import SwiftUI

struct PlayerProfileView: View {
    let player: PlayerSearchResult
    @StateObject private var viewModel: PlayerViewModel
    @State private var selectedTab: Tab = .overview
    @State private var selectedRole: Role = .batting

    enum Tab: String, CaseIterable, Identifiable {
        case overview  = "Overview"
        case career    = "Career"
        case gameLogs  = "Game Logs"
        var id: String { rawValue }
    }

    enum Role: String, CaseIterable, Identifiable {
        case batting  = "Batting"
        case pitching = "Pitching"
        var id: String { rawValue }
    }

    init(player: PlayerSearchResult) {
        self.player = player
        _viewModel = StateObject(wrappedValue: PlayerViewModel(player: player))
    }

    var body: some View {
        ScrollView {
            VStack(spacing: 0) {
                header
                VStack(spacing: 16) {
                    if viewModel.isTwoWay {
                        roleSelector
                    }
                    tabSelector
                    tabContent
                }
                .padding(.horizontal, 16)
                .padding(.top, 16)
                .padding(.bottom, 32)
            }
        }
        .ignoresSafeArea(edges: .top)
        .background(Color(.systemBackground))
        .navigationBarTitleDisplayMode(.inline)
        // Transparent toolbar so the header bleeds under the back chevron.
        .toolbarBackground(.hidden, for: .navigationBar)
        .toolbarColorScheme(.dark, for: .navigationBar)
        .task { await viewModel.loadData() }
    }

    // MARK: - Header

    private var header: some View {
        ZStack(alignment: .bottomLeading) {
            headshot
                .frame(height: 360)
                .frame(maxWidth: .infinity)
                .clipped()

            // Fade from clear in the upper half down to near-black at the
            // bottom so the player name reads cleanly over any image.
            LinearGradient(
                colors: [.clear, .black.opacity(0.85)],
                startPoint: UnitPoint(x: 0.5, y: 0.45),
                endPoint:   .bottom
            )
            .frame(height: 360)
            .allowsHitTesting(false)

            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(player.name)
                        .font(.largeTitle.bold())
                        .foregroundStyle(.white)
                        .lineLimit(1)
                        .minimumScaleFactor(0.6)

                    if player.is_hof == true {
                        hofBadge
                    }
                }

                if let subtitle = headerSubtitle {
                    Text(subtitle)
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(.white.opacity(0.9))
                        .lineLimit(1)
                }

                if let detail = headerDetail {
                    Text(detail)
                        .font(.caption)
                        .foregroundStyle(.white.opacity(0.75))
                }
            }
            .padding(.horizontal, 20)
            .padding(.bottom, 24)
        }
    }

    private var headshot: some View {
        AsyncImage(url: URL(string: player.headshot_url ?? "")) { phase in
            switch phase {
            case .success(let image):
                image.resizable().scaledToFill()
            case .empty:
                LinearGradient(
                    colors: [Color(.systemGray3), Color(.systemGray5)],
                    startPoint: .top, endPoint: .bottom
                )
            case .failure:
                Color(.systemGray4)
                    .overlay(
                        Image(systemName: "person.fill")
                            .font(.system(size: 100))
                            .foregroundStyle(.white.opacity(0.4))
                    )
            @unknown default:
                Color(.systemGray4)
            }
        }
    }

    private var hofBadge: some View {
        Text("HOF")
            .font(.caption.weight(.bold))
            .foregroundStyle(.white)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(
                Capsule().fill(Color(red: 0.8, green: 0.1, blue: 0.1).gradient)
            )
            .accessibilityLabel("Hall of Fame")
    }

    /// "RF · New York Yankees" — same logic as the search row.
    private var headerSubtitle: String? {
        let pos  = nonEmpty(player.position)
        let team = nonEmpty(player.teamCode).flatMap(PlayerSearchResultRow.teamFullName(for:))
        switch (pos, team) {
        case let (p?, t?): return "\(p) · \(t)"
        case let (p?, nil): return p
        case let (nil, t?): return t
        default: return nil
        }
    }

    /// "Bats: R · Throws: R · Active"
    private var headerDetail: String? {
        var parts: [String] = []
        if let bats = nonEmpty(player.bats)        { parts.append("Bats: \(bats)") }
        if let arm  = nonEmpty(player.throwingArm) { parts.append("Throws: \(arm)") }
        parts.append(isActive ? "Active" : "Retired")
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    /// Active when we have no last-season info (rookies whose row hasn't
    /// landed yet) or when the recorded last season is the current year.
    /// "Retired" requires a known mlb_last_season strictly before the
    /// current year.
    private var isActive: Bool {
        guard let last = player.mlb_last_season else { return true }
        let currentYear = Calendar.current.component(.year, from: Date())
        return last >= currentYear
    }

    // MARK: - Selectors

    private var roleSelector: some View {
        Picker("Role", selection: $selectedRole) {
            ForEach(Role.allCases) { role in
                Text(role.rawValue).tag(role)
            }
        }
        .pickerStyle(.segmented)
    }

    private var tabSelector: some View {
        Picker("Section", selection: $selectedTab) {
            ForEach(Tab.allCases) { tab in
                Text(tab.rawValue).tag(tab)
            }
        }
        .pickerStyle(.segmented)
    }

    // MARK: - Tab routing

    /// Decides which role's content to show. For two-way players, follows
    /// the role picker. For single-role players, the player's actual role
    /// wins regardless of any stale `selectedRole` state. Defaults to
    /// batting before any data has loaded so the loading state has a
    /// consistent visual.
    private var showingBatting: Bool {
        if viewModel.isTwoWay {
            return selectedRole == .batting
        }
        if viewModel.isPitcher && !viewModel.isBatter {
            return false
        }
        return true
    }

    @ViewBuilder
    private var tabContent: some View {
        Group {
            switch selectedTab {
            case .overview: overviewTab
            case .career:   careerTab
            case .gameLogs: gameLogsTab
            }
        }
        .animation(.easeInOut(duration: 0.18), value: selectedTab)
        .animation(.easeInOut(duration: 0.18), value: selectedRole)
    }

    // MARK: - Overview

    @ViewBuilder
    private var overviewTab: some View {
        if showingBatting {
            battingOverview
        } else {
            pitchingOverview
        }
    }

    @ViewBuilder
    private var battingOverview: some View {
        VStack(spacing: 16) {
            if viewModel.isLoadingCurrentBatting && viewModel.currentBatting == nil {
                loadingCard
            } else if let stats = viewModel.currentBatting {
                battingCurrentStatsCard(stats)
                battingWARBreakdownCard(stats.advanced)
            } else if let error = viewModel.error {
                errorCard(error)
            } else {
                noStatsCard("No current season batting stats")
            }
        }
    }

    @ViewBuilder
    private var pitchingOverview: some View {
        VStack(spacing: 16) {
            if viewModel.isLoadingCurrentPitching && viewModel.currentPitching == nil {
                loadingCard
            } else if let stats = viewModel.currentPitching {
                pitchingCurrentStatsCard(stats)
                pitchingAdvancedCard(stats.advanced)
            } else if let error = viewModel.error {
                errorCard(error)
            } else {
                noStatsCard("No current season pitching stats")
            }
        }
    }

    // MARK: - Batting cards

    private func battingCurrentStatsCard(_ stats: PlayerCurrentStats) -> some View {
        VStack(spacing: 14) {
            HStack {
                Text("\(String(stats.season)) Season")
                    .font(.headline)
                Spacer()
                if let team = nonEmpty(stats.standard?.team) {
                    Text(team)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
            }

            // Hero WAR
            heroStat(value: formatWAR(stats.advanced?.WAR), label: "WAR")

            Divider()

            HStack(spacing: 0) {
                StatBlock(label: "AVG", value: format3(stats.standard?.BA))
                StatBlock(label: "OBP", value: format3(stats.standard?.OBP))
                StatBlock(label: "SLG", value: format3(stats.standard?.SLG))
            }

            HStack(spacing: 0) {
                StatBlock(label: "HR",  value: formatInt(stats.standard?.HR))
                StatBlock(label: "RBI", value: formatInt(stats.standard?.RBI))
                StatBlock(label: "SB",  value: formatInt(stats.standard?.SB))
            }
        }
        .padding(20)
        .frame(maxWidth: .infinity)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 20))
    }

    private func battingWARBreakdownCard(_ advanced: BattingAdvancedStats?) -> some View {
        let off = advanced?.WAR_off ?? 0
        let def = advanced?.WAR_def ?? 0
        let total = max(abs(off) + abs(def), 0.001)

        return VStack(alignment: .leading, spacing: 14) {
            Text("WAR Breakdown")
                .font(.headline)

            warBar(label: "Offense", value: advanced?.WAR_off, fraction: abs(off) / total, tint: .blue)
            warBar(label: "Defense", value: advanced?.WAR_def, fraction: abs(def) / total, tint: .green)
        }
        .padding(20)
        .frame(maxWidth: .infinity)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 20))
    }

    // MARK: - Pitching cards

    private func pitchingCurrentStatsCard(_ stats: PitcherCurrentStats) -> some View {
        VStack(spacing: 14) {
            HStack {
                Text("\(String(stats.season)) Season")
                    .font(.headline)
                Spacer()
                if let team = nonEmpty(stats.standard?.team) {
                    Text(team)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
            }

            // Hero ERA
            heroStat(value: format2(stats.standard?.ERA), label: "ERA")

            Divider()

            HStack(spacing: 0) {
                StatBlock(label: "W-L", value: formatWL(stats.standard?.W, stats.standard?.L))
                StatBlock(label: "IP",  value: formatIP(stats.standard?.IP))
                StatBlock(label: "SO",  value: formatInt(stats.standard?.SO))
            }

            HStack(spacing: 0) {
                StatBlock(label: "WHIP", value: format2(stats.standard?.WHIP))
                StatBlock(label: "FIP",  value: format2(stats.standard?.FIP))
                StatBlock(label: "K/9",  value: format2(stats.standard?.K_per9))
            }
        }
        .padding(20)
        .frame(maxWidth: .infinity)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 20))
    }

    /// Pitching equivalent of the WAR Breakdown card. Pitching WAR isn't
    /// split into off/def, so this card surfaces the headline advanced
    /// metrics (WAR, WAA, ERA+) instead.
    private func pitchingAdvancedCard(_ advanced: PitcherAdvancedStats?) -> some View {
        VStack(spacing: 14) {
            Text("Advanced")
                .font(.headline)
                .frame(maxWidth: .infinity, alignment: .leading)

            HStack(spacing: 0) {
                StatBlock(label: "WAR",  value: formatWAR(advanced?.WAR))
                StatBlock(label: "WAA",  value: formatWAR(advanced?.WAA))
                // ERA+ is a rate stat that's conventionally shown as a
                // rounded integer (100 = league average).
                StatBlock(label: "ERA+", value: formatRoundedInt(advanced?.ERA_plus))
            }
        }
        .padding(20)
        .frame(maxWidth: .infinity)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 20))
    }

    // MARK: - Shared overview helpers

    private func heroStat(value: String, label: String) -> some View {
        VStack(spacing: 0) {
            Text(value)
                .font(.system(size: 56, weight: .bold, design: .rounded))
                .monospacedDigit()
            Text(label)
                .font(.caption2.weight(.bold))
                .foregroundStyle(.secondary)
                .tracking(1.2)
        }
        .padding(.vertical, 4)
    }

    private func warBar(label: String, value: Double?, fraction: Double, tint: Color) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(label).font(.subheadline)
                Spacer()
                Text(formatWAR(value))
                    .font(.subheadline.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            ProgressView(value: max(0, min(1, fraction)))
                .tint(tint)
        }
    }

    private func noStatsCard(_ title: String) -> some View {
        VStack(spacing: 10) {
            Image(systemName: "chart.bar")
                .font(.system(size: 36))
                .foregroundStyle(.secondary)
            Text(title)
                .font(.headline)
            Text("This player has no recorded stats here.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
        .padding(24)
        .frame(maxWidth: .infinity)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 20))
    }

    private func errorCard(_ message: String) -> some View {
        VStack(spacing: 10) {
            Image(systemName: "exclamationmark.triangle")
                .font(.system(size: 36))
                .foregroundStyle(.orange)
            Text("Couldn't load stats").font(.headline)
            Text(message)
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            Button("Try Again") { Task { await viewModel.loadData() } }
                .buttonStyle(.borderedProminent)
        }
        .padding(24)
        .frame(maxWidth: .infinity)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 20))
    }

    private var loadingCard: some View {
        ProgressView()
            .controlSize(.large)
            .frame(maxWidth: .infinity, minHeight: 180)
            .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 20))
    }

    // MARK: - Career

    @ViewBuilder
    private var careerTab: some View {
        if showingBatting {
            battingCareer
        } else {
            pitchingCareer
        }
    }

    @ViewBuilder
    private var battingCareer: some View {
        if viewModel.isLoadingCareerBatting && viewModel.careerBatting == nil {
            loadingCard
        } else if let career = viewModel.careerBatting,
                  let seasons = career.seasons, !seasons.isEmpty {
            battingCareerTable(seasons: seasons, totals: career.career_totals)
        } else {
            noStatsCard("No batting career stats")
        }
    }

    @ViewBuilder
    private var pitchingCareer: some View {
        if viewModel.isLoadingCareerPitching && viewModel.careerPitching == nil {
            loadingCard
        } else if let career = viewModel.careerPitching,
                  let seasons = career.seasons, !seasons.isEmpty {
            pitchingCareerTable(seasons: seasons, totals: career.career_totals)
        } else {
            noStatsCard("No pitching career stats")
        }
    }

    private func battingCareerTable(seasons: [CareerSeason], totals: CareerTotals?) -> some View {
        let sorted = seasons.sorted { ($0.year ?? 0) > ($1.year ?? 0) }
        return VStack(spacing: 0) {
            BattingCareerHeaderRow()
            Divider()
            ForEach(Array(sorted.enumerated()), id: \.offset) { index, season in
                BattingCareerSeasonRow(season: season)
                if index != sorted.indices.last {
                    Divider().opacity(0.4)
                }
            }
            if let totals {
                Divider()
                BattingCareerTotalsRow(totals: totals)
            }
        }
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 16))
    }

    private func pitchingCareerTable(seasons: [PitcherCareerSeason], totals: PitcherCareerTotals?) -> some View {
        let sorted = seasons.sorted { ($0.year ?? 0) > ($1.year ?? 0) }
        return VStack(spacing: 0) {
            PitchingCareerHeaderRow()
            Divider()
            ForEach(Array(sorted.enumerated()), id: \.offset) { index, season in
                PitchingCareerSeasonRow(season: season)
                if index != sorted.indices.last {
                    Divider().opacity(0.4)
                }
            }
            if let totals {
                Divider()
                PitchingCareerTotalsRow(totals: totals)
            }
        }
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 16))
    }

    // MARK: - Game Logs

    private var gameLogsTab: some View {
        VStack(spacing: 12) {
            Image(systemName: "list.bullet.rectangle.portrait")
                .font(.system(size: 40))
                .foregroundStyle(.secondary)
            Text("Game logs coming soon")
                .font(.headline)
            Text("Per-game logs and rolling 5/10/15/30 splits will land here.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
        .padding(28)
        .frame(maxWidth: .infinity)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 20))
    }
}

// MARK: - Stat block

private struct StatBlock: View {
    let label: String
    let value: String

    var body: some View {
        VStack(spacing: 2) {
            Text(value)
                .font(.title2.weight(.semibold))
                .monospacedDigit()
            Text(label)
                .font(.caption2.weight(.bold))
                .foregroundStyle(.secondary)
                .tracking(0.8)
        }
        .frame(maxWidth: .infinity)
    }
}

// MARK: - Batting career table

// Column widths sized for batting career totals (4-digit games/RBI,
// 3-digit HR, signed 3-character WAR like "-1.2"). Header and data rows
// share these constants so columns line up.
private enum BattingCareerColumn {
    static let year: CGFloat = 56
    static let team: CGFloat = 60
    static let games: CGFloat = 50
    static let avg: CGFloat = 56
    static let hr: CGFloat = 44
    static let rbi: CGFloat = 56
    static let war: CGFloat = 52
}

private struct BattingCareerHeaderRow: View {
    var body: some View {
        HStack(spacing: 0) {
            Text("Year").frame(width: BattingCareerColumn.year, alignment: .leading)
            Text("Team").frame(width: BattingCareerColumn.team, alignment: .leading)
            Spacer(minLength: 4)
            Text("G").frame(width: BattingCareerColumn.games, alignment: .trailing)
            Text("AVG").frame(width: BattingCareerColumn.avg, alignment: .trailing)
            Text("HR").frame(width: BattingCareerColumn.hr, alignment: .trailing)
            Text("RBI").frame(width: BattingCareerColumn.rbi, alignment: .trailing)
            Text("WAR").frame(width: BattingCareerColumn.war, alignment: .trailing)
        }
        .font(.caption.weight(.bold))
        .foregroundStyle(.secondary)
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }
}

private struct BattingCareerSeasonRow: View {
    let season: CareerSeason
    var body: some View {
        HStack(spacing: 0) {
            Text(formatYear(season.year))
                .frame(width: BattingCareerColumn.year, alignment: .leading)
            // Raw season.team — no lookup. Backend stores Lahman codes for
            // most rows; current-season rows can carry a city which will
            // truncate.
            Text(season.team ?? "—")
                .lineLimit(1)
                .truncationMode(.tail)
                .frame(width: BattingCareerColumn.team, alignment: .leading)
            Spacer(minLength: 4)
            Text(formatInt(season.G)).frame(width: BattingCareerColumn.games, alignment: .trailing).monospacedDigit()
            Text(format3(season.BA)).frame(width: BattingCareerColumn.avg, alignment: .trailing).monospacedDigit()
            Text(formatInt(season.HR)).frame(width: BattingCareerColumn.hr, alignment: .trailing).monospacedDigit()
            Text(formatInt(season.RBI)).frame(width: BattingCareerColumn.rbi, alignment: .trailing).monospacedDigit()
            Text(formatWAR(season.WAR)).frame(width: BattingCareerColumn.war, alignment: .trailing).monospacedDigit()
        }
        .font(.subheadline)
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
    }
}

private struct BattingCareerTotalsRow: View {
    let totals: CareerTotals
    var body: some View {
        HStack(spacing: 0) {
            Text("Career").frame(width: BattingCareerColumn.year, alignment: .leading)
            Spacer(minLength: 4)
            Text(formatInt(totals.G))
                .frame(width: BattingCareerColumn.games, alignment: .trailing)
                .monospacedDigit().lineLimit(1)
            // Career batting average isn't part of the totals payload —
            // it'd need to be derived from the seasons array. Punted.
            Text("—")
                .frame(width: BattingCareerColumn.avg, alignment: .trailing)
                .foregroundStyle(.tertiary)
            Text(formatInt(totals.HR))
                .frame(width: BattingCareerColumn.hr, alignment: .trailing)
                .monospacedDigit().lineLimit(1)
            Text(formatInt(totals.RBI))
                .frame(width: BattingCareerColumn.rbi, alignment: .trailing)
                .monospacedDigit().lineLimit(1)
            Text(formatWAR(totals.WAR))
                .frame(width: BattingCareerColumn.war, alignment: .trailing)
                .monospacedDigit().lineLimit(1)
        }
        .font(.subheadline.weight(.semibold))
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
    }
}

// MARK: - Pitching career table

// Pitching column layout — W-L combined into one cell, no AVG, ERA shown
// to two decimals.
private enum PitchingCareerColumn {
    static let year: CGFloat = 56
    static let team: CGFloat = 50
    static let wl: CGFloat = 60
    static let era: CGFloat = 56
    static let so: CGFloat = 56
    static let war: CGFloat = 52
}

private struct PitchingCareerHeaderRow: View {
    var body: some View {
        HStack(spacing: 0) {
            Text("Year").frame(width: PitchingCareerColumn.year, alignment: .leading)
            Text("Team").frame(width: PitchingCareerColumn.team, alignment: .leading)
            Spacer(minLength: 4)
            Text("W-L").frame(width: PitchingCareerColumn.wl, alignment: .trailing)
            Text("ERA").frame(width: PitchingCareerColumn.era, alignment: .trailing)
            Text("SO").frame(width: PitchingCareerColumn.so, alignment: .trailing)
            Text("WAR").frame(width: PitchingCareerColumn.war, alignment: .trailing)
        }
        .font(.caption.weight(.bold))
        .foregroundStyle(.secondary)
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }
}

private struct PitchingCareerSeasonRow: View {
    let season: PitcherCareerSeason
    var body: some View {
        HStack(spacing: 0) {
            Text(formatYear(season.year))
                .frame(width: PitchingCareerColumn.year, alignment: .leading)
            Text(season.team ?? "—")
                .lineLimit(1)
                .truncationMode(.tail)
                .frame(width: PitchingCareerColumn.team, alignment: .leading)
            Spacer(minLength: 4)
            Text(formatWL(season.W, season.L))
                .frame(width: PitchingCareerColumn.wl, alignment: .trailing)
                .monospacedDigit()
            Text(format2(season.ERA))
                .frame(width: PitchingCareerColumn.era, alignment: .trailing)
                .monospacedDigit()
            Text(formatInt(season.SO))
                .frame(width: PitchingCareerColumn.so, alignment: .trailing)
                .monospacedDigit()
            Text(formatWAR(season.WAR))
                .frame(width: PitchingCareerColumn.war, alignment: .trailing)
                .monospacedDigit()
        }
        .font(.subheadline)
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
    }
}

private struct PitchingCareerTotalsRow: View {
    let totals: PitcherCareerTotals
    var body: some View {
        HStack(spacing: 0) {
            Text("Career").frame(width: PitchingCareerColumn.year, alignment: .leading)
            Spacer(minLength: 4)
            Text(formatWL(totals.W, totals.L))
                .frame(width: PitchingCareerColumn.wl, alignment: .trailing)
                .monospacedDigit().lineLimit(1)
            // Career ERA isn't in the totals payload (needs IP-weighted
            // aggregation across seasons) — punted.
            Text("—")
                .frame(width: PitchingCareerColumn.era, alignment: .trailing)
                .foregroundStyle(.tertiary)
            Text(formatInt(totals.SO))
                .frame(width: PitchingCareerColumn.so, alignment: .trailing)
                .monospacedDigit().lineLimit(1)
            Text(formatWAR(totals.WAR))
                .frame(width: PitchingCareerColumn.war, alignment: .trailing)
                .monospacedDigit().lineLimit(1)
        }
        .font(.subheadline.weight(.semibold))
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
    }
}

// MARK: - Formatters

private func formatYear(_ year: Int?) -> String {
    guard let year else { return "—" }
    return String(year)
}

/// Three-decimal display with the leading "0" stripped — ".342" not "0.342",
/// matching baseball convention. Returns "—" for nil.
private func format3(_ value: Double?) -> String {
    guard let value else { return "—" }
    let s = String(format: "%.3f", value)
    if s.hasPrefix("0.")  { return String(s.dropFirst()) }
    if s.hasPrefix("-0.") { return "-" + String(s.dropFirst(2)) }
    return s
}

/// Two-decimal display — used for ERA, WHIP, FIP, K/9. ERAs above 1
/// keep the leading digit, so unlike `format3` we don't strip a leading
/// zero. Returns "—" for nil.
private func format2(_ value: Double?) -> String {
    guard let value else { return "—" }
    return String(format: "%.2f", value)
}

private func formatInt(_ value: Int?) -> String {
    guard let value else { return "—" }
    return String(value)
}

/// Doubles displayed as a rounded integer — used for ERA+ and similar
/// integer-conventioned ratings.
private func formatRoundedInt(_ value: Double?) -> String {
    guard let value else { return "—" }
    return String(Int(value.rounded()))
}

private func formatWAR(_ value: Double?) -> String {
    guard let value else { return "—" }
    return String(format: "%.1f", value)
}

/// Innings pitched — one decimal (e.g. "182.1" = 182⅓ innings).
private func formatIP(_ value: Double?) -> String {
    guard let value else { return "—" }
    return String(format: "%.1f", value)
}

/// "12-8" — formats wins–losses as a single combined cell. Either side
/// missing renders as "—" on that side.
private func formatWL(_ w: Int?, _ l: Int?) -> String {
    let wStr = w.map(String.init) ?? "—"
    let lStr = l.map(String.init) ?? "—"
    return "\(wStr)-\(lStr)"
}

private func nonEmpty(_ s: String?) -> String? {
    guard let s, !s.isEmpty else { return nil }
    return s
}

#Preview {
    NavigationStack {
        PlayerProfileView(player: .init(
            player_id: 660271,
            name: "Shohei Ohtani",
            bbref_id: "ohtansh01",
            mlb_debut: 2018,
            mlb_last_season: 2026,
            currentTeam: "Los Angeles",
            teamCode: "LAN",
            position: "DH",
            bats: "L",
            throwingArm: "R",
            height: 76,
            weight: 210,
            birth_year: 1994, birth_month: 7, birth_day: 5,
            birth_city: "Oshu", birth_state: nil, birth_country: "Japan",
            debut: "2018-03-29",
            final_game: nil,
            birthdate: "1994-07-05",
            headshot_url: "https://img.mlbstatic.com/mlb-photos/image/upload/d_people:generic:headshot:67:current.png/w_213,q_auto:best/v1/people/660271/headshot/67/current",
            is_hof: false,
            hof_year: nil
        ))
    }
}
