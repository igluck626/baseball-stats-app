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

    // Career-table column visibility. Sets contain the keys for every
    // optional column the user wants to show; core columns (WAR/G/PA/AB
    // for batting, WAR/G for pitching) are always rendered regardless.
    @State private var showingColumnFilter = false
    @State private var visibleBattingColumns: Set<String> = Self.defaultBattingColumns
    @State private var visiblePitchingColumns: Set<String> = Self.defaultPitchingColumns

    static let defaultBattingColumns: Set<String> = [
        "AVG", "OBP", "SLG", "OPS",
        "R", "H", "2B", "3B", "HR", "RBI", "SB", "BB", "SO",
    ]

    static let defaultPitchingColumns: Set<String> = [
        "ERA", "WHIP", "FIP",
        "W", "L", "W-L%", "GS", "IP", "SO", "BB", "HR",
    ]

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
        // Outer ZStack so the column-filter overlay can slide up from
        // the bottom over the profile content. We render the overlay
        // ourselves instead of using .sheet — under iOS 26 the system
        // sheet card stamps its own opaque chrome that .presentation-
        // Background can't reliably clear, so a custom panel is the
        // only way to get a real glass effect.
        ZStack(alignment: .bottom) {
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(spacing: 0) {
                        header.id("top")
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
                // Hide the default scroll content background and pin our
                // own systemGroupedBackground so the page reads as a
                // single grouped surface (subtle gray) with the
                // ultraThinMaterial cards sitting on top.
                .scrollContentBackground(.hidden)
                .background(Color(.systemGroupedBackground))
                .navigationBarTitleDisplayMode(.inline)
                // Material nav-bar matches the rest of the app's chrome.
                // No more transparent / dark-scheme overrides — the
                // header is no longer a cinematic photo, so the system
                // back chevron renders against the standard nav surface.
                .toolbarBackground(.ultraThinMaterial, for: .navigationBar)
                .task { await viewModel.loadData() }
                .onChange(of: selectedTab) { _, _ in
                    // No animation — withAnimation here would interpolate
                    // the scroll offset over time and re-introduce the
                    // mid-transition layout glitches we're fixing.
                    proxy.scrollTo("top", anchor: .top)
                }
            }

            // Dim layer + filter panel. Both are conditionally rendered
            // so the .move / .opacity transitions fire on dismiss as
            // well as present. .animation on this ZStack drives the
            // spring; explicit `withAnimation` in callbacks isn't
            // needed.
            if showingColumnFilter {
                Color.black.opacity(0.3)
                    .ignoresSafeArea()
                    .transition(.opacity)
                    .onTapGesture { showingColumnFilter = false }
                    .zIndex(1)

                columnFilterPanel
                    .transition(.move(edge: .bottom))
                    .zIndex(2)
            }
        }
        .animation(.spring(response: 0.42, dampingFraction: 0.88), value: showingColumnFilter)
    }

    /// Bridges the showingBatting state to the right catalog + binding
    /// for whichever career tab is on screen. Single source of truth
    /// for the panel — we don't need separate batting/pitching sheets
    /// since `showingBatting` already decides which career table is
    /// rendered above.
    @ViewBuilder
    private var columnFilterPanel: some View {
        if showingBatting {
            ColumnFilterPanel(
                title: "Batting Columns",
                groups: battingFilterGroups,
                visible: $visibleBattingColumns,
                defaults: Self.defaultBattingColumns,
                onDismiss: { showingColumnFilter = false }
            )
        } else {
            ColumnFilterPanel(
                title: "Pitching Columns",
                groups: pitchingFilterGroups,
                visible: $visiblePitchingColumns,
                defaults: Self.defaultPitchingColumns,
                onDismiss: { showingColumnFilter = false }
            )
        }
    }

    // MARK: - Header

    /// Card-style header — headshot on the left, identity + bio on
    /// the right. Sits in the same 16pt page padding as the cards
    /// below so the left/right edges align. HStack alignment is
    /// `.top` so the photo anchors to the upper-left of the card and
    /// the bio rows flow down beside it (the right column ends up
    /// taller than the photo).
    private var header: some View {
        HStack(alignment: .top, spacing: 16) {
            // Rounded portrait rect (90×110) — MLB headshots ship with
            // a built-in grey background, so scaledToFill on a portrait
            // rect matches the source aspect ratio: face and hat fill
            // the frame with no grey backdrop visible.
            AsyncImage(url: player.largeHeadshotURL) { image in
                image
                    .resizable()
                    .scaledToFill()
            } placeholder: {
                RoundedRectangle(cornerRadius: 12)
                    .fill(Color(.secondarySystemFill))
            }
            .frame(width: 90, height: 110)
            .clipShape(RoundedRectangle(cornerRadius: 12))

            VStack(alignment: .leading, spacing: 4) {
                Text(player.name)
                    .font(.title2.weight(.bold))
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)

                if let subtitle = headerSubtitle {
                    Text(subtitle)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }

                if let detail = headerDetail {
                    Text(detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                if player.is_hof == true {
                    Text("⭐ Hall of Fame")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.yellow)
                }

                // Divider separates the identity rows above from the
                // bio rows below. Was its own .ultraThinMaterial card
                // before this turn — folded in here so the header
                // contains all the static player info.
                Divider().padding(.vertical, 4)

                if let dob = formatLongDate(player.birthdate) {
                    HeaderBioRow(label: "Date of Birth", value: dob)
                }
                if let place = placeOfBirth {
                    HeaderBioRow(label: "Place of Birth", value: place)
                }
                if let h = formatHeight(player.height) {
                    HeaderBioRow(label: "Height", value: h)
                }
                if let w = formatWeight(player.weight) {
                    HeaderBioRow(label: "Weight", value: w)
                }
                if let debut = formatLongDate(player.debut) {
                    HeaderBioRow(label: "MLB Debut", value: debut)
                }
                if viewModel.isRetired,
                   let final = formatLongDate(player.final_game) {
                    HeaderBioRow(label: "Final Game", value: final)
                }
            }

            Spacer(minLength: 0)
        }
        .padding(16)
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 16))
        .padding(.horizontal, 16)
        .padding(.top, 8)
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

    /// Show the Batting/Pitching toggle when the player has a
    /// meaningful career on BOTH sides of the ball — career PA > 50
    /// AND career IP > 50. The previous `viewModel.isTwoWay` gate used
    /// per-season PA thresholds, which excluded NL-era pitchers like
    /// deGrom (423 career PA but never 250 in a single season) — they
    /// have a real batting career and deserve the toggle. The 50/50
    /// floor still keeps modern AL pitchers with a single interleague
    /// at-bat from tripping the toggle on.
    private var showsRoleSelector: Bool {
        viewModel.hasMeaningfulBatting && viewModel.hasMeaningfulPitching
    }

    /// The role to surface before any explicit user toggle. Priority:
    ///   1. Leaderboard `is_pitcher` hint — if the user tapped a
    ///      pitcher row, default to pitching even before fetches land
    ///      (and even when the role toggle is visible — a tap from
    ///      the pitching board should land on pitching for two-way
    ///      players too).
    ///   2. No hint → use the IP-vs-PA heuristic. Pitching wins for
    ///      pure pitchers (any IP, no PA) and NL-era starters (deGrom:
    ///      1500 IP > 423 PA); batting wins for position players and
    ///      Ohtani / Ruth (PA dominates IP).
    private var defaultRole: Role {
        if player.is_pitcher == true { return .pitching }
        if player.is_pitcher == false { return .batting }
        return viewModel.inferredPitcherRole ? .pitching : .batting
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
                // .frame(maxWidth: .infinity) on each label forces the
                // segmented picker to give every segment the same
                // width — without it, segments size to their label's
                // intrinsic width and "Career" / "Game Logs" end up
                // visibly different sizes.
                Text(tab.rawValue)
                    .frame(maxWidth: .infinity)
                    .tag(tab)
            }
        }
        .pickerStyle(.segmented)
        // Breathing room between the segmented control and the first
        // card below it. Stacks with the parent VStack(spacing: 16)
        // for a total ~24pt gap.
        .padding(.bottom, 8)
        .onAppear {
            if !availableTabs.contains(selectedTab) {
                selectedTab = availableTabs.first ?? .career
            }
        }
    }

    // MARK: - Tab routing

    /// Decides which role's content to show. With `showsRoleSelector`
    /// gated on meaningful batting AND pitching, this branch table
    /// only matters for one-sided players plus the loading window
    /// before either fetch lands.
    private var showingBatting: Bool {
        if showsRoleSelector {
            return effectiveRole == .batting
        }
        // Leaderboard hint wins for the loading-window case where
        // career data hasn't resolved yet.
        if player.is_pitcher == true { return false }
        if player.is_pitcher == false { return true }
        // Pure pitcher — pitching only.
        if viewModel.hasMeaningfulPitching && !viewModel.hasMeaningfulBatting {
            return false
        }
        // Pure batter — batting only.
        if viewModel.hasMeaningfulBatting && !viewModel.hasMeaningfulPitching {
            return true
        }
        // Neither side has loaded yet (or neither crosses the 50-PA /
        // 50-IP floor). Fall back to the same IP-vs-PA heuristic the
        // default role uses, so a search-tapped pitcher whose career
        // pitching has resolved shows pitching content right away.
        return !viewModel.inferredPitcherRole
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
        // No animation on selectedTab — the body's onChange does an
        // instant scrollTo("top") on tab change, so animating content
        // height here would interpolate against an already-resetting
        // scroll offset. Role toggles stay animated since they change
        // content within the same tab.
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

    /// Two stacked cards: current season → career. Bio data now lives
    /// in the header card at the top of the page, so we no longer
    /// render a separate Player Info card here.
    @ViewBuilder
    private var battingOverview: some View {
        VStack(spacing: 20) {
            battingCurrentSeasonCard
            battingCareerCard
        }
    }

    @ViewBuilder
    private var pitchingOverview: some View {
        VStack(spacing: 20) {
            pitchingCurrentSeasonCard
            pitchingCareerCard
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
            // 5×2 grid: row 1 rate stats, row 2 mixed (W-L, role-
            // dependent cell, IP, SO, BB/9). For starters we surface
            // GS (their primary workload signal); for relievers we
            // surface SV instead — GS is always 0 for them and SV is
            // the metric that actually matters.
            let gs = stats.standard?.GS ?? 0
            let isStarter = gs > 0
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
                    isStarter
                        ? ("GS", formatCount(stats.standard?.GS))
                        : ("SV", formatCount(stats.standard?.SV)),
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
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 20))
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

    // The previous build distinguished the current-season card with a
    // tinted glass; the tint read too heavy next to the standard cards
    // and broke the consistent feel of the profile. Both card styles
    // now use plain `.regular` glass — the current card still stands
    // out via the accent-colored title bar inside `statsGridCard`.

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
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 20))
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
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 20))
    }

    private var loadingCard: some View {
        ProgressView()
            .controlSize(.large)
            .frame(maxWidth: .infinity, minHeight: 180)
            .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 20))
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
            // The column-filter UI is rendered as a custom overlay on
            // the profile body (see `columnFilterOverlay` below) — we
            // own its chrome so the glass renders correctly. The old
            // .sheet path is intentionally gone.
            VStack(alignment: .leading, spacing: 10) {
                careerToolbar
                leaderLegend
                battingCareerTable(seasons: seasons)
            }
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
            VStack(alignment: .leading, spacing: 10) {
                careerToolbar
                leaderLegend
                pitchingCareerTable(seasons: seasons)
            }
        } else {
            noStatsCard("No pitching career stats")
        }
    }

    /// "Standard" preset label + filter button row above each career
    /// table. Tapping the slider button opens the column-filter sheet
    /// scoped to whichever role's table is showing.
    private var careerToolbar: some View {
        HStack(spacing: 8) {
            Text("Standard")
                .font(.subheadline.weight(.medium))
                .foregroundStyle(.secondary)
            Spacer()
            Button {
                showingColumnFilter = true
            } label: {
                Image(systemName: "slider.horizontal.3")
                    .font(.body)
                    .foregroundStyle(.secondary)
            }
            .accessibilityLabel("Choose columns")
        }
        .padding(.horizontal, 4)
    }

    /// Compact one-line legend explaining the gold-tinted leader cells.
    /// Two colored dots + caption labels, secondary tone — sits between
    /// the column-filter row and the career table on both batting and
    /// pitching tabs.
    private var leaderLegend: some View {
        HStack(spacing: 14) {
            HStack(spacing: 5) {
                Circle()
                    .fill(LeaderTint.league)
                    .frame(width: 7, height: 7)
                Text("League leader")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            HStack(spacing: 5) {
                Circle()
                    .fill(LeaderTint.majors)
                    .frame(width: 7, height: 7)
                Text("Majors leader")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
        }
        .padding(.horizontal, 4)
    }

    private func battingCareerTable(seasons: [CareerSeason]) -> some View {
        let sorted = seasons.sorted { ($0.year ?? 0) > ($1.year ?? 0) }
        let agg = BattingCareerAgg.compute(seasons: seasons)
        return HStack(spacing: 0) {
            // Frozen section — Year, Age, Team. Stays put while the
            // stat columns to the right scroll horizontally. Subtle
            // shadow on the right edge separates it from the scroller.
            VStack(spacing: 0) {
                BattingCareerFrozenHeader()
                Divider()
                ForEach(Array(sorted.enumerated()), id: \.offset) { index, season in
                    BattingCareerFrozenSeasonRow(
                        season: season,
                        birthYear:  player.birth_year,
                        birthMonth: player.birth_month,
                        birthDay:   player.birth_day,
                        alternate:  !index.isMultiple(of: 2)
                    )
                    if index != sorted.indices.last {
                        Divider().opacity(0.4)
                    }
                }
                Divider()
                BattingCareerFrozenTotalsRow()
            }
            .frame(width: careerFrozenPaneWidth)
            .background(.ultraThinMaterial)
            .shadow(color: .black.opacity(0.08), radius: 4, x: 2, y: 0)
            .zIndex(1)

            // Scrollable section — WAR + filtered optional columns.
            ScrollView(.horizontal, showsIndicators: false) {
                VStack(spacing: 0) {
                    BattingCareerScrollableHeader(visible: visibleBattingColumns)
                    Divider()
                    ForEach(Array(sorted.enumerated()), id: \.offset) { index, season in
                        BattingCareerScrollableSeasonRow(
                            season: season,
                            alternate: !index.isMultiple(of: 2),
                            visible: visibleBattingColumns
                        )
                        if index != sorted.indices.last {
                            Divider().opacity(0.4)
                        }
                    }
                    Divider()
                    BattingCareerScrollableTotalsRow(
                        agg: agg,
                        visible: visibleBattingColumns
                    )
                }
            }
        }
        .frame(maxWidth: .infinity)
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 16))
    }

    private func pitchingCareerTable(seasons: [PitcherCareerSeason]) -> some View {
        let sorted = seasons.sorted { ($0.year ?? 0) > ($1.year ?? 0) }
        let agg = PitchingCareerAgg.compute(seasons: seasons)
        return HStack(spacing: 0) {
            // Frozen section — Year, Age, Team. Matches the batting
            // table's split-pane structure: stays put while WAR/W/L/…
            // scroll horizontally to the right.
            VStack(spacing: 0) {
                PitchingCareerFrozenHeader()
                Divider()
                ForEach(Array(sorted.enumerated()), id: \.offset) { index, season in
                    PitchingCareerFrozenSeasonRow(
                        season: season,
                        birthYear:  player.birth_year,
                        birthMonth: player.birth_month,
                        birthDay:   player.birth_day,
                        alternate:  !index.isMultiple(of: 2)
                    )
                    if index != sorted.indices.last {
                        Divider().opacity(0.4)
                    }
                }
                Divider()
                PitchingCareerFrozenTotalsRow()
            }
            .frame(width: careerFrozenPaneWidth)
            .background(.ultraThinMaterial)
            .shadow(color: .black.opacity(0.08), radius: 4, x: 2, y: 0)
            .zIndex(1)

            // Scrollable section — WAR + filtered optional columns.
            ScrollView(.horizontal, showsIndicators: false) {
                VStack(spacing: 0) {
                    PitchingCareerScrollableHeader(visible: visiblePitchingColumns)
                    Divider()
                    ForEach(Array(sorted.enumerated()), id: \.offset) { index, season in
                        PitchingCareerScrollableSeasonRow(
                            season: season,
                            alternate: !index.isMultiple(of: 2),
                            visible: visiblePitchingColumns
                        )
                        if index != sorted.indices.last {
                            Divider().opacity(0.4)
                        }
                    }
                    Divider()
                    PitchingCareerScrollableTotalsRow(
                        agg: agg,
                        visible: visiblePitchingColumns
                    )
                }
            }
        }
        .frame(maxWidth: .infinity)
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 16))
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

// Frozen-pane total width — pinned on the VStack so the outer HStack
// doesn't hand it any extra space. Both batting and pitching career
// tables use the same Year/Age/Team layout, so this constant is shared.
//
//   leadingPad 12
// + year 32  + (2+2 cell padding)
// + age 24   + (2+2)
// + gap 4
// + team 30  + (2+2)
// = 114pt
//
private let careerFrozenPaneWidth: CGFloat = 114

// Column widths for the full Baseball Reference batting layout —
// 28 columns totaling ~902pt. Wider than the screen, so the table
// wraps in a horizontal ScrollView and the user swipes to see the
// trailing stats. Per-season counting stats stay under 1000, so the
// narrow widths (28-32pt) accommodate them comfortably; career totals
// (4,000+ hits, 1,500+ games) commafy via formatCount and use
// minimumScaleFactor in the totals row to shrink to fit.
private enum BattingCareerColumn {
    // Frozen pane (Year + Age + gap + Team) — pinned width:
    //   leadingPad 12 + year 32 + (2+2) + age 24 + (2+2) + gap 4
    //                 + team 30 + (2+2)                  = 114pt
    // Each cell adds 2pt left + 2pt right padding around its frame
    // width, hence the +4 per cell. See careerFrozenPaneWidth below.
    //
    // Year fits "Career" totals text at 9.5pt-semibold (~30pt) and
    // "2024" at 11pt monospaced. Age fits the "Age" header at
    // 11pt-semibold (~22pt). Team fits the "Team" header (~26pt) and
    // 3-letter codes like "LAD"/"CWS" at 11pt regular.
    static let year:    CGFloat = 32
    static let age:     CGFloat = 24
    /// Visual gap between the right-aligned Age cell and the
    /// left-aligned Team cell so values don't run together.
    static let ageTeamGap: CGFloat = 4
    static let team:    CGFloat = 30
    static let war:     CGFloat = 40
    static let g:       CGFloat = 32
    static let pa:      CGFloat = 40
    static let ab:      CGFloat = 40
    static let r:       CGFloat = 32
    static let h:       CGFloat = 32
    static let doubles: CGFloat = 30
    static let triples: CGFloat = 28
    static let hr:      CGFloat = 30
    static let rbi:     CGFloat = 36
    static let sb:      CGFloat = 30
    static let cs:      CGFloat = 28
    static let bb:      CGFloat = 34
    static let so:      CGFloat = 36
    static let ba:      CGFloat = 38
    static let obp:     CGFloat = 38
    static let slg:     CGFloat = 38
    static let ops:     CGFloat = 44
    static let opsPlus: CGFloat = 38
    static let tb:      CGFloat = 36
    static let gidp:    CGFloat = 34
    static let hbp:     CGFloat = 30
    static let sh:      CGFloat = 28
    static let sf:      CGFloat = 28
    static let ibb:     CGFloat = 32
}

// Lahman / nightly-update → modern display code. Three input shapes
// land in the team column depending on which loader wrote the row:
//   • Lahman team_id   ("LAN", "NYA", "SLN")             — historical
//   • Baseball-Reference Tm ("NYY", "STL")                — most current
//   • Full team name ("Los Angeles Angels")               — fallback path
// Anything not in the dict falls through to the raw value, so
// already-modern codes ("BOS", "NYY") pass through unchanged.
private let lahmanToDisplay: [String: String] = [
    // Lahman team_id codes
    "LAN": "LAD", "NYA": "NYY", "NYN": "NYM", "SLN": "STL",
    "CHN": "CHC", "CHA": "CWS", "KCA": "KC",  "SDN": "SD",
    "SFN": "SF",  "TBA": "TB",  "MIA": "MIA", "FLO": "FLA",
    "MON": "MON", "WAS": "WSH", "ANA": "LAA", "CAL": "CAL",
    "ML4": "MIL", "MIL": "MIL", "HOU": "HOU", "ATL": "ATL",
    "CIN": "CIN", "PIT": "PIT", "PHI": "PHI", "MIN": "MIN",
    "CLE": "CLE", "DET": "DET", "BAL": "BAL", "BOS": "BOS",
    "SEA": "SEA", "OAK": "OAK", "TEX": "TEX", "TOR": "TOR",
    "COL": "COL", "ARI": "ARI", "ATH": "ATH",
    // Full team names — happen for current-year rows when the nightly
    // update lands them via the bref override path with the long name
    // instead of the abbreviation.
    "Los Angeles Angels":   "LAA",
    "Los Angeles Dodgers":  "LAD",
    "New York Yankees":     "NYY",
    "New York Mets":        "NYM",
    "Chicago Cubs":         "CHC",
    "Chicago White Sox":    "CWS",
    "San Francisco Giants": "SF",
    "San Diego Padres":     "SD",
    "Tampa Bay Rays":       "TB",
    "Kansas City Royals":   "KC",
    "Washington Nationals": "WSH",
    "Arizona Diamondbacks": "ARI",
    "Colorado Rockies":     "COL",
    "Miami Marlins":        "MIA",
    "Atlanta Braves":       "ATL",
    "Houston Astros":       "HOU",
    "Seattle Mariners":     "SEA",
    "Texas Rangers":        "TEX",
    "Toronto Blue Jays":    "TOR",
    "Minnesota Twins":      "MIN",
    "Cleveland Guardians":  "CLE",
    "Detroit Tigers":       "DET",
    "Baltimore Orioles":    "BAL",
    "Boston Red Sox":       "BOS",
    "Oakland Athletics":    "OAK",
    "Athletics":            "ATH",
    "Philadelphia Phillies":"PHI",
    "Milwaukee Brewers":    "MIL",
    "Cincinnati Reds":      "CIN",
    "St. Louis Cardinals":  "STL",
    "Pittsburgh Pirates":   "PIT",
    // City-only values — emitted by the nightly bwar path when bref
    // data isn't available (`_TEAM_DISPLAY` ships just the city).
    // Single-team cities map directly. Two-team cities ("Los Angeles",
    // "New York", "Chicago") are intentionally NOT in this dict;
    // displayTeamCode handles them via the league switch instead.
    "Texas":         "TEX",
    "Houston":       "HOU",
    "Seattle":       "SEA",
    "Boston":        "BOS",
    "Detroit":       "DET",
    "Minnesota":     "MIN",
    "Cleveland":     "CLE",
    "Baltimore":     "BAL",
    "Oakland":       "OAK",
    "Toronto":       "TOR",
    "Tampa Bay":     "TB",
    "Kansas City":   "KC",
    "Atlanta":       "ATL",
    "Philadelphia":  "PHI",
    "Milwaukee":     "MIL",
    "Cincinnati":    "CIN",
    "Pittsburgh":    "PIT",
    "Colorado":      "COL",
    "Arizona":       "ARI",
    "Miami":         "MIA",
    "Washington":    "WSH",
    "San Francisco": "SF",
    "San Diego":     "SD",
    "St. Louis":     "STL",
]

/// Resolve any of the team-column shapes (Lahman code, bref code,
/// full team name, city-only value) to a 2–3 char display code.
///
/// Order of resolution:
///   1. Empty/nil → "—"
///   2. Already short (≤3 chars) → dict lookup, then raw passthrough
///      (lets unknown historical codes like "BSN" still display).
///   3. Exact match against the full-name dict → mapped code.
///   4. League-aware disambiguation for ambiguous two-team cities
///      ("Los Angeles", "New York", "Chicago") — picks LAA vs LAD
///      etc. based on the league field on the season.
///   5. Substring match — `raw.contains(key) || key.contains(raw)`.
///      Catches the ambiguous-city case when no league info is
///      available; first iteration wins (dict-order dependent).
///   6. Last resort: first 3 chars uppercased.
private func displayTeamCode(_ raw: String?, league: String? = nil) -> String {
    guard let raw = raw, !raw.isEmpty else { return "—" }

    if raw.count <= 3 {
        return lahmanToDisplay[raw] ?? raw
    }

    if let code = lahmanToDisplay[raw] { return code }

    // Two-team cities — disambiguate via the league field before
    // falling through to the (non-deterministic) substring match.
    switch (raw, league) {
    case ("Los Angeles", "AL"): return "LAA"
    case ("Los Angeles", "NL"): return "LAD"
    case ("New York",    "AL"): return "NYY"
    case ("New York",    "NL"): return "NYM"
    case ("Chicago",     "AL"): return "CWS"
    case ("Chicago",     "NL"): return "CHC"
    default: break
    }

    for (key, value) in lahmanToDisplay {
        if raw.contains(key) || key.contains(raw) {
            return value
        }
    }

    return String(raw.prefix(3)).uppercased()
}

// MARK: - Frozen section (Year, Age, Team)

private struct BattingCareerFrozenHeader: View {
    var body: some View {
        HStack(spacing: 0) {
            Text("Year").frame(width: BattingCareerColumn.year,    alignment: .leading) .padding(.horizontal, 2)
            Text("Age") .frame(width: BattingCareerColumn.age,     alignment: .trailing).padding(.horizontal, 2)
            // Explicit Color.clear gap so right-aligned Age and
            // left-aligned Team don't touch ("34LAA" → "34   LAA").
            Color.clear.frame(width: BattingCareerColumn.ageTeamGap)
            Text("Team").frame(width: BattingCareerColumn.team,    alignment: .leading) .padding(.horizontal, 2)
        }
        .font(.system(size: 11, weight: .semibold))
        .foregroundStyle(.secondary)
        .padding(.leading, 12)
        .frame(height: 28)
    }
}

private struct BattingCareerFrozenSeasonRow: View {
    let season: CareerSeason
    let birthYear:  Int?
    let birthMonth: Int?
    let birthDay:   Int?
    let alternate:  Bool

    var body: some View {
        HStack(spacing: 0) {
            Text(formatYear(season.year))
                .frame(width: BattingCareerColumn.year, alignment: .leading)
                .padding(.horizontal, 2)
            Text(formatAge(seasonYear: season.year,
                           birthYear: birthYear,
                           birthMonth: birthMonth,
                           birthDay: birthDay))
                .frame(width: BattingCareerColumn.age, alignment: .trailing)
                .monospacedDigit()
                .padding(.horizontal, 2)
            Color.clear.frame(width: BattingCareerColumn.ageTeamGap)
            // Lahman → modern code lookup with league disambiguation
            // for two-team cities ("Los Angeles" + AL → "LAA" vs +NL
            // → "LAD"). Anything outside the dict (already-modern
            // codes, ancient teams) passes through.
            Text(displayTeamCode(season.team, league: season.league))
                .lineLimit(1)
                .truncationMode(.tail)
                .frame(width: BattingCareerColumn.team, alignment: .leading)
                .padding(.horizontal, 2)
        }
        .font(.system(size: 11))
        .padding(.leading, 12)
        .frame(height: 28)
        .background(alternate ? Color(.systemGray6).opacity(0.5) : Color.clear)
    }
}

private struct BattingCareerFrozenTotalsRow: View {
    var body: some View {
        HStack(spacing: 0) {
            Text("Career").frame(width: BattingCareerColumn.year, alignment: .leading)
                .padding(.horizontal, 2)
            Color.clear.frame(width: BattingCareerColumn.age)
                .padding(.horizontal, 2)
            Color.clear.frame(width: BattingCareerColumn.ageTeamGap)
            Color.clear.frame(width: BattingCareerColumn.team)
                .padding(.horizontal, 2)
        }
        .font(.system(size: 9.5, weight: .semibold))
        .lineLimit(1)
        .minimumScaleFactor(0.6)
        .padding(.leading, 12)
        .frame(height: 28)
        .background(Color(.systemGray5).opacity(0.7))
        .overlay(alignment: .top) { Divider() }
    }
}

// MARK: - Scrollable section (WAR through IBB)

private struct BattingCareerScrollableHeader: View {
    let visible: Set<String>
    var body: some View {
        HStack(spacing: 0) {
            // Core — always shown.
            Text("WAR").frame(width: BattingCareerColumn.war, alignment: .trailing).padding(.horizontal, 2)
            Text("G")  .frame(width: BattingCareerColumn.g,   alignment: .trailing).padding(.horizontal, 2)
            Text("PA") .frame(width: BattingCareerColumn.pa,  alignment: .trailing).padding(.horizontal, 2)
            Text("AB") .frame(width: BattingCareerColumn.ab,  alignment: .trailing).padding(.horizontal, 2)
            // Optional — render only when visible.
            if visible.contains("R")    { Text("R")   .frame(width: BattingCareerColumn.r,       alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("H")    { Text("H")   .frame(width: BattingCareerColumn.h,       alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("2B")   { Text("2B")  .frame(width: BattingCareerColumn.doubles, alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("3B")   { Text("3B")  .frame(width: BattingCareerColumn.triples, alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("HR")   { Text("HR")  .frame(width: BattingCareerColumn.hr,      alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("RBI")  { Text("RBI") .frame(width: BattingCareerColumn.rbi,     alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("SB")   { Text("SB")  .frame(width: BattingCareerColumn.sb,      alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("CS")   { Text("CS")  .frame(width: BattingCareerColumn.cs,      alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("BB")   { Text("BB")  .frame(width: BattingCareerColumn.bb,      alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("SO")   { Text("SO")  .frame(width: BattingCareerColumn.so,      alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("AVG")  { Text("BA")  .frame(width: BattingCareerColumn.ba,      alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("OBP")  { Text("OBP") .frame(width: BattingCareerColumn.obp,     alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("SLG")  { Text("SLG") .frame(width: BattingCareerColumn.slg,     alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("OPS")  { Text("OPS") .frame(width: BattingCareerColumn.ops,     alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("OPS+") { Text("OPS+").frame(width: BattingCareerColumn.opsPlus, alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("TB")   { Text("TB")  .frame(width: BattingCareerColumn.tb,      alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("GIDP") { Text("GIDP").frame(width: BattingCareerColumn.gidp,    alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("HBP")  { Text("HBP") .frame(width: BattingCareerColumn.hbp,     alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("SH")   { Text("SH")  .frame(width: BattingCareerColumn.sh,      alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("SF")   { Text("SF")  .frame(width: BattingCareerColumn.sf,      alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("IBB")  { Text("IBB") .frame(width: BattingCareerColumn.ibb,     alignment: .trailing).padding(.horizontal, 2) }
        }
        .font(.system(size: 11, weight: .semibold))
        .foregroundStyle(.secondary)
        .padding(.trailing, 12)
        .frame(height: 28)
    }
}

private struct BattingCareerScrollableSeasonRow: View {
    let season: CareerSeason
    let alternate: Bool
    let visible: Set<String>

    var body: some View {
        let l = season.leaders
        HStack(spacing: 0) {
            // Core
            leaderCell(formatWAR(season.WAR),    label: "WAR", leaders: l, width: BattingCareerColumn.war)
            Text(formatCount(season.G)) .frame(width: BattingCareerColumn.g,  alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatCount(season.PA)).frame(width: BattingCareerColumn.pa, alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatCount(season.AB)).frame(width: BattingCareerColumn.ab, alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            // Optional. Tracked-by-bbref leadership stats use leaderCell;
            // CS / OPS+ / TB / GIDP / HBP / SH / SF / IBB aren't on the
            // leaderboard catalog and stay as plain Text.
            if visible.contains("R")    { leaderCell(formatCount(season.R),       label: "R",   leaders: l, width: BattingCareerColumn.r) }
            if visible.contains("H")    { leaderCell(formatCount(season.H),       label: "H",   leaders: l, width: BattingCareerColumn.h) }
            if visible.contains("2B")   { leaderCell(formatCount(season.doubles), label: "2B",  leaders: l, width: BattingCareerColumn.doubles) }
            if visible.contains("3B")   { leaderCell(formatCount(season.triples), label: "3B",  leaders: l, width: BattingCareerColumn.triples) }
            if visible.contains("HR")   { leaderCell(formatCount(season.HR),      label: "HR",  leaders: l, width: BattingCareerColumn.hr) }
            if visible.contains("RBI")  { leaderCell(formatCount(season.RBI),     label: "RBI", leaders: l, width: BattingCareerColumn.rbi) }
            if visible.contains("SB")   { leaderCell(formatCount(season.SB),      label: "SB",  leaders: l, width: BattingCareerColumn.sb) }
            if visible.contains("CS")   { Text(formatCount(season.CS))     .frame(width: BattingCareerColumn.cs,      alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("BB")   { leaderCell(formatCount(season.BB),      label: "BB",  leaders: l, width: BattingCareerColumn.bb) }
            if visible.contains("SO")   { leaderCell(formatCount(season.SO),      label: "SO",  leaders: l, width: BattingCareerColumn.so) }
            if visible.contains("AVG")  { leaderCell(format3(season.BA),          label: "AVG", leaders: l, width: BattingCareerColumn.ba) }
            if visible.contains("OBP")  { leaderCell(format3(season.OBP),         label: "OBP", leaders: l, width: BattingCareerColumn.obp) }
            if visible.contains("SLG")  { leaderCell(format3(season.SLG),         label: "SLG", leaders: l, width: BattingCareerColumn.slg) }
            if visible.contains("OPS")  { leaderCell(format3(season.OPS),         label: "OPS", leaders: l, width: BattingCareerColumn.ops) }
            if visible.contains("OPS+") {
                Text(formatRoundedInt(season.OPS_plus))
                    .frame(width: BattingCareerColumn.opsPlus, alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            }
            if visible.contains("TB")   { Text(formatCount(seasonTB(season))).frame(width: BattingCareerColumn.tb,    alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("GIDP") { Text(formatCount(season.GIDP))   .frame(width: BattingCareerColumn.gidp,    alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("HBP")  { Text(formatCount(season.HBP))    .frame(width: BattingCareerColumn.hbp,     alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("SH")   { Text(formatCount(season.SH))     .frame(width: BattingCareerColumn.sh,      alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("SF")   { Text(formatCount(season.SF))     .frame(width: BattingCareerColumn.sf,      alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("IBB")  { Text(formatCount(season.IBB))    .frame(width: BattingCareerColumn.ibb,     alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
        }
        .font(.system(size: 11))
        .padding(.trailing, 12)
        .frame(height: 28)
        .background(alternate ? Color(.systemGray6).opacity(0.5) : Color.clear)
    }
}

private struct BattingCareerScrollableTotalsRow: View {
    let agg: BattingCareerAgg
    let visible: Set<String>

    var body: some View {
        HStack(spacing: 0) {
            // Core
            Text(formatWAR(agg.war))
                .frame(width: BattingCareerColumn.war, alignment: .trailing)
                .monospacedDigit()
                .padding(.horizontal, 2)
            Text(formatCount(agg.g)) .frame(width: BattingCareerColumn.g,  alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatCount(agg.pa)).frame(width: BattingCareerColumn.pa, alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            Text(formatCount(agg.ab)).frame(width: BattingCareerColumn.ab, alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            // Optional
            if visible.contains("R")    { Text(formatCount(agg.r))   .frame(width: BattingCareerColumn.r,       alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("H")    { Text(formatCount(agg.h))   .frame(width: BattingCareerColumn.h,       alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("2B")   { Text(formatCount(agg.dbl)) .frame(width: BattingCareerColumn.doubles, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("3B")   { Text(formatCount(agg.trp)) .frame(width: BattingCareerColumn.triples, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("HR")   { Text(formatCount(agg.hr))  .frame(width: BattingCareerColumn.hr,      alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("RBI")  { Text(formatCount(agg.rbi)) .frame(width: BattingCareerColumn.rbi,     alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("SB")   { Text(formatCount(agg.sb))  .frame(width: BattingCareerColumn.sb,      alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("CS")   { Text(formatCount(agg.cs))  .frame(width: BattingCareerColumn.cs,      alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("BB")   { Text(formatCount(agg.bb))  .frame(width: BattingCareerColumn.bb,      alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("SO")   { Text(formatCount(agg.so))  .frame(width: BattingCareerColumn.so,      alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("AVG")  { Text(format3(agg.avg))     .frame(width: BattingCareerColumn.ba,      alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("OBP")  { Text(format3(agg.obp))     .frame(width: BattingCareerColumn.obp,     alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("SLG")  { Text(format3(agg.slg))     .frame(width: BattingCareerColumn.slg,     alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("OPS")  { Text(format3(agg.ops))     .frame(width: BattingCareerColumn.ops,     alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("OPS+") {
                Text("—").frame(width: BattingCareerColumn.opsPlus, alignment: .trailing)
                    .foregroundStyle(.tertiary)
                    .padding(.horizontal, 2)
            }
            if visible.contains("TB")   { Text(formatCount(agg.tb))  .frame(width: BattingCareerColumn.tb,      alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("GIDP") { Text(formatCount(agg.gidp)).frame(width: BattingCareerColumn.gidp,    alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("HBP")  { Text(formatCount(agg.hbp)) .frame(width: BattingCareerColumn.hbp,     alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("SH")   { Text(formatCount(agg.sh))  .frame(width: BattingCareerColumn.sh,      alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("SF")   { Text(formatCount(agg.sf))  .frame(width: BattingCareerColumn.sf,      alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("IBB")  { Text(formatCount(agg.ibb)) .frame(width: BattingCareerColumn.ibb,     alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
        }
        .font(.system(size: 9.5, weight: .semibold))
        .lineLimit(1)
        .minimumScaleFactor(0.6)
        .padding(.trailing, 12)
        .frame(height: 28)
        .background(Color(.systemGray5).opacity(0.7))
        .overlay(alignment: .top) { Divider() }
    }
}

// MARK: - Pitching career table

// Pitching column layout — W-L combined into one cell, no AVG, ERA shown
// to two decimals.
// 34-column Baseball Reference pitching layout. Total intrinsic width
// ~1,096pt; wraps in a horizontal ScrollView.
private enum PitchingCareerColumn {
    // Same frozen-pane layout as the batting career table — see
    // BattingCareerColumn for the width derivation.
    static let year:       CGFloat = 32
    static let age:        CGFloat = 24
    /// Visual gap between right-aligned Age and left-aligned Team.
    static let ageTeamGap: CGFloat = 4
    static let team:       CGFloat = 30
    static let war:        CGFloat = 38
    static let w:          CGFloat = 26
    static let l:          CGFloat = 26
    static let wlPct:      CGFloat = 36
    static let era:        CGFloat = 38
    static let g:          CGFloat = 30
    static let gs:         CGFloat = 30
    static let gf:         CGFloat = 30
    static let cg:         CGFloat = 28
    static let sho:        CGFloat = 30
    static let sv:         CGFloat = 28
    static let ip:         CGFloat = 40
    static let h:          CGFloat = 30
    static let r:          CGFloat = 28
    static let er:         CGFloat = 28
    static let hr:         CGFloat = 28
    static let bb:         CGFloat = 30
    static let ibb:        CGFloat = 30
    static let so:         CGFloat = 32
    static let hbp:        CGFloat = 30
    static let bk:         CGFloat = 26
    static let wp:         CGFloat = 28
    static let bf:         CGFloat = 34
    static let eraPlus:    CGFloat = 36
    static let fip:        CGFloat = 36
    static let whip:       CGFloat = 38
    static let hPer9:      CGFloat = 34
    static let hrPer9:     CGFloat = 34
    static let bbPer9:     CGFloat = 34
    static let soPer9:     CGFloat = 34
    static let soBB:       CGFloat = 36
}

// MARK: - Frozen section (Year, Age, Team)

private struct PitchingCareerFrozenHeader: View {
    var body: some View {
        HStack(spacing: 0) {
            Text("Year").frame(width: PitchingCareerColumn.year,    alignment: .leading) .padding(.horizontal, 2)
            Text("Age") .frame(width: PitchingCareerColumn.age,     alignment: .trailing).padding(.horizontal, 2)
            Color.clear.frame(width: PitchingCareerColumn.ageTeamGap)
            Text("Team").frame(width: PitchingCareerColumn.team,    alignment: .leading) .padding(.horizontal, 2)
        }
        .font(.system(size: 11, weight: .semibold))
        .foregroundStyle(.secondary)
        .padding(.leading, 12)
        .frame(height: 28)
    }
}

private struct PitchingCareerFrozenSeasonRow: View {
    let season: PitcherCareerSeason
    let birthYear:  Int?
    let birthMonth: Int?
    let birthDay:   Int?
    let alternate:  Bool

    var body: some View {
        HStack(spacing: 0) {
            Text(formatYear(season.year))
                .frame(width: PitchingCareerColumn.year, alignment: .leading)
                .padding(.horizontal, 2)
            Text(formatAge(seasonYear: season.year,
                           birthYear: birthYear,
                           birthMonth: birthMonth,
                           birthDay: birthDay))
                .frame(width: PitchingCareerColumn.age, alignment: .trailing)
                .monospacedDigit()
                .padding(.horizontal, 2)
            Color.clear.frame(width: PitchingCareerColumn.ageTeamGap)
            Text(displayTeamCode(season.team, league: season.league))
                .lineLimit(1)
                .truncationMode(.tail)
                .frame(width: PitchingCareerColumn.team, alignment: .leading)
                .padding(.horizontal, 2)
        }
        .font(.system(size: 11))
        .padding(.leading, 12)
        .frame(height: 28)
        .background(alternate ? Color(.systemGray6).opacity(0.5) : Color.clear)
    }
}

private struct PitchingCareerFrozenTotalsRow: View {
    var body: some View {
        HStack(spacing: 0) {
            Text("Career").frame(width: PitchingCareerColumn.year, alignment: .leading)
                .padding(.horizontal, 2)
            Color.clear.frame(width: PitchingCareerColumn.age)
                .padding(.horizontal, 2)
            Color.clear.frame(width: PitchingCareerColumn.ageTeamGap)
            Color.clear.frame(width: PitchingCareerColumn.team)
                .padding(.horizontal, 2)
        }
        .font(.system(size: 9.5, weight: .semibold))
        .lineLimit(1)
        .minimumScaleFactor(0.6)
        .padding(.leading, 12)
        .frame(height: 28)
        .background(Color(.systemGray5).opacity(0.7))
        .overlay(alignment: .top) { Divider() }
    }
}

// MARK: - Scrollable section (WAR through SO/BB)

private struct PitchingCareerScrollableHeader: View {
    let visible: Set<String>
    var body: some View {
        HStack(spacing: 0) {
            // Core
            Text("WAR").frame(width: PitchingCareerColumn.war, alignment: .trailing).padding(.horizontal, 2)
            // Optional
            if visible.contains("W")     { Text("W")     .frame(width: PitchingCareerColumn.w,       alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("L")     { Text("L")     .frame(width: PitchingCareerColumn.l,       alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("W-L%")  { Text("W-L%")  .frame(width: PitchingCareerColumn.wlPct,   alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("ERA")   { Text("ERA")   .frame(width: PitchingCareerColumn.era,     alignment: .trailing).padding(.horizontal, 2) }
            // Core
            Text("G").frame(width: PitchingCareerColumn.g, alignment: .trailing).padding(.horizontal, 2)
            // Optional
            if visible.contains("GS")    { Text("GS")    .frame(width: PitchingCareerColumn.gs,      alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("GF")    { Text("GF")    .frame(width: PitchingCareerColumn.gf,      alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("CG")    { Text("CG")    .frame(width: PitchingCareerColumn.cg,      alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("SHO")   { Text("SHO")   .frame(width: PitchingCareerColumn.sho,     alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("SV")    { Text("SV")    .frame(width: PitchingCareerColumn.sv,      alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("IP")    { Text("IP")    .frame(width: PitchingCareerColumn.ip,      alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("H")     { Text("H")     .frame(width: PitchingCareerColumn.h,       alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("R")     { Text("R")     .frame(width: PitchingCareerColumn.r,       alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("ER")    { Text("ER")    .frame(width: PitchingCareerColumn.er,      alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("HR")    { Text("HR")    .frame(width: PitchingCareerColumn.hr,      alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("BB")    { Text("BB")    .frame(width: PitchingCareerColumn.bb,      alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("IBB")   { Text("IBB")   .frame(width: PitchingCareerColumn.ibb,     alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("SO")    { Text("SO")    .frame(width: PitchingCareerColumn.so,      alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("HBP")   { Text("HBP")   .frame(width: PitchingCareerColumn.hbp,     alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("BK")    { Text("BK")    .frame(width: PitchingCareerColumn.bk,      alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("WP")    { Text("WP")    .frame(width: PitchingCareerColumn.wp,      alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("BF")    { Text("BF")    .frame(width: PitchingCareerColumn.bf,      alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("ERA+")  { Text("ERA+")  .frame(width: PitchingCareerColumn.eraPlus, alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("FIP")   { Text("FIP")   .frame(width: PitchingCareerColumn.fip,     alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("WHIP")  { Text("WHIP")  .frame(width: PitchingCareerColumn.whip,    alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("H/9")   { Text("H/9")   .frame(width: PitchingCareerColumn.hPer9,   alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("HR/9")  { Text("HR/9")  .frame(width: PitchingCareerColumn.hrPer9,  alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("BB/9")  { Text("BB/9")  .frame(width: PitchingCareerColumn.bbPer9,  alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("SO/9")  { Text("SO/9")  .frame(width: PitchingCareerColumn.soPer9,  alignment: .trailing).padding(.horizontal, 2) }
            if visible.contains("SO/BB") { Text("SO/BB") .frame(width: PitchingCareerColumn.soBB,    alignment: .trailing).padding(.horizontal, 2) }
        }
        .font(.system(size: 11, weight: .semibold))
        .foregroundStyle(.secondary)
        .padding(.trailing, 12)
        .frame(height: 28)
    }
}

private struct PitchingCareerScrollableSeasonRow: View {
    let season: PitcherCareerSeason
    let alternate: Bool
    let visible: Set<String>

    var body: some View {
        let l = season.leaders
        HStack(spacing: 0) {
            // Core
            leaderCell(formatWAR(season.WAR), label: "WAR", leaders: l, width: PitchingCareerColumn.war)
            // Optional. Pitching leadership stats per bbref: ERA, SO, W,
            // WHIP, SV, IP, WAR. Everything else stays plain Text.
            if visible.contains("W")     { leaderCell(formatCount(season.W), label: "W", leaders: l, width: PitchingCareerColumn.w) }
            if visible.contains("L")     { Text(formatCount(season.L)).frame(width: PitchingCareerColumn.l, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("W-L%")  {
                Text(formatWinPct(w: season.W, l: season.L))
                    .frame(width: PitchingCareerColumn.wlPct, alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            }
            if visible.contains("ERA")   { leaderCell(format2(season.ERA), label: "ERA", leaders: l, width: PitchingCareerColumn.era) }
            // Core
            Text(formatCount(season.G)).frame(width: PitchingCareerColumn.g, alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            // Optional
            if visible.contains("GS")    { Text(formatCount(season.GS)).frame(width: PitchingCareerColumn.gs, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            // For modern pitchers GF/CG/SHO/SV are realistically 0 when
            // the field comes back null (nightly fetched a bref row
            // without the column), so show "0" rather than "—".
            if visible.contains("GF")    { Text(formatCountOrZero(season.GF)).frame(width: PitchingCareerColumn.gf,  alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("CG")    { Text(formatCountOrZero(season.CG)).frame(width: PitchingCareerColumn.cg,  alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("SHO")   { Text(formatCountOrZero(season.SHO)).frame(width: PitchingCareerColumn.sho, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("SV")    { leaderCell(formatCountOrZero(season.SV), label: "SV", leaders: l, width: PitchingCareerColumn.sv) }
            if visible.contains("IP")    { leaderCell(formatIP(season.IP),          label: "IP", leaders: l, width: PitchingCareerColumn.ip) }
            if visible.contains("H")     { Text(formatCount(season.H)).frame(width: PitchingCareerColumn.h, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("R")     { Text(formatCount(season.R)).frame(width: PitchingCareerColumn.r, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("ER")    { Text(formatCount(season.ER)).frame(width: PitchingCareerColumn.er, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("HR")    { Text(formatCount(season.HR)).frame(width: PitchingCareerColumn.hr, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("BB")    { Text(formatCount(season.BB)).frame(width: PitchingCareerColumn.bb, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("IBB")   { Text(formatCount(season.IBB)).frame(width: PitchingCareerColumn.ibb, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("SO")    { leaderCell(formatCount(season.SO), label: "SO", leaders: l, width: PitchingCareerColumn.so) }
            if visible.contains("HBP")   { Text(formatCountOrZero(season.HBP)).frame(width: PitchingCareerColumn.hbp, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("BK")    { Text(formatCountOrZero(season.BK)).frame(width: PitchingCareerColumn.bk, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("WP")    { Text(formatCountOrZero(season.WP)).frame(width: PitchingCareerColumn.wp, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("BF")    { Text(formatCount(season.BFP)).frame(width: PitchingCareerColumn.bf, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("ERA+")  {
                Text(formatRoundedInt(season.ERA_plus))
                    .frame(width: PitchingCareerColumn.eraPlus, alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            }
            if visible.contains("FIP")   { Text(format2(season.FIP)).frame(width: PitchingCareerColumn.fip, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("WHIP")  { leaderCell(format2(season.WHIP), label: "WHIP", leaders: l, width: PitchingCareerColumn.whip) }
            // H/9 isn't stored — derive on-device. The HR/9, BB/9, SO/9
            // values are stored on the season and used directly.
            if visible.contains("H/9")   {
                Text(format2(perNine(season.H, ip: season.IP)))
                    .frame(width: PitchingCareerColumn.hPer9, alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            }
            if visible.contains("HR/9")  { Text(format2(season.HR_per9)).frame(width: PitchingCareerColumn.hrPer9, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("BB/9")  { Text(format2(season.BB_per9)).frame(width: PitchingCareerColumn.bbPer9, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("SO/9")  { Text(format2(season.K_per9)).frame(width: PitchingCareerColumn.soPer9, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("SO/BB") {
                Text(format2(soBBRatio(so: season.SO, bb: season.BB)))
                    .frame(width: PitchingCareerColumn.soBB, alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            }
        }
        .font(.system(size: 11))
        .padding(.trailing, 12)
        .frame(height: 28)
        .background(alternate ? Color(.systemGray6).opacity(0.5) : Color.clear)
    }
}

private struct PitchingCareerScrollableTotalsRow: View {
    let agg: PitchingCareerAgg
    let visible: Set<String>
    var body: some View {
        HStack(spacing: 0) {
            // Core
            Text(formatWAR(agg.war))
                .frame(width: PitchingCareerColumn.war, alignment: .trailing)
                .monospacedDigit().padding(.horizontal, 2)
            // Optional
            if visible.contains("W")     { Text(formatCount(agg.w)).frame(width: PitchingCareerColumn.w, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("L")     { Text(formatCount(agg.l)).frame(width: PitchingCareerColumn.l, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("W-L%")  {
                Text(formatWinPctValue(agg.winPct))
                    .frame(width: PitchingCareerColumn.wlPct, alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            }
            if visible.contains("ERA")   { Text(format2(agg.era)).frame(width: PitchingCareerColumn.era, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            // Core
            Text(formatCount(agg.g)).frame(width: PitchingCareerColumn.g, alignment: .trailing).monospacedDigit().padding(.horizontal, 2)
            // Optional
            if visible.contains("GS")    { Text(formatCount(agg.gs)).frame(width: PitchingCareerColumn.gs, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("GF")    { Text(formatCount(agg.gf)).frame(width: PitchingCareerColumn.gf, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("CG")    { Text(formatCount(agg.cg)).frame(width: PitchingCareerColumn.cg, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("SHO")   { Text(formatCount(agg.sho)).frame(width: PitchingCareerColumn.sho, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("SV")    { Text(formatCount(agg.sv)).frame(width: PitchingCareerColumn.sv, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("IP")    { Text(formatIP(agg.ip)).frame(width: PitchingCareerColumn.ip, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("H")     { Text(formatCount(agg.h)).frame(width: PitchingCareerColumn.h, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("R")     { Text(formatCount(agg.r)).frame(width: PitchingCareerColumn.r, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("ER")    { Text(formatCount(agg.er)).frame(width: PitchingCareerColumn.er, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("HR")    { Text(formatCount(agg.hr)).frame(width: PitchingCareerColumn.hr, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("BB")    { Text(formatCount(agg.bb)).frame(width: PitchingCareerColumn.bb, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("IBB")   { Text(formatCount(agg.ibb)).frame(width: PitchingCareerColumn.ibb, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("SO")    { Text(formatCount(agg.so)).frame(width: PitchingCareerColumn.so, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("HBP")   { Text(formatCount(agg.hbp)).frame(width: PitchingCareerColumn.hbp, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("BK")    { Text(formatCount(agg.bk)).frame(width: PitchingCareerColumn.bk, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("WP")    { Text(formatCount(agg.wp)).frame(width: PitchingCareerColumn.wp, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("BF")    { Text(formatCount(agg.bf)).frame(width: PitchingCareerColumn.bf, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            // ERA+ and FIP aren't summable — leave blank.
            if visible.contains("ERA+")  {
                Text("—").frame(width: PitchingCareerColumn.eraPlus, alignment: .trailing)
                    .foregroundStyle(.tertiary).padding(.horizontal, 2)
            }
            if visible.contains("FIP")   {
                Text("—").frame(width: PitchingCareerColumn.fip, alignment: .trailing)
                    .foregroundStyle(.tertiary).padding(.horizontal, 2)
            }
            if visible.contains("WHIP")  { Text(format2(agg.whip)).frame(width: PitchingCareerColumn.whip, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("H/9")   { Text(format2(agg.hPer9)).frame(width: PitchingCareerColumn.hPer9, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("HR/9")  { Text(format2(agg.hrPer9)).frame(width: PitchingCareerColumn.hrPer9, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("BB/9")  { Text(format2(agg.careerBB9)).frame(width: PitchingCareerColumn.bbPer9, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("SO/9")  { Text(format2(agg.kPer9)).frame(width: PitchingCareerColumn.soPer9, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
            if visible.contains("SO/BB") { Text(format2(agg.soBB)).frame(width: PitchingCareerColumn.soBB, alignment: .trailing).monospacedDigit().padding(.horizontal, 2) }
        }
        .font(.system(size: 9.5, weight: .semibold))
        .lineLimit(1)
        .minimumScaleFactor(0.6)
        .padding(.trailing, 12)
        .frame(height: 28)
        .background(Color(.systemGray5).opacity(0.7))
        .overlay(alignment: .top) { Divider() }
    }
}

// MARK: - League-leader cells

/// Tint colors used to mark league/majors leaders. Muted gold for a
/// single-league lead, brighter gold for a majors lead — readable in a
/// monospaced numeric table without being garish, and the same shade
/// the legend dot uses so the colors map 1:1.
private enum LeaderTint {
    static let league = Color(red: 0.8, green: 0.6, blue: 0.1)
    static let majors = Color(red: 0.9, green: 0.7, blue: 0.0)
}

/// Builds the standard "career-row stat cell" — `.frame(width:, alignment:)
/// .monospacedDigit().padding(.horizontal, 2)` — and applies the leadership
/// styling: `.bold()` + muted gold if the player led their league in that
/// stat that season, `.bold().italic()` + brighter gold if they led the
/// majors. Pass-through to a plain primary-color Text when the leaders
/// dict has no entry for `label` (the common case for any given cell).
@ViewBuilder
private func leaderCell(
    _ value: String,
    label: String,
    leaders: [String: String]?,
    width: CGFloat
) -> some View {
    let kind = leaders?[label]
    let tint: Color = kind == "majors" ? LeaderTint.majors :
                      kind == "league" ? LeaderTint.league : .primary
    Text(value)
        .bold(kind != nil)
        .italic(kind == "majors")
        .foregroundStyle(tint)
        .frame(width: width, alignment: .trailing)
        .monospacedDigit()
        .padding(.horizontal, 2)
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

/// Doubles displayed as a rounded integer — used for OPS+ and similar
/// integer-conventioned ratings (100 = league average).
private func formatRoundedInt(_ value: Double?) -> String {
    guard let value else { return "—" }
    return String(Int(value.rounded()))
}

/// Age for a given season, using Baseball Reference convention: a
/// player's age is their age on June 30 of that season. If month/day
/// aren't known we fall back to a simple year subtraction.
private func formatAge(seasonYear: Int?, birthYear: Int?, birthMonth: Int?, birthDay: Int?) -> String {
    guard let y = seasonYear, let by = birthYear, by > 0 else { return "—" }
    var age = y - by
    if let m = birthMonth, let d = birthDay {
        // If born on/after July 1, the player hasn't turned `y - by`
        // by June 30, so subtract one.
        if m > 6 || (m == 6 && d > 30) {
            age -= 1
        }
    }
    return String(age)
}

/// Per-season total bases — the backend doesn't ship TB directly so
/// we derive it the same way Baseball Reference does:
///   TB = 1·1B + 2·2B + 3·3B + 4·HR, simplified to h + 2B + 2·3B + 3·HR
/// since 1B = h − 2B − 3B − HR. Returns nil when H is missing (older
/// Lahman seasons sometimes lack any batting counting stats).
private func seasonTB(_ s: CareerSeason) -> Int? {
    guard let h = s.H else { return nil }
    let dbl = s.doubles ?? 0
    let trp = s.triples ?? 0
    let hr  = s.HR ?? 0
    return h + dbl + 2 * trp + 3 * hr
}

/// Per-nine rate (e.g. H/9, HR/9) — generic helper for stats not
/// stored on the season row directly.
private func perNine(_ stat: Int?, ip: Double?) -> Double? {
    guard let stat = stat, let ip = ip, ip > 0 else { return nil }
    return Double(stat) * 9.0 / ip
}

/// Strikeout-to-walk ratio. nil when BB is 0 (would otherwise divide
/// by zero or render as a misleading "infinite" rate).
private func soBBRatio(so: Int?, bb: Int?) -> Double? {
    guard let so = so, let bb = bb, bb > 0 else { return nil }
    return Double(so) / Double(bb)
}

/// W-L percentage formatted as ".620" (3 decimals, leading zero
/// stripped). "—" when both wins and losses are missing or zero
/// (no decisions recorded).
private func formatWinPct(w: Int?, l: Int?) -> String {
    guard let w = w, let l = l, (w + l) > 0 else { return "—" }
    return formatWinPctValue(Double(w) / Double(w + l))
}

/// Same as formatWinPct but takes an already-computed Double — used by
/// the career totals row where the agg derives the value once and
/// passes it in.
private func formatWinPctValue(_ value: Double?) -> String {
    guard let value else { return "—" }
    let s = String(format: "%.3f", value)
    if s.hasPrefix("0.")  { return String(s.dropFirst()) }
    if s.hasPrefix("-0.") { return "-" + String(s.dropFirst(2)) }
    return s
}

/// Counting-stat display with thousands separators ("1,682"). Used for
/// career totals like career hits, career HRs, etc. — anything that can
/// realistically reach four digits over a long career. Cached
/// NumberFormatter so we don't allocate one per cell render.
private func formatCount(_ value: Int?) -> String {
    guard let value else { return "—" }
    return countFormatter.string(from: NSNumber(value: value)) ?? String(value)
}

/// Like `formatCount` but returns "0" for nil instead of "—". Right
/// for pitcher counting stats like CG/SHO/SV/GF/BK/WP/HBP where the
/// backend often stores null rather than zero (pybaseball bref
/// dataframe missing the column), but the player almost certainly has
/// 0 of that stat — not "unknown."
private func formatCountOrZero(_ value: Int?) -> String {
    guard let value else { return "0" }
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

// MARK: - Column filter metadata

/// One toggleable column in the career-table filter sheet.
struct ColumnFilterEntry: Identifiable {
    let key: String
    let label: String
    let description: String
    var id: String { key }
}

struct ColumnFilterGroup: Identifiable {
    let title: String
    let columns: [ColumnFilterEntry]
    var id: String { title }
}

let battingFilterGroups: [ColumnFilterGroup] = [
    ColumnFilterGroup(title: "Rate Stats", columns: [
        ColumnFilterEntry(key: "AVG",  label: "AVG",  description: "Batting average (H ÷ AB)"),
        ColumnFilterEntry(key: "OBP",  label: "OBP",  description: "On-base percentage"),
        ColumnFilterEntry(key: "SLG",  label: "SLG",  description: "Slugging percentage"),
        ColumnFilterEntry(key: "OPS",  label: "OPS",  description: "On-base + slugging"),
        ColumnFilterEntry(key: "OPS+", label: "OPS+", description: "OPS adjusted to league/park (100 = avg)"),
    ]),
    ColumnFilterGroup(title: "Counting — Offense", columns: [
        ColumnFilterEntry(key: "R",   label: "R",   description: "Runs scored"),
        ColumnFilterEntry(key: "H",   label: "H",   description: "Hits"),
        ColumnFilterEntry(key: "2B",  label: "2B",  description: "Doubles"),
        ColumnFilterEntry(key: "3B",  label: "3B",  description: "Triples"),
        ColumnFilterEntry(key: "HR",  label: "HR",  description: "Home runs"),
        ColumnFilterEntry(key: "RBI", label: "RBI", description: "Runs batted in"),
        ColumnFilterEntry(key: "SB",  label: "SB",  description: "Stolen bases"),
        ColumnFilterEntry(key: "CS",  label: "CS",  description: "Caught stealing"),
        ColumnFilterEntry(key: "BB",  label: "BB",  description: "Walks"),
        ColumnFilterEntry(key: "SO",  label: "SO",  description: "Strikeouts"),
    ]),
    ColumnFilterGroup(title: "Advanced", columns: [
        ColumnFilterEntry(key: "TB",   label: "TB",   description: "Total bases"),
        ColumnFilterEntry(key: "GIDP", label: "GIDP", description: "Grounded into double play"),
        ColumnFilterEntry(key: "HBP",  label: "HBP",  description: "Hit by pitch"),
        ColumnFilterEntry(key: "SH",   label: "SH",   description: "Sacrifice hits"),
        ColumnFilterEntry(key: "SF",   label: "SF",   description: "Sacrifice flies"),
        ColumnFilterEntry(key: "IBB",  label: "IBB",  description: "Intentional walks"),
    ]),
]

let pitchingFilterGroups: [ColumnFilterGroup] = [
    ColumnFilterGroup(title: "Rate Stats", columns: [
        ColumnFilterEntry(key: "ERA",   label: "ERA",   description: "Earned-run average"),
        ColumnFilterEntry(key: "WHIP",  label: "WHIP",  description: "Walks + hits per inning"),
        ColumnFilterEntry(key: "FIP",   label: "FIP",   description: "Fielding-independent pitching"),
        ColumnFilterEntry(key: "ERA+",  label: "ERA+",  description: "ERA adjusted to league/park (100 = avg)"),
        ColumnFilterEntry(key: "H/9",   label: "H/9",   description: "Hits per nine innings"),
        ColumnFilterEntry(key: "HR/9",  label: "HR/9",  description: "Home runs per nine innings"),
        ColumnFilterEntry(key: "BB/9",  label: "BB/9",  description: "Walks per nine innings"),
        ColumnFilterEntry(key: "SO/9",  label: "SO/9",  description: "Strikeouts per nine innings"),
        ColumnFilterEntry(key: "SO/BB", label: "SO/BB", description: "Strikeout-to-walk ratio"),
    ]),
    ColumnFilterGroup(title: "Counting", columns: [
        ColumnFilterEntry(key: "W",    label: "W",    description: "Wins"),
        ColumnFilterEntry(key: "L",    label: "L",    description: "Losses"),
        ColumnFilterEntry(key: "W-L%", label: "W-L%", description: "Winning percentage"),
        ColumnFilterEntry(key: "GS",   label: "GS",   description: "Games started"),
        ColumnFilterEntry(key: "GF",   label: "GF",   description: "Games finished"),
        ColumnFilterEntry(key: "CG",   label: "CG",   description: "Complete games"),
        ColumnFilterEntry(key: "SHO",  label: "SHO",  description: "Shutouts"),
        ColumnFilterEntry(key: "SV",   label: "SV",   description: "Saves"),
        ColumnFilterEntry(key: "IP",   label: "IP",   description: "Innings pitched"),
        ColumnFilterEntry(key: "H",    label: "H",    description: "Hits allowed"),
        ColumnFilterEntry(key: "R",    label: "R",    description: "Runs allowed"),
        ColumnFilterEntry(key: "ER",   label: "ER",   description: "Earned runs"),
        ColumnFilterEntry(key: "HR",   label: "HR",   description: "Home runs allowed"),
        ColumnFilterEntry(key: "BB",   label: "BB",   description: "Walks allowed"),
        ColumnFilterEntry(key: "IBB",  label: "IBB",  description: "Intentional walks"),
        ColumnFilterEntry(key: "SO",   label: "SO",   description: "Strikeouts"),
        ColumnFilterEntry(key: "HBP",  label: "HBP",  description: "Hit batters"),
        ColumnFilterEntry(key: "BK",   label: "BK",   description: "Balks"),
        ColumnFilterEntry(key: "WP",   label: "WP",   description: "Wild pitches"),
        ColumnFilterEntry(key: "BF",   label: "BF",   description: "Batters faced"),
    ]),
]

// MARK: - Column filter sheet

/// Modal sheet with grouped toggles for the optional career-table
/// columns. Core columns (WAR/G/PA/AB for batting, WAR/G for pitching)
/// aren't shown here — they're always visible and aren't toggleable.
/// Bottom-aligned column filter panel — replaces the previous
/// `.sheet(isPresented:)` + `ColumnFilterSheet` combination. Under
/// iOS 26 the system sheet card forces its own opaque chrome that
/// `.presentationBackground(.ultraThinMaterial)` couldn't reliably
/// override. Rendering this ourselves inside a ZStack lets the
/// .ultraThinMaterial actually be glass.
///
/// The panel: drag indicator → Reset/title/Done bar → divider →
/// scrolling list of grouped toggle rows. Top corners rounded to
/// 24pt; bottom extends past the safe area so the material reads
/// flush with the screen edge. Tap-to-dismiss is handled by the
/// dim layer in `PlayerProfileView.body`; this view only exposes
/// the Done callback.
struct ColumnFilterPanel: View {
    let title: String
    let groups: [ColumnFilterGroup]
    @Binding var visible: Set<String>
    let defaults: Set<String>
    let onDismiss: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            // Drag indicator — visual affordance only (the dim-tap and
            // Done button do the actual dismiss; we don't wire a drag
            // gesture to the panel itself).
            Capsule()
                .fill(Color(.systemFill))
                .frame(width: 40, height: 5)
                .padding(.top, 10)
                .padding(.bottom, 8)

            // Header bar — Reset on the left, title centered, Done
            // on the right. Same chrome as the previous sheet's
            // toolbar.
            HStack {
                Button("Reset") { visible = defaults }
                    .font(.subheadline)
                Spacer()
                Text(title)
                    .font(.headline)
                Spacer()
                Button("Done") { onDismiss() }
                    .font(.subheadline.weight(.semibold))
            }
            .padding(.horizontal, 16)
            .padding(.bottom, 10)

            // Custom divider — SwiftUI's Divider() renders as a near-
            // invisible hairline against the panel's glass. A flat
            // Rectangle filled with .primary at 15% opacity stays
            // legible in both light and dark mode while still reading
            // as a hairline rather than a hard rule.
            Rectangle()
                .fill(Color.primary.opacity(0.15))
                .frame(height: 0.5)

            ScrollView {
                VStack(spacing: 22) {
                    ForEach(groups) { group in
                        ColumnFilterGroupView(
                            group: group,
                            bindingFor: binding(for:)
                        )
                    }
                }
                .padding(.horizontal, 16)
                .padding(.top, 16)
                // Bottom padding clears the home indicator on safe-
                // area-extending phones since the panel ignores the
                // bottom inset for its material backdrop.
                .padding(.bottom, 36)
            }
            .scrollContentBackground(.hidden)
            .background(Color.clear)
        }
        .frame(maxWidth: .infinity)
        // Cap height to roughly the bottom 78% of the screen so the
        // user can still see (and tap) the dimmed profile content
        // peeking out above. 720pt is enough for any of the longer
        // column groups; on shorter phones the panel naturally
        // shrinks to fit.
        .frame(maxHeight: 720)
        // .regularMaterial reads as a lighter frosted surface than
        // .ultraThinMaterial in dark mode, where ultraThin showed too
        // much of the dark content behind and made the panel feel
        // like a dark overlay. regularMaterial trades a bit of
        // transparency in light mode for consistent legibility in
        // both schemes.
        .background(.regularMaterial)
        .clipShape(UnevenRoundedRectangle(cornerRadii: .init(
            topLeading: 24, bottomLeading: 0, bottomTrailing: 0, topTrailing: 24
        )))
        // Soft shadow above the panel reinforces the elevation cue
        // even when the dim layer alone isn't enough contrast.
        .shadow(color: .black.opacity(0.12), radius: 12, x: 0, y: -4)
        // Let the material extend to the absolute screen bottom; the
        // inner ScrollView's bottom padding above handles home-
        // indicator clearance for the toggle rows.
        .ignoresSafeArea(.container, edges: .bottom)
    }

    private func binding(for key: String) -> Binding<Bool> {
        Binding(
            get: { visible.contains(key) },
            set: { newValue in
                if newValue { visible.insert(key) }
                else        { visible.remove(key) }
            }
        )
    }
}

// MARK: - Column filter sheet subviews

/// One group ("Rate Stats", "Counting — Offense", …) inside the column
/// filter sheet. Section title + a column of `ColumnFilterRow`s with
/// hairline dividers between them. Sits on the sheet's glass — no
/// internal background, just the dividers as visual structure.
private struct ColumnFilterGroupView: View {
    let group: ColumnFilterGroup
    let bindingFor: (String) -> Binding<Bool>

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            // Section title sits on the panel's glass (not inside the
            // card). .primary at 50% gives the same contrast it
            // showed at on the all-glass version.
            Text(group.title)
                .font(.caption2.weight(.bold))
                .foregroundStyle(.primary)
                .opacity(0.5)
                .textCase(.uppercase)
                .tracking(1.0)
                .padding(.horizontal, 16)
                .padding(.bottom, 2)

            // Form-grouped-style card. systemBackground is solid white
            // in light mode and solid black in dark mode — high
            // contrast against the panel's regularMaterial backdrop in
            // both schemes. Inside the card, row content uses standard
            // .primary / .secondary text styles.
            VStack(spacing: 0) {
                ForEach(Array(group.columns.enumerated()), id: \.element.id) { idx, col in
                    ColumnFilterRow(column: col, isOn: bindingFor(col.key))
                    if idx != group.columns.count - 1 {
                        Divider()
                            .padding(.leading, 16)
                    }
                }
            }
            .background(Color(.systemBackground), in: RoundedRectangle(cornerRadius: 12))
        }
    }
}

/// Single toggle row. Earlier this fed a VStack into `Toggle`'s label
/// slot, but the iOS 26 switch style was collapsing the two-line label
/// (abbreviation + description) so the description never rendered.
/// Splitting into an explicit HStack + a labelless Toggle gives us
/// total control over the label layout. Tap target spans the full row
/// via `contentShape(Rectangle())`; the gesture flips the binding,
/// matching the switch toggle behavior.
private struct ColumnFilterRow: View {
    let column: ColumnFilterEntry
    @Binding var isOn: Bool

    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text(column.label)
                    .font(.body.weight(.semibold))
                    .foregroundStyle(.primary)
                // Rows sit inside the systemBackground card now, so
                // .secondary reads correctly — it was only the
                // ultraThinMaterial glass backdrop that washed it out
                // in the earlier all-glass layout.
                Text(column.description)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
            Toggle("", isOn: $isOn)
                .labelsHidden()
        }
        // 16pt horizontal inset to match iOS Form's standard
        // grouped-section row indent (label text aligns with the
        // section header above the card).
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .contentShape(Rectangle())
        .onTapGesture { isOn.toggle() }
    }
}

// MARK: - Header bio row

/// Compact label/value row inside the player header card. Label sits
/// left in secondary color, value right-aligned in primary. .caption
/// throughout — denser than the previous standalone Player Info card.
private struct HeaderBioRow: View {
    let label: String
    let value: String

    var body: some View {
        HStack(spacing: 8) {
            Text(label).foregroundStyle(.secondary)
            Spacer(minLength: 8)
            Text(value)
        }
        .font(.caption)
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
    // Additional sums used by the full-detail career table totals row.
    let g:    Int
    let r:    Int
    let rbi:  Int
    let cs:   Int
    let so:   Int
    let gidp: Int
    let sh:   Int
    let ibb:  Int
    let war:  Double

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

    /// Career total bases. h + 2B + 2*3B + 3*HR is the closed form of
    /// 1*singles + 2*2B + 3*3B + 4*HR with `singles = h - 2B - 3B - HR`.
    var tb: Int {
        h + dbl + 2 * trp + 3 * hr
    }

    static func compute(seasons: [CareerSeason]) -> BattingCareerAgg {
        BattingCareerAgg(
            pa:   seasons.reduce(0) { $0 + ($1.PA ?? 0) },
            ab:   seasons.reduce(0) { $0 + ($1.AB ?? 0) },
            h:    seasons.reduce(0) { $0 + ($1.H ?? 0) },
            bb:   seasons.reduce(0) { $0 + ($1.BB ?? 0) },
            hbp:  seasons.reduce(0) { $0 + ($1.HBP ?? 0) },
            sf:   seasons.reduce(0) { $0 + ($1.SF ?? 0) },
            dbl:  seasons.reduce(0) { $0 + ($1.doubles ?? 0) },
            trp:  seasons.reduce(0) { $0 + ($1.triples ?? 0) },
            hr:   seasons.reduce(0) { $0 + ($1.HR ?? 0) },
            sb:   seasons.reduce(0) { $0 + ($1.SB ?? 0) },
            g:    seasons.reduce(0) { $0 + ($1.G ?? 0) },
            r:    seasons.reduce(0) { $0 + ($1.R ?? 0) },
            rbi:  seasons.reduce(0) { $0 + ($1.RBI ?? 0) },
            cs:   seasons.reduce(0) { $0 + ($1.CS ?? 0) },
            so:   seasons.reduce(0) { $0 + ($1.SO ?? 0) },
            gidp: seasons.reduce(0) { $0 + ($1.GIDP ?? 0) },
            sh:   seasons.reduce(0) { $0 + ($1.SH ?? 0) },
            ibb:  seasons.reduce(0) { $0 + ($1.IBB ?? 0) },
            war:  seasons.reduce(0.0) { $0 + ($1.WAR ?? 0) }
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
    let r: Int
    let bb: Int
    let ibb: Int
    let so: Int
    let hr: Int
    let hbp: Int
    let bk: Int
    let wp: Int
    let bf: Int
    let g: Int
    let gs: Int
    let gf: Int
    let cg: Int
    let sho: Int
    let sv: Int
    let w: Int
    let l: Int
    let war: Double

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

    var hPer9: Double? {
        ip > 0 ? Double(h) * 9.0 / ip : nil
    }

    var hrPer9: Double? {
        ip > 0 ? Double(hr) * 9.0 / ip : nil
    }

    /// SO / BB — strikeout-to-walk ratio. nil when BB == 0 (would
    /// divide by zero or render as a misleading "infinite" rate).
    var soBB: Double? {
        bb > 0 ? Double(so) / Double(bb) : nil
    }

    /// W-L percentage. nil when both W and L are 0 (no decisions).
    var winPct: Double? {
        let total = w + l
        return total > 0 ? Double(w) / Double(total) : nil
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
            r:   seasons.reduce(0)   { $0 + ($1.R   ?? 0) },
            bb:  seasons.reduce(0)   { $0 + ($1.BB  ?? 0) },
            ibb: seasons.reduce(0)   { $0 + ($1.IBB ?? 0) },
            so:  seasons.reduce(0)   { $0 + ($1.SO  ?? 0) },
            hr:  seasons.reduce(0)   { $0 + ($1.HR  ?? 0) },
            hbp: seasons.reduce(0)   { $0 + ($1.HBP ?? 0) },
            bk:  seasons.reduce(0)   { $0 + ($1.BK  ?? 0) },
            wp:  seasons.reduce(0)   { $0 + ($1.WP  ?? 0) },
            bf:  seasons.reduce(0)   { $0 + ($1.BFP ?? 0) },
            g:   seasons.reduce(0)   { $0 + ($1.G   ?? 0) },
            gs:  seasons.reduce(0)   { $0 + ($1.GS  ?? 0) },
            gf:  seasons.reduce(0)   { $0 + ($1.GF  ?? 0) },
            cg:  seasons.reduce(0)   { $0 + ($1.CG  ?? 0) },
            sho: seasons.reduce(0)   { $0 + ($1.SHO ?? 0) },
            sv:  seasons.reduce(0)   { $0 + ($1.SV  ?? 0) },
            w:   seasons.reduce(0)   { $0 + ($1.W   ?? 0) },
            l:   seasons.reduce(0)   { $0 + ($1.L   ?? 0) },
            war: seasons.reduce(0.0) { $0 + ($1.WAR ?? 0) }
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
            hof_year: nil,
            is_pitcher: nil
        ))
    }
}
