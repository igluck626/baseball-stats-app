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
    @State private var selectedTab: Tab
    /// nil until the user explicitly toggles. While nil, the picker
    /// reflects `defaultRole`, which depends on the loaded VM data
    /// (two-way → batting; pitcher with batting history → pitching).
    @State private var selectedRole: Role?

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
        let vm = PlayerViewModel(player: player)
        _viewModel = StateObject(wrappedValue: vm)
        // Retired players land on Career; active players on Overview.
        // `isRetired` only depends on player.mlb_last_season, so it's
        // valid here even before any network data has loaded.
        _selectedTab = State(initialValue: vm.isRetired ? .career : .overview)
        _selectedRole = State(initialValue: nil)
    }

    var body: some View {
        ScrollView {
            VStack(spacing: 0) {
                header
                VStack(spacing: 16) {
                    if showsRoleSelector {
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

    /// "RF · New York Yankees" — same logic as the search row, both
    /// surfaces share the resolver in Components/TeamNames.swift.
    private var headerSubtitle: String? {
        let pos  = nonEmpty(player.position)
        let team = nonEmpty(player.teamCode).flatMap(teamFullName(for:))
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

    /// Show the Batting/Pitching toggle whenever a pitcher has any
    /// batting history at all, not just for true two-way players. This
    /// lets users see the historical batting line for, say, a deadball-
    /// era pitcher who hit a few home runs.
    private var showsRoleSelector: Bool {
        viewModel.isPitcher && viewModel.hasAnyBatting
    }

    /// The role to surface before any explicit toggle by the user.
    /// Two-way players default to batting (the marquee role); pure
    /// pitchers with batting history default to pitching (their primary
    /// role). Anything else lands on batting.
    private var defaultRole: Role {
        if viewModel.isTwoWay { return .batting }
        if viewModel.isPitcher { return .pitching }
        return .batting
    }

    /// What the picker currently reflects — user choice if they've
    /// touched the toggle, otherwise the default.
    private var effectiveRole: Role {
        selectedRole ?? defaultRole
    }

    /// Two-way binding for the picker that reads `effectiveRole` and
    /// writes through to `selectedRole` on user toggle.
    private var roleBinding: Binding<Role> {
        Binding(
            get: { effectiveRole },
            set: { selectedRole = $0 }
        )
    }

    private var roleSelector: some View {
        Picker("Role", selection: roleBinding) {
            ForEach(Role.allCases) { role in
                Text(role.rawValue).tag(role)
            }
        }
        .pickerStyle(.segmented)
    }

    /// Tabs to expose for this player. Retired players don't get Overview
    /// — there's no current season to discuss. `selectedTab` is
    /// initialized to `.career` for retired players in `init`, and the
    /// `onAppear` below is a safety net in case that invariant ever
    /// drifts (e.g. via state restoration).
    private var availableTabs: [Tab] {
        viewModel.isRetired ? [.career, .gameLogs] : [.overview, .career, .gameLogs]
    }

    private var tabSelector: some View {
        Picker("Section", selection: $selectedTab) {
            ForEach(availableTabs) { tab in
                Text(tab.rawValue).tag(tab)
            }
        }
        .pickerStyle(.segmented)
        .onAppear {
            if !availableTabs.contains(selectedTab) {
                selectedTab = availableTabs.first ?? .career
            }
        }
    }

    // MARK: - Tab routing

    /// Decides which role's content to show.
    /// 1. Toggle visible (pitcher with any batting history) → follow
    ///    the picker (which itself defaults via `defaultRole`).
    /// 2. Threshold batter only → batting.
    /// 3. Sub-threshold fallback → whichever side has data, batting
    ///    otherwise.
    private var showingBatting: Bool {
        if showsRoleSelector {
            return effectiveRole == .batting
        }
        if viewModel.isBatter && !viewModel.isPitcher {
            return true
        }
        if !viewModel.hasAnyBatting && viewModel.hasAnyPitching {
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
        .animation(.easeInOut(duration: 0.18), value: effectiveRole)
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

    /// Three stacked cards: current season → career → bio. Each card
    /// renders independently — bio is always available (data lives on
    /// the search result), current/career follow their own load state.
    @ViewBuilder
    private var battingOverview: some View {
        VStack(spacing: 20) {
            battingCurrentSeasonCard
            battingCareerCard
            bioCard
        }
    }

    @ViewBuilder
    private var pitchingOverview: some View {
        VStack(spacing: 20) {
            pitchingCurrentSeasonCard
            pitchingCareerCard
            bioCard
        }
    }

    // MARK: - Current season cards (3×3 grids)

    @ViewBuilder
    private var battingCurrentSeasonCard: some View {
        if viewModel.isLoadingCurrentBatting && viewModel.currentBatting == nil {
            loadingCard
        } else if let stats = viewModel.currentBatting {
            // 5×2 grid: row 1 rate stats, row 2 counting stats.
            statsGridCard(
                title: "\(String(stats.season)) Season",
                subtitle: currentSeasonTeamName,
                items: [
                    ("AVG", format3(stats.standard?.BA)),
                    ("OBP", format3(stats.standard?.OBP)),
                    ("SLG", format3(stats.standard?.SLG)),
                    ("OPS", format3(stats.standard?.OPS)),
                    ("WAR", formatWAR(stats.advanced?.WAR)),
                    ("G",   formatCount(stats.standard?.G)),
                    ("HR",  formatCount(stats.standard?.HR)),
                    ("RBI", formatCount(stats.standard?.RBI)),
                    ("SB",  formatCount(stats.standard?.SB)),
                    ("PA",  formatCount(stats.standard?.PA)),
                ],
                style: .current
            )
        } else if let error = viewModel.error {
            errorCard(error)
        } else {
            noStatsCard("No current season batting stats")
        }
    }

    @ViewBuilder
    private var pitchingCurrentSeasonCard: some View {
        if viewModel.isLoadingCurrentPitching && viewModel.currentPitching == nil {
            loadingCard
        } else if let stats = viewModel.currentPitching {
            // 5×2 grid: row 1 rate stats, row 2 mixed (W-L, GS, IP,
            // SO, BB/9). GS replaces total G — for pitchers, starts is
            // the more meaningful workload signal.
            statsGridCard(
                title: "\(String(stats.season)) Season",
                subtitle: currentSeasonTeamName,
                items: [
                    ("ERA",  format2(stats.standard?.ERA)),
                    ("WHIP", format2(stats.standard?.WHIP)),
                    ("FIP",  format2(stats.standard?.FIP)),
                    ("K/9",  format2(stats.standard?.K_per9)),
                    ("WAR",  formatWAR(stats.advanced?.WAR)),
                    ("W-L",  formatWL(stats.standard?.W, stats.standard?.L)),
                    ("GS",   formatCount(stats.standard?.GS)),
                    ("IP",   formatIP(stats.standard?.IP)),
                    ("SO",   formatCount(stats.standard?.SO)),
                    ("BB/9", format2(stats.standard?.BB_per9)),
                ],
                style: .current
            )
        } else if let error = viewModel.error {
            errorCard(error)
        } else {
            noStatsCard("No current season pitching stats")
        }
    }

    /// Full team name for the season-card subtitle, resolved from the
    /// player's normalized `teamCode`. Prefer this over `stats.standard
    /// ?.team` because the latter can be a raw Lahman code or a
    /// city-only display string depending on which loader wrote the row.
    private var currentSeasonTeamName: String? {
        nonEmpty(player.teamCode).flatMap(teamFullName(for:))
    }

    // MARK: - Career cards (3×3 grids with derived rates)

    @ViewBuilder
    private var battingCareerCard: some View {
        if viewModel.isLoadingCareerBatting && viewModel.careerBatting == nil {
            loadingCard
        } else if let career = viewModel.careerBatting,
                  let seasons = career.seasons, !seasons.isEmpty {
            // career_totals omits AB, SB, AVG, OBP, SLG — derive from
            // the seasons array. HR / RBI / G / WAR come straight off
            // the totals payload.
            let agg = BattingCareerAgg.compute(seasons: seasons)
            let totals = career.career_totals
            // 5×2 grid mirrors the current-season layout: row 1 rate
            // stats, row 2 counting stats. Career rates derive from the
            // BattingCareerAgg (career_totals omits AVG/OBP/SLG/PA).
            // PA is summed across seasons in the agg; G comes from
            // career_totals which is already a sum.
            statsGridCard(
                title: "Career",
                subtitle: nil,
                items: [
                    ("AVG", format3(agg.avg)),
                    ("OBP", format3(agg.obp)),
                    ("SLG", format3(agg.slg)),
                    ("OPS", format3(agg.ops)),
                    ("WAR", formatWAR(totals?.WAR)),
                    ("G",   formatCount(totals?.G)),
                    ("HR",  formatCount(totals?.HR)),
                    ("RBI", formatCount(totals?.RBI)),
                    ("SB",  formatCount(agg.sb)),
                    ("PA",  formatCount(agg.pa)),
                ]
            )
        }
        // No empty state — career card is suppressed if there's no career
        // data; current-season + bio cards still carry the screen.
    }

    @ViewBuilder
    private var pitchingCareerCard: some View {
        if viewModel.isLoadingCareerPitching && viewModel.careerPitching == nil {
            loadingCard
        } else if let career = viewModel.careerPitching,
                  let seasons = career.seasons, !seasons.isEmpty {
            // career_totals carries IP/SO/W/L/WAR; ERA, WHIP, FIP, K/9,
            // CG, SV come from the seasons-level aggregate (totals
            // doesn't ship them, and ERA is IP-weighted which equals
            // sum(ER)*9/sum(IP) for linear sums).
            let agg = PitchingCareerAgg.compute(seasons: seasons)
            let totals = career.career_totals
            // Career mirrors current-season layout exactly so users see
            // a direct comparison cell-by-cell. GS and BB/9 come from
            // PitchingCareerAgg (career_totals doesn't ship either —
            // BB/9 needs IP-weighting which collapses to bb*9/sum(IP)).
            statsGridCard(
                title: "Career",
                subtitle: nil,
                items: [
                    ("ERA",  format2(agg.era)),
                    ("WHIP", format2(agg.whip)),
                    ("FIP",  format2(agg.fip)),
                    ("K/9",  format2(agg.kPer9)),
                    ("WAR",  formatWAR(totals?.WAR)),
                    ("W-L",  formatWL(totals?.W, totals?.L)),
                    ("GS",   formatCount(agg.totalGS)),
                    ("IP",   formatIP(totals?.IP)),
                    ("SO",   formatCount(totals?.SO)),
                    ("BB/9", format2(agg.careerBB9)),
                ]
            )
        }
    }

    // MARK: - Bio card

    /// Always present — data comes from the PlayerSearchResult passed
    /// into the view, no network call required.
    private var bioCard: some View {
        let rows = bioRows
        return VStack(alignment: .leading, spacing: 12) {
            Text("Player Info")
                .font(.headline)
            VStack(spacing: 0) {
                ForEach(Array(rows.enumerated()), id: \.offset) { index, row in
                    BioInfoRow(label: row.0, value: row.1)
                    if index != rows.indices.last {
                        Divider().opacity(0.4)
                    }
                }
            }
        }
        .padding(20)
        .frame(maxWidth: .infinity)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 20))
        .shadow(color: .black.opacity(0.06), radius: 8, x: 0, y: 2)
    }

    /// Rows to render in the bio card. Each entry is conditionally
    /// included based on whether the underlying data is present, so
    /// missing fields don't leave dangling labels with "—" values.
    private var bioRows: [(String, String)] {
        var rows: [(String, String)] = []
        if let pos = nonEmpty(player.position) {
            rows.append(("Position", pos))
        }
        if let bt = batsThrowsLabel {
            rows.append(("Bats / Throws", bt))
        }
        if let dob = formatLongDate(player.birthdate) {
            rows.append(("Date of Birth", dob))
        }
        if let place = placeOfBirth {
            rows.append(("Place of Birth", place))
        }
        if let h = formatHeight(player.height) {
            rows.append(("Height", h))
        }
        if let w = formatWeight(player.weight) {
            rows.append(("Weight", w))
        }
        if let debut = formatLongDate(player.debut) {
            rows.append(("MLB Debut", debut))
        }
        if viewModel.isRetired,
           let final = formatLongDate(player.final_game) {
            rows.append(("Final Game", final))
        }
        return rows
    }

    /// "R / R" or "L / —" — uses an em-dash for either side that's
    /// missing, returns nil only when both are missing.
    private var batsThrowsLabel: String? {
        let b = nonEmpty(player.bats)
        let t = nonEmpty(player.throwingArm)
        guard b != nil || t != nil else { return nil }
        return "\(b ?? "—") / \(t ?? "—")"
    }

    /// "Linden, NJ" for US-born, "Oshu, Japan" for international (state
    /// is skipped per spec when birth_country isn't USA).
    private var placeOfBirth: String? {
        let city = nonEmpty(player.birth_city)
        let state = nonEmpty(player.birth_state)
        let country = nonEmpty(player.birth_country)

        var parts: [String] = []
        if let city { parts.append(city) }
        if country == "USA" {
            if let state { parts.append(state) }
        } else if let country {
            parts.append(country)
        } else if let state {
            parts.append(state)
        }
        return parts.isEmpty ? nil : parts.joined(separator: ", ")
    }

    // MARK: - Shared overview helpers

    /// Visual treatment for `statsGridCard`. The current-season card gets
    /// a brighter, accented look; career cards use the standard
    /// material treatment shared with the bio card.
    enum CardStyle {
        case current   // Translucent white background + accent title bar
        case standard  // .ultraThinMaterial (matches bio card)
    }

    /// Card with title (+ optional subtitle on the right) and a 5×2 grid
    /// of stat blocks. Caller passes exactly 10 items in row-major order;
    /// the layout never scrolls — at iPhone widths each column gets
    /// ~70pt, comfortable for 4–5 char monospaced .callout values.
    private func statsGridCard(
        title: String,
        subtitle: String?,
        items: [(String, String)],
        style: CardStyle = .standard
    ) -> some View {
        VStack(spacing: 10) {
            HStack(spacing: 8) {
                if style == .current {
                    // Small colored bar — visual indicator that this is
                    // the headline (current-season) card.
                    RoundedRectangle(cornerRadius: 2)
                        .fill(Color.accentColor)
                        .frame(width: 4, height: 20)
                }
                Text(title).font(.headline)
                Spacer()
                if let subtitle {
                    Text(subtitle)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
            }
            statsTwoRows(items: items)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity)
        .background(cardBackground(style))
        .shadow(color: .black.opacity(0.06), radius: 8, x: 0, y: 2)
    }

    /// 5-column row × 2 rows. Each column uses .frame(maxWidth: .infinity)
    /// inside StatBlock, so the row evenly splits whatever horizontal
    /// space is available.
    private func statsTwoRows(items: [(String, String)]) -> some View {
        let row1 = Array(items.prefix(5))
        let row2 = Array(items.dropFirst(5).prefix(5))
        return VStack(spacing: 8) {
            statsRow(items: row1)
            statsRow(items: row2)
        }
    }

    private func statsRow(items: [(String, String)]) -> some View {
        HStack(spacing: 0) {
            ForEach(items.indices, id: \.self) { i in
                StatBlock(label: items[i].0, value: items[i].1)
            }
        }
    }

    /// Background fill for a stats grid card. @ViewBuilder so the two
    /// branches can return different concrete types (filled white vs.
    /// material-filled rounded rect).
    @ViewBuilder
    private func cardBackground(_ style: CardStyle) -> some View {
        let shape = RoundedRectangle(cornerRadius: 20)
        switch style {
        case .current:
            shape.fill(Color.white.opacity(0.8))
        case .standard:
            shape.fill(.ultraThinMaterial)
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

    /// `.id(showingBatting)` so flipping the role toggle recreates the
    /// inner GameLogsView (and its @StateObject VM) with the new role,
    /// rather than leaving stale data on screen until the next refresh.
    private var gameLogsTab: some View {
        GameLogsView(playerId: player.player_id, isPitcher: !showingBatting)
            .id(showingBatting)
    }
}

// MARK: - Stat block

private struct StatBlock: View {
    let label: String
    let value: String

    var body: some View {
        VStack(spacing: 2) {
            Text(value)
                .font(.callout.weight(.semibold))
                .monospacedDigit()
                .lineLimit(1)
                .minimumScaleFactor(0.8)
            Text(label)
                .font(.system(size: 9, weight: .bold))
                .foregroundStyle(.secondary)
                .tracking(0.6)
        }
        .padding(.vertical, 4)
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

/// Counting-stat display with thousands separators ("1,682"). Used for
/// career totals like career hits, career HRs, etc. — anything that can
/// realistically reach four digits over a long career. Cached
/// NumberFormatter so we don't allocate one per cell render.
private func formatCount(_ value: Int?) -> String {
    guard let value else { return "—" }
    return countFormatter.string(from: NSNumber(value: value)) ?? String(value)
}

private let countFormatter: NumberFormatter = {
    let f = NumberFormatter()
    f.numberStyle = .decimal
    return f
}()

private func formatWAR(_ value: Double?) -> String {
    guard let value else { return "—" }
    return String(format: "%.1f", value)
}

/// Innings pitched in baseball notation. Backend stores IP as a true
/// decimal (e.g. 1576.667 = 1576⅔ innings); we round the fractional
/// third to .0/.1/.2 and stitch on thousands separators for the
/// integer part. Examples:
///   1576.667 → "1,576.2"
///   37.333   → "37.1"
///   323.0    → "323.0"
///
/// Threshold logic — each frac maps to the *nearest* of {0, ⅓, ⅔}:
///   frac < 0.17  → .0   (closer to 0/3 than 1/3)
///   frac < 0.5   → .1   (closer to 1/3 than 2/3)
///   else         → .2   (closer to 2/3 than the next whole inning)
private func formatIP(_ value: Double?) -> String {
    guard let value else { return "—" }
    let whole = Int(value)
    let frac = value - Double(whole)

    let suffix: String
    if frac < 0.17 {
        suffix = ".0"
    } else if frac < 0.5 {
        suffix = ".1"
    } else {
        suffix = ".2"
    }

    let wholeStr = countFormatter.string(from: NSNumber(value: whole)) ?? String(whole)
    return wholeStr + suffix
}

/// "12-8" — formats wins–losses as a single combined cell. Treats nil
/// as 0 on either side so a reliever with 0-1 (where the backend may
/// surface W as null instead of 0) renders as "0-1", not "—-1". Only
/// shows "—" when both sides are nil, i.e. no record data at all.
private func formatWL(_ w: Int?, _ l: Int?) -> String {
    guard w != nil || l != nil else { return "—" }
    return "\(w ?? 0)-\(l ?? 0)"
}

private func nonEmpty(_ s: String?) -> String? {
    guard let s, !s.isEmpty else { return nil }
    return s
}

/// "76" inches → "6'4\"". Uses straight quotes (foot/inch marks).
/// Returns nil for nil or non-positive values so the bio row can be
/// skipped entirely.
private func formatHeight(_ inches: Int?) -> String? {
    guard let inches, inches > 0 else { return nil }
    let feet = inches / 12
    let remaining = inches % 12
    return "\(feet)'\(remaining)\""
}

private func formatWeight(_ lbs: Int?) -> String? {
    guard let lbs, lbs > 0 else { return nil }
    return "\(lbs) lbs"
}

/// "1994-07-05" → "July 5, 1994". UTC + en_US_POSIX so the formatter
/// is locale- and timezone-stable across devices. Returns nil for
/// missing input or unparseable strings.
private func formatLongDate(_ iso: String?) -> String? {
    guard let iso = nonEmpty(iso) else { return nil }
    let input = DateFormatter()
    input.dateFormat = "yyyy-MM-dd"
    input.timeZone = TimeZone(identifier: "UTC")
    input.locale = Locale(identifier: "en_US_POSIX")
    guard let date = input.date(from: iso) else { return iso }

    let output = DateFormatter()
    output.dateFormat = "MMMM d, yyyy"
    output.timeZone = TimeZone(identifier: "UTC")
    return output.string(from: date)
}

// MARK: - Bio row

/// Single label/value row inside the Player Info card. Label sits left
/// in secondary color, value right-aligned in primary with .medium
/// weight for readability.
private struct BioInfoRow: View {
    let label: String
    let value: String

    var body: some View {
        HStack {
            Text(label)
                .font(.footnote.weight(.medium))
                .foregroundStyle(.secondary)
            Spacer(minLength: 12)
            Text(value)
                .font(.subheadline.weight(.semibold))
                .multilineTextAlignment(.trailing)
        }
        .padding(.vertical, 10)
    }
}

// MARK: - Career aggregation

/// Career batting aggregates derived from the seasons array. The
/// backend's `career_totals` payload omits AB / SB / AVG / OBP / SLG,
/// so we sum on-device. Mirrors the same formulas used elsewhere in
/// the app (matches `WindowSnapshot.computeBatting` in GameLogsView).
private struct BattingCareerAgg {
    /// Career plate appearances — summed from seasons because
    /// CareerTotals doesn't ship a PA total.
    let pa: Int
    let ab: Int
    let h: Int
    let bb: Int
    let hbp: Int
    let sf: Int
    let dbl: Int
    let trp: Int
    let hr: Int
    let sb: Int

    var avg: Double? {
        ab > 0 ? Double(h) / Double(ab) : nil
    }

    var obp: Double? {
        let den = ab + bb + hbp + sf
        return den > 0 ? Double(h + bb + hbp) / Double(den) : nil
    }

    var slg: Double? {
        guard ab > 0 else { return nil }
        let singles = h - dbl - trp - hr
        let tb = singles + 2 * dbl + 3 * trp + 4 * hr
        return Double(tb) / Double(ab)
    }

    /// OPS = OBP + SLG. nil only when neither side resolved (no ABs).
    var ops: Double? {
        guard let obp, let slg else { return nil }
        return obp + slg
    }

    static func compute(seasons: [CareerSeason]) -> BattingCareerAgg {
        BattingCareerAgg(
            pa:  seasons.reduce(0) { $0 + ($1.PA ?? 0) },
            ab:  seasons.reduce(0) { $0 + ($1.AB ?? 0) },
            h:   seasons.reduce(0) { $0 + ($1.H ?? 0) },
            bb:  seasons.reduce(0) { $0 + ($1.BB ?? 0) },
            hbp: seasons.reduce(0) { $0 + ($1.HBP ?? 0) },
            sf:  seasons.reduce(0) { $0 + ($1.SF ?? 0) },
            dbl: seasons.reduce(0) { $0 + ($1.doubles ?? 0) },
            trp: seasons.reduce(0) { $0 + ($1.triples ?? 0) },
            hr:  seasons.reduce(0) { $0 + ($1.HR ?? 0) },
            sb:  seasons.reduce(0) { $0 + ($1.SB ?? 0) }
        )
    }
}

/// Career pitching aggregates — `career_totals` carries IP/SO/W/L/WAR
/// but not ER/GS/SV/G, so we sum from seasons. ERA is computed as the
/// IP-weighted equivalent: sum(ER)*9 / sum(IP), which is exactly the
/// rate over the entire career.
private struct PitchingCareerAgg {
    let ip: Double
    let er: Int
    let h: Int
    let bb: Int
    let so: Int
    let hr: Int
    let hbp: Int
    let g: Int
    let gs: Int
    let cg: Int
    let sv: Int

    var era: Double? {
        ip > 0 ? Double(er) * 9.0 / ip : nil
    }

    var whip: Double? {
        ip > 0 ? Double(bb + h) / ip : nil
    }

    /// K/9 — strikeouts per nine innings.
    var kPer9: Double? {
        ip > 0 ? Double(so) * 9.0 / ip : nil
    }

    /// Career BB/9 — career walks per nine innings. PitcherCareerTotals
    /// ships BB but not BB/9 (that needs IP-weighting, which for linear
    /// totals is just bb*9/ip).
    var careerBB9: Double? {
        ip > 0 ? Double(bb) * 9.0 / ip : nil
    }

    /// Career games started. Same value as the stored `gs` field —
    /// surfaced under a more explicit name for the career grid call
    /// site.
    var totalGS: Int { gs }

    /// FIP — fielding-independent pitching. Constant 3.10 matches the
    /// backend's `_fip` helper in data_service.py.
    var fip: Double? {
        guard ip > 0 else { return nil }
        return (13.0 * Double(hr) + 3.0 * Double(bb + hbp) - 2.0 * Double(so)) / ip + 3.10
    }

    static func compute(seasons: [PitcherCareerSeason]) -> PitchingCareerAgg {
        PitchingCareerAgg(
            ip:  seasons.reduce(0.0) { $0 + ($1.IP  ?? 0) },
            er:  seasons.reduce(0)   { $0 + ($1.ER  ?? 0) },
            h:   seasons.reduce(0)   { $0 + ($1.H   ?? 0) },
            bb:  seasons.reduce(0)   { $0 + ($1.BB  ?? 0) },
            so:  seasons.reduce(0)   { $0 + ($1.SO  ?? 0) },
            hr:  seasons.reduce(0)   { $0 + ($1.HR  ?? 0) },
            hbp: seasons.reduce(0)   { $0 + ($1.HBP ?? 0) },
            g:   seasons.reduce(0)   { $0 + ($1.G   ?? 0) },
            gs:  seasons.reduce(0)   { $0 + ($1.GS  ?? 0) },
            cg:  seasons.reduce(0)   { $0 + ($1.CG  ?? 0) },
            sv:  seasons.reduce(0)   { $0 + ($1.SV  ?? 0) }
        )
    }
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
