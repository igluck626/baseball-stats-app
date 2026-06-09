//
//  TeamHistorySheet.swift
//  BaseballStats
//
//  Season-by-season history for the favorite franchise rendered as a
//  vertical timeline of glass cards, most-recent year first. Each
//  card carries the year, W-L, division finish badge, win %, R/RA,
//  and the deepest postseason round (with series record from the
//  favorite's perspective). Tapping a playoff year's postseason
//  cell expands the card to list every round the team played that
//  October.
//

import SwiftUI

struct TeamHistorySheet: View {
    let entry: MLBTeamCatalog.Entry
    /// Drives the Awards section (`vm.teamAwards`) and the Franchise
    /// Leaders section (`vm.loadTeamLeaderboard`). Shared with the
    /// Home view so the same already-loaded data backs the sheet.
    @ObservedObject var vm: HomeViewModel
    /// Reuses the shared `TeamStanding` shape (already used by the
    /// Standings tab) — a strict superset of what this sheet shows.
    let history: [TeamStanding]
    /// Pre-grouped `{year → [series]}` lookup so each row can render
    /// its postseason cell with one dict access instead of walking
    /// the full array.
    let postseasonByYear: [Int: [TeamPostseasonSeries]]
    let isLoading: Bool

    @Environment(\.dismiss) private var dismiss

    /// The three top-level sections selected by the segmented picker.
    /// Named `HistorySection` rather than `Section` so it can't be
    /// confused with SwiftUI's own `Section` inside this file.
    enum HistorySection: String, CaseIterable, Hashable {
        case history, awards, leaders

        /// Compact label for the segmented control (full names don't
        /// fit three segments on an iPhone width).
        var segmentLabel: String {
            switch self {
            case .history: return "Seasons"
            case .awards:  return "Awards"
            case .leaders: return "Leaders"
            }
        }

        /// Full title shown in the nav bar.
        var navTitle: String {
            switch self {
            case .history: return "Season History"
            case .awards:  return "Awards"
            case .leaders: return "Franchise Leaders"
            }
        }
    }

    @State private var section: HistorySection = .history
    /// Nav stack for tapping a winner / leader through to their
    /// profile. Lives here so both the Awards and Franchise Leaders
    /// sections push onto the same stack.
    @State private var path = NavigationPath()

    /// Tracks which year cards have their playoff-rounds dropdown
    /// expanded. Lives on the sheet so the open/closed state survives
    /// scrolling the LazyVStack (each card's `@State` resets on
    /// recycle, but a sheet-level set is stable).
    @State private var expandedYears: Set<Int> = []

    private var tint: Color {
        TeamColors.color(for: entry.lahmanCode) ?? .accentColor
    }

    /// World Series border tint — gold (#FFD700-ish) at 0.8 opacity.
    /// Pulled out so the value lives in one place rather than being
    /// scattered across YearCard's overlay logic.
    private static let worldSeriesBorder = Color(
        red: 1.0, green: 0.84, blue: 0.0,
    ).opacity(0.8)

    var body: some View {
        NavigationStack(path: $path) {
            VStack(spacing: 0) {
                Picker("Section", selection: $section) {
                    ForEach(HistorySection.allCases, id: \.self) { s in
                        Text(s.segmentLabel).tag(s)
                    }
                }
                .pickerStyle(.segmented)
                .padding(.horizontal, 16)
                .padding(.top, 10)
                .padding(.bottom, 8)

                sectionContent
            }
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .principal) {
                    HStack(spacing: 6) {
                        TeamLogoView(team: entry.teamInfo, size: 22)
                        Text(section.navTitle)
                            .font(.headline.weight(.semibold))
                    }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Close") { dismiss() }
                }
            }
            // Both the Awards and Franchise Leaders sections push the
            // tapped player's profile onto this shared stack.
            .navigationDestination(for: PlayerSearchResult.self) { player in
                PlayerProfileView(player: player)
            }
        }
        .presentationBackground(.ultraThinMaterial)
    }

    @ViewBuilder
    private var sectionContent: some View {
        switch section {
        case .history:
            seasonHistory
        case .awards:
            AwardsSection(awards: vm.teamAwards, tint: tint, path: $path)
        case .leaders:
            FranchiseLeadersSection(entry: entry, vm: vm, tint: tint)
        }
    }

    @ViewBuilder
    private var seasonHistory: some View {
        if isLoading && history.isEmpty {
            VStack {
                Spacer()
                ProgressView()
                Spacer()
            }
        } else if history.isEmpty {
            VStack(spacing: 6) {
                Spacer()
                Image(systemName: "clock.arrow.circlepath")
                    .font(.title)
                    .foregroundStyle(.tertiary)
                Text("No history available")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                Spacer()
            }
        } else {
            ScrollView {
                LazyVStack(spacing: 14) {
                    ForEach(Array(history.enumerated()), id: \.offset) { idx, row in
                        let year = row.year ?? 0
                        YearCard(
                            row:           row,
                            postseason:    postseasonByYear[year] ?? [],
                            tint:          tint,
                            wsBorderColor: Self.worldSeriesBorder,
                            isExpanded:    expandedYears.contains(year),
                            onToggleExpand: {
                                if expandedYears.contains(year) {
                                    expandedYears.remove(year)
                                } else {
                                    expandedYears.insert(year)
                                }
                            },
                        )
                        if idx < history.count - 1 {
                            Divider().opacity(0.5)
                                .padding(.horizontal, 12)
                        }
                    }
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 12)
                .animation(.spring(response: 0.3), value: expandedYears)
            }
        }
    }

    /// Ordinal depth ranking for postseason rounds; higher = deeper.
    /// Kept at file scope so `YearCard` can reach it without paying
    /// the cost of a duplicate lookup table.
    static func depth(of round: String) -> Int {
        switch round {
        case "World Series":                          return 4
        case "ALCS", "NLCS":                          return 3
        case "ALDS", "NLDS":                          return 2
        case "AL Wild Card", "NL Wild Card":          return 1
        default:                                       return 0
        }
    }

    /// Lahman team_id → modern Baseball Reference / display code.
    /// Covers every modern franchise plus the common relocation
    /// codes Lahman uses for the same franchise across its history
    /// (FLO → MIA, MON → WSN, ANA → LAA, etc.). Codes already in
    /// modern form pass through via the `displayCode(_:)` fallback.
    static let lahmanToDisplay: [String: String] = [
        "NYA": "NYY", "NYN": "NYM", "LAN": "LAD", "SLN": "STL",
        "SFN": "SF",  "SDN": "SD",  "KCA": "KC",  "TBA": "TB",
        "CHA": "CWS", "CHN": "CHC", "ANA": "LAA", "LAA": "LAA",
        "FLO": "MIA", "MIA": "MIA", "MON": "WSN", "WAS": "WSN",
        "HOU": "HOU", "ATL": "ATL", "PHI": "PHI", "BOS": "BOS",
        "MIN": "MIN", "CLE": "CLE", "DET": "DET", "OAK": "OAK",
        "SEA": "SEA", "TEX": "TEX", "BAL": "BAL", "TOR": "TOR",
        "MIL": "MIL", "CIN": "CIN", "PIT": "PIT", "ARI": "ARI",
        "COL": "COL",
    ]

    /// Convert a Lahman team_id to a display abbreviation. Unmapped
    /// codes pass through unchanged so historical / 19th-century
    /// team_ids (PRO, NY4, SL4, BS1, etc.) still render legibly.
    static func displayCode(_ lahman: String) -> String {
        lahmanToDisplay[lahman] ?? lahman
    }
}

// MARK: - Awards section

/// Franchise major-award winners grouped by award type (MVP, Cy
/// Young, ROY, Gold Glove, Silver Slugger). Each group gets a
/// trophy-iconed header; each winner row carries the year + name and
/// resolves the player_id to a profile on tap.
private struct AwardsSection: View {
    let awards: [TeamAwardGroup]
    let tint: Color
    @Binding var path: NavigationPath

    /// The winner currently being resolved (by row id) — drives the
    /// inline spinner and guards against double-taps.
    @State private var resolvingId: String?

    /// #FFD700-ish gold for MVP / Gold Glove icons.
    private static let gold = Color(red: 1.0, green: 0.84, blue: 0.0)
    /// Muted silver for the Silver Slugger icon.
    private static let silver = Color(red: 0.75, green: 0.76, blue: 0.80)

    var body: some View {
        if awards.isEmpty {
            emptyState
        } else {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 20) {
                    ForEach(awards) { group in
                        awardGroup(group)
                    }
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 14)
            }
        }
    }

    private var emptyState: some View {
        VStack(spacing: 6) {
            Spacer()
            Image(systemName: "trophy")
                .font(.title)
                .foregroundStyle(.tertiary)
            Text("No major awards on record")
                .font(.subheadline)
                .foregroundStyle(.secondary)
            Spacer()
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    @ViewBuilder
    private func awardGroup(_ group: TeamAwardGroup) -> some View {
        let style = Self.style(for: group.award, tint: tint)
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Image(systemName: style.icon)
                    .font(.headline)
                    .foregroundStyle(style.color)
                Text(group.award)
                    .font(.headline.weight(.bold))
                Spacer()
                Text("\(group.winners.count)")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
            VStack(spacing: 2) {
                ForEach(group.winners) { winner in
                    winnerRow(winner)
                }
            }
        }
    }

    private func winnerRow(_ w: TeamAwardWinner) -> some View {
        Button {
            resolveAndPush(w)
        } label: {
            HStack(spacing: 12) {
                Text(String(w.year))
                    .font(.subheadline.weight(.bold))
                    .monospacedDigit()
                    .foregroundStyle(tint)
                    .frame(width: 48, alignment: .leading)
                Text(w.name)
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
                if let lg = w.league, !lg.isEmpty {
                    Text(lg)
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
                if resolvingId == w.id {
                    ProgressView()
                        .controlSize(.small)
                }
            }
            .padding(.vertical, 4)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    /// Resolve the MLBAM id to a full `PlayerSearchResult` and push
    /// the profile. Mirrors the resolve-on-tap pattern the injury
    /// rows use — the awards payload only carries the id + name.
    private func resolveAndPush(_ w: TeamAwardWinner) {
        guard resolvingId == nil else { return }
        resolvingId = w.id
        Task { @MainActor in
            let player = (try? await APIClient.shared.getPlayerByMlbId(w.player_id)) ?? nil
            if let player { path.append(player) }
            resolvingId = nil
        }
    }

    /// SF Symbol + tint for each award type. Falls back to a generic
    /// rosette for anything the backend adds later. `tint` is the
    /// team color (used for Cy Young / ROY).
    private static func style(
        for award: String, tint: Color,
    ) -> (icon: String, color: Color) {
        switch award {
        case "MVP":                return ("trophy.fill",     gold)
        case "CY Young":           return ("baseball.fill",   tint)
        case "Rookie of the Year", "ROY":
                                   return ("star.fill",       tint)
        case "Gold Glove":         return ("hand.raised.fill", gold)
        case "Silver Slugger":     return ("figure.baseball", silver)
        default:                   return ("rosette",          tint)
        }
    }
}

// MARK: - Franchise Leaders section

/// All-time franchise leaderboards. Batting/Pitching segmented
/// toggle + a scrollable stat-pill row; the selected stat shows the
/// top 10 career leaders scoped to the franchise. Reuses the
/// `TeamLeadersSheet` loading + row pattern, but in `mode: "career"`.
private struct FranchiseLeadersSection: View {
    let entry: MLBTeamCatalog.Entry
    @ObservedObject var vm: HomeViewModel
    let tint: Color

    enum Role: String, Hashable { case batting, pitching }

    @State private var role: Role = .batting
    @State private var stat: String = "HR"
    @State private var leaders: [LeaderCard] = []
    @State private var isLoading: Bool = false

    private static let battingStats:  [String] = ["HR", "AVG", "RBI", "WAR", "H", "SB", "OPS"]
    private static let pitchingStats: [String] = ["W", "SO", "ERA", "WAR", "IP", "SV"]

    private var currentStats: [String] {
        role == .batting ? Self.battingStats : Self.pitchingStats
    }

    var body: some View {
        VStack(spacing: 0) {
            Picker("Role", selection: $role) {
                Text("Batting").tag(Role.batting)
                Text("Pitching").tag(Role.pitching)
            }
            .pickerStyle(.segmented)
            .padding(.horizontal, 16)
            .padding(.vertical, 8)

            statPillRow

            Divider().padding(.top, 4)

            content
        }
        .task {
            stat = currentStats.first ?? "HR"
            await load()
        }
        .onChange(of: role) { _, _ in
            // Clear stale rows immediately so the role flip doesn't
            // flash the previous side's numbers mid-fetch.
            leaders = []
            let newFirst = currentStats.first ?? "HR"
            if stat == newFirst {
                Task { await load() }
            } else {
                stat = newFirst
            }
        }
        .onChange(of: stat) { _, _ in
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
            .frame(maxWidth: .infinity, maxHeight: .infinity)
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
            .frame(maxWidth: .infinity, maxHeight: .infinity)
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
            Text(TeamLeadersSheet.formatValue(card.value, stat: card.stat))
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
            stat: stat, playerType: playerType, mode: "career",
        )
        isLoading = false
    }
}

// MARK: - Year card

/// One year's accomplishments. Top row: year + W-L + finish-pill.
/// Bottom row: win% + R/RA + tappable postseason summary (when the
/// team made the playoffs). Tapping the postseason cell expands the
/// card with a chronological round-by-round breakdown.
///
/// Border policy: World Series winners get a gold border (handled
/// here); every other year is borderless — the inner glass material
/// + drop shadow is enough visual separation.
private struct YearCard: View {
    let row: TeamStanding
    let postseason: [TeamPostseasonSeries]
    let tint: Color
    let wsBorderColor: Color
    let isExpanded: Bool
    let onToggleExpand: () -> Void

    private var year: Int { row.year ?? 0 }

    private var isDivisionWinner: Bool {
        row.rank == 1 || row.division_leader == true
    }

    private var hasPostseason: Bool { !postseason.isEmpty }

    /// Series the team played that October, sorted chronologically
    /// (Wild Card first → LDS → LCS → World Series last).
    private var orderedSeries: [TeamPostseasonSeries] {
        postseason.sorted {
            TeamHistorySheet.depth(of: $0.round) <
            TeamHistorySheet.depth(of: $1.round)
        }
    }

    private var deepestRound: TeamPostseasonSeries? {
        orderedSeries.last
    }

    private var wonWorldSeries: Bool {
        deepestRound?.round == "World Series" && deepestRound?.won == true
    }

    var body: some View {
        VStack(spacing: 6) {
            topRow
            bottomRow
            if isExpanded && hasPostseason {
                Divider().padding(.top, 2)
                expandedSection
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 14)
        .background(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .fill(Color(.systemBackground).opacity(0.5))
        )
        .overlay(borderOverlay)
    }

    @ViewBuilder
    private var borderOverlay: some View {
        if wonWorldSeries {
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .stroke(wsBorderColor, lineWidth: 1.5)
        }
        // All other years: no border. The glass fill + shadow are
        // the only separators.
    }

    // MARK: Top row — year + W-L + finish badge

    private var topRow: some View {
        HStack(spacing: 10) {
            Text(String(year))
                .font(.title2.bold())
                .foregroundStyle(isDivisionWinner ? tint : .primary)
                .monospacedDigit()
            Text(wlText)
                .font(.headline.weight(.semibold))
                .foregroundStyle(.primary)
                .monospacedDigit()
            Spacer(minLength: 0)
            finishPill
        }
    }

    private var wlText: String {
        guard let w = row.W, let l = row.L else { return "—" }
        return "\(w)-\(l)"
    }

    private var finishPill: some View {
        let label = isDivisionWinner ? "🏆 DIV" : ordinalRank
        return Text(label)
            .font(.caption2.weight(.semibold))
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(
                isDivisionWinner ? tint.opacity(0.2) : Color(.systemFill),
                in: Capsule(),
            )
            .foregroundStyle(isDivisionWinner ? tint : .secondary)
    }

    private var ordinalRank: String {
        guard let r = row.rank, r > 0 else { return "—" }
        switch r {
        case 1: return "1st"
        case 2: return "2nd"
        case 3: return "3rd"
        default: return "\(r)th"
        }
    }

    // MARK: Bottom row — pct + R/RA + postseason

    private var bottomRow: some View {
        HStack(spacing: 10) {
            Text(pctText)
                .font(.caption.weight(.medium))
                .foregroundStyle(.secondary)
                .monospacedDigit()
            if !rsRaText.isEmpty {
                Text(rsRaText)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
            Spacer(minLength: 0)
            postseasonCell
        }
    }

    /// 3-decimal win-pct with the leading zero stripped — ".605"
    /// matches the batting-rate convention used across the app.
    private var pctText: String {
        guard let p = row.win_pct else { return "—" }
        let s = String(format: "%.3f", p)
        if s.hasPrefix("0.")  { return String(s.dropFirst()) }
        if s.hasPrefix("-0.") { return "-" + String(s.dropFirst(2)) }
        return s
    }

    /// "R: 842 RA: 686" — collapses to empty when either field is
    /// missing so the row doesn't render a partial half-line.
    private var rsRaText: String {
        guard let rs = row.runs_scored, let ra = row.runs_allowed else { return "" }
        return "R: \(rs) RA: \(ra)"
    }

    // MARK: Postseason cell (tappable when there's a postseason)

    @ViewBuilder
    private var postseasonCell: some View {
        if hasPostseason {
            Button(action: onToggleExpand) {
                HStack(spacing: 4) {
                    Text(postseasonText)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(postseasonColor)
                        .lineLimit(1)
                    Image(systemName: isExpanded ? "chevron.down" : "chevron.right")
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(.secondary)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
        } else {
            Text("—")
                .font(.caption.weight(.semibold))
                .foregroundStyle(Color(.tertiaryLabel))
        }
    }

    /// Deepest-round summary as `"<Round> (W-L)"` with the record
    /// flipped to the favorite's perspective (Lahman ships wins/
    /// losses from the WINNER's perspective; if `won == false`, the
    /// favorite is the loser and the record needs to be inverted).
    /// Appends "※" to any 2017 series the favorite played against
    /// the Astros — sign-stealing tainted every Astros postseason
    /// matchup that year (Red Sox ALDS, Yankees ALCS, Dodgers WS).
    /// The footnote at the bottom of the sheet explains the marker.
    private var postseasonText: String {
        guard let series = deepestRound else { return "—" }
        let (favWins, favLosses) = Self.favoritePerspective(series)
        let asterisk = (year == 2017 && series.opponent == "HOU") ? "※" : ""
        let rec = "(\(favWins)-\(favLosses))\(asterisk)"
        if series.round == "World Series" {
            return series.won
                ? "🏆 World Series \(rec)"
                : "World Series \(rec)"
        }
        return "\(series.round) \(rec)"
    }

    private var postseasonColor: Color {
        if wonWorldSeries          { return tint }
        if !hasPostseason          { return Color(.tertiaryLabel) }
        return .primary
    }

    // MARK: Expanded round-by-round breakdown

    private var expandedSection: some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(orderedSeries, id: \.id) { series in
                roundRow(series)
            }
        }
        .padding(.top, 2)
    }

    private func roundRow(_ series: TeamPostseasonSeries) -> some View {
        let (favWins, favLosses) = Self.favoritePerspective(series)
        let resultChar = series.won ? "W" : "L"
        let astrosTainted = (year == 2017 && series.opponent == "HOU")
        let asterisk = astrosTainted ? "※" : ""
        return VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 6) {
                Text(series.round)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Text("·")
                    .font(.caption)
                    .foregroundStyle(.tertiary)
                Text("vs \(TeamHistorySheet.displayCode(series.opponent))")
                    .font(.caption)
                    .foregroundStyle(.primary)
                Text("·")
                    .font(.caption)
                    .foregroundStyle(.tertiary)
                Text("\(resultChar) \(favWins)-\(favLosses)\(asterisk)")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(series.won ? .green : .red)
                    .monospacedDigit()
                Spacer(minLength: 0)
            }
            if astrosTainted {
                HStack(spacing: 3) {
                    Text("※ Astros cheated")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .italic()
                    Image(systemName: "trash.fill")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                .padding(.leading, 8)
            }
        }
    }

    /// Lahman's `SeriesPost.csv` stores `wins` / `losses` from the
    /// SERIES winner's perspective. When the favorite team is the
    /// loser, we flip so the displayed record always reads favorite-
    /// wins-first.
    private static func favoritePerspective(
        _ series: TeamPostseasonSeries,
    ) -> (wins: Int, losses: Int) {
        if series.won {
            return (series.wins, series.losses)
        } else {
            return (series.losses, series.wins)
        }
    }
}
