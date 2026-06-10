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

    /// Franchise Leaders selection — hoisted to the sheet so it
    /// survives navigating into a player profile and back (a child
    /// view's own `@State` would reset when its `.task` re-fired on
    /// return, snapping the user back to WAR). Passed down as
    /// bindings.
    @State private var leaderRole: FranchiseLeadersSection.Role = .batting
    @State private var leaderStat: String = "WAR"

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
            FranchiseLeadersSection(
                entry: entry, vm: vm, tint: tint,
                role: $leaderRole, stat: $leaderStat,
            )
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

// MARK: - Shared stat formatting

/// Comma-groups integers ≥ 1000 so career totals read cleanly
/// (1523 → "1,523"); plain integer below 1000 (245 → "245"); "—" for
/// nil. Used by both the Awards stat rows and the Franchise Leaders
/// rows for counting stats that can run into the thousands over a
/// career (H, RBI, SO, …).
func formatLargeInt(_ n: Double?) -> String {
    guard let n else { return "—" }
    let i = Int(n.rounded())
    if abs(i) >= 1000 {
        return teamStatGroupingFormatter.string(from: NSNumber(value: i)) ?? String(i)
    }
    return String(i)
}

private let teamStatGroupingFormatter: NumberFormatter = {
    let f = NumberFormatter()
    f.numberStyle = .decimal
    f.groupingSeparator = ","
    f.maximumFractionDigits = 0
    return f
}()

/// Single stat-value formatter shared by the Awards + Leaders rows.
/// Counting stats that can reach four digits over a career route
/// through `formatLargeInt` (comma grouping); IP gets comma grouping
/// only past 999 (otherwise the usual baseball-outs notation); every
/// other stat defers to `TeamLeadersSheet.formatValue` (rate-stat
/// precision, ERA/WHIP 2-decimal, WAR 1-decimal, etc.).
func formatTeamStatValue(_ value: Double?, stat: String) -> String {
    switch stat {
    case "H", "RBI", "SO", "BB", "R", "AB", "PA", "TB", "W":
        return formatLargeInt(value)
    case "IP":
        if let v = value, v > 999 { return formatLargeInt(value) }
        return TeamLeadersSheet.formatValue(value, stat: "IP")
    default:
        return TeamLeadersSheet.formatValue(value, stat: stat)
    }
}

// MARK: - Awards section

/// Franchise major-award winners. A horizontal pill row selects the
/// award type (MVP · CY YOUNG · ROY · GOLD GLOVE · SILVER SLUGGER) —
/// same pill style as the See-All Stats sheet — and the winners for
/// the selected award render below in the same row layout: year +
/// name + position + the award-year stat line (right-aligned,
/// fixed-width cells). Tapping a row resolves the player_id to a
/// profile.
private struct AwardsSection: View {
    let awards: [TeamAwardGroup]
    let tint: Color
    @Binding var path: NavigationPath

    /// Currently-selected award pill. Empty until the first `.task`
    /// seeds it with the first available award.
    @State private var selectedAward: String = ""
    /// Career-stats caches keyed by player_id, populated once by a bulk
    /// prefetch when the Awards tab first appears. Awards data is
    /// static (changes once a year), so we fetch every winner's career
    /// up front and render every row synchronously from cache — no
    /// per-row async, no fetch races, and no rate-limit storm from a
    /// multi-award player's many rows each firing their own request.
    @State private var battingCareerCache: [Int: PlayerCareerStats] = [:]
    @State private var pitchingCareerCache: [Int: PitcherCareerStats] = [:]
    /// True while the one-shot bulk prefetch is in flight (drives the
    /// single loading spinner); `prefetched` guards it to run once.
    @State private var isLoadingStats: Bool = false
    @State private var prefetched: Bool = false
    /// The winner currently being resolved for navigation — drives
    /// the inline spinner and guards against double-taps.
    @State private var resolvingId: String?

    /// #FFD700-ish gold for MVP / Gold Glove icons.
    private static let gold = Color(red: 1.0, green: 0.84, blue: 0.0)
    /// Muted silver for the Silver Slugger icon.
    private static let silver = Color(red: 0.75, green: 0.76, blue: 0.80)

    /// Synchronous per-winner display data, computed at render time
    /// from the pre-fetched career caches (no async, no per-row fetch).
    struct AwardStatLine {
        var position: String = ""
        /// True when the (single) award-year line is a pitching line
        /// (IP-based). Ignored when `pitchingValues` is set (two-way).
        var isPitcher: Bool = false
        /// Primary stat line. For a two-way player this is the batting
        /// line and `pitchingValues` carries the pitching one; missing
        /// keys render as "—".
        var values: [String: Double] = [:]
        /// Non-nil only for a two-way award year — the pitching line
        /// rendered beneath the batting line ("BAT" / "PIT" labeled).
        var pitchingValues: [String: Double]? = nil
    }

    /// Award-year stat lines. Which one a row uses is decided per
    /// player (by resolved position), not per award — so a pitcher who
    /// won an MVP / ROY shows the pitching line. Gold Glove shows no
    /// stat line at all (handled separately).
    private static let battingStatLabels:  [String] = ["WAR", "AVG", "HR", "RBI", "OPS"]
    private static let pitchingStatLabels: [String] = ["WAR", "ERA", "W", "SO", "WHIP"]

    /// Per-column fixed widths, positional — index N applies to the
    /// N-th label in either stat array (WAR / AVG·ERA / HR·W / RBI·SO /
    /// OPS·WHIP). Fixed so values never wrap and the once-per-group
    /// column header lines up exactly over every row's cells.
    private static let statColumnWidths: [CGFloat] = [38, 44, 32, 36, 48]
    private static let statColumnSpacing: CGFloat = 8
    /// "BAT" / "PIT" gutter width on the two-way lines.
    private static let twoWayLabelWidth: CGFloat = 30

    /// The column header a group shows: pitching for Cy Young,
    /// batting for everything else (MVP / ROY / Silver Slugger). Gold
    /// Glove has no stat line, so no header.
    private static func headerLabels(for award: String) -> [String] {
        award == "CY Young" ? pitchingStatLabels : battingStatLabels
    }

    private var selectedGroup: TeamAwardGroup? {
        awards.first { $0.award == selectedAward } ?? awards.first
    }

    /// Gold Glove is the only stat-less award (it's a fielding honor;
    /// we don't surface defensive metrics here).
    private static func isFielding(_ award: String) -> Bool {
        award == "Gold Glove"
    }

    /// Innings-pitched floor that distinguishes a genuine pitching
    /// season from a position player's mop-up / position-player-
    /// pitching cameo. At/above this we render the pitching line.
    private static let pitcherIPThreshold: Double = 10.0

    /// Award-year batting line from a `CareerSeason`. Nils drop out so
    /// missing stats render as "—".
    private static func battingValues(_ s: CareerSeason) -> [String: Double] {
        [
            "WAR": s.WAR, "AVG": s.BA,
            "HR": s.HR.map(Double.init),
            "RBI": s.RBI.map(Double.init), "OPS": s.OPS,
        ].compactMapValues { $0 }
    }

    /// Award-year pitching line from a `PitcherCareerSeason`.
    private static func pitchingValues(_ s: PitcherCareerSeason) -> [String: Double] {
        [
            "WAR": s.WAR, "ERA": s.ERA,
            "W": s.W.map(Double.init),
            "SO": s.SO.map(Double.init), "WHIP": s.WHIP,
        ].compactMapValues { $0 }
    }

    /// Short pill label per award — keeps the filter row compact.
    private static func pillLabel(_ award: String) -> String {
        switch award {
        case "MVP":                            return "MVP"
        case "CY Young", "CY Young Award":     return "CY"
        case "Rookie of the Year", "ROY":      return "ROY"
        case "Gold Glove":                     return "GG"
        case "Silver Slugger":                 return "SS"
        default:                               return award.uppercased()
        }
    }

    var body: some View {
        if awards.isEmpty {
            emptyState
        } else {
            VStack(spacing: 0) {
                pillRow
                Divider().padding(.top, 4)
                content
            }
            .task {
                if selectedAward.isEmpty {
                    selectedAward = awards.first?.award ?? ""
                }
                await prefetchAll()
            }
        }
    }

    /// One loading spinner while the bulk prefetch runs; once it's
    /// done every row renders synchronously from cache.
    @ViewBuilder
    private var content: some View {
        if isLoadingStats {
            VStack {
                Spacer()
                ProgressView()
                Spacer()
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            winnersList
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

    private var pillRow: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                ForEach(awards) { group in
                    let selected = (group.award == selectedAward)
                    Button {
                        selectedAward = group.award
                    } label: {
                        Text("\(Self.pillLabel(group.award)) (\(group.winners.count))")
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
            .padding(.vertical, 8)
        }
    }

    @ViewBuilder
    private var winnersList: some View {
        if let group = selectedGroup {
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    header(for: group)
                    if !Self.isFielding(group.award) {
                        columnHeader(keys: Self.headerLabels(for: group.award))
                    }
                    ForEach(Array(group.winners.enumerated()), id: \.offset) { idx, winner in
                        winnerRow(winner, award: group.award)
                        if idx < group.winners.count - 1 {
                            Divider().opacity(0.3)
                        }
                    }
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 10)
            }
        }
    }

    /// Award icon + name + winner count.
    private func header(for group: TeamAwardGroup) -> some View {
        let style = Self.style(for: group.award, tint: tint)
        return HStack(spacing: 8) {
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
        .padding(.bottom, 8)
    }

    /// Once-per-group column header — labels right-aligned over the
    /// fixed-width value cells every row renders. No leading content,
    /// so it lines up with each row's trailing cell block regardless
    /// of any "BAT"/"PIT" gutter those rows carry.
    private func columnHeader(keys: [String]) -> some View {
        HStack(spacing: 0) {
            Spacer(minLength: 8)
            HStack(spacing: Self.statColumnSpacing) {
                ForEach(Array(zip(keys, Self.statColumnWidths).enumerated()), id: \.offset) { item in
                    Text(item.element.0)
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(.secondary)
                        .frame(width: item.element.1, alignment: .trailing)
                }
            }
        }
        .padding(.bottom, 6)
    }

    /// Winner row. Line 1: year (team color) + name + position. Line 2
    /// (and 3 for a two-way MVP): bare value cells, right-aligned in
    /// fixed columns under the group header — no inline labels. Two-way
    /// rows carry a small "BAT" / "PIT" gutter on the left.
    private func winnerRow(_ w: TeamAwardWinner, award: String) -> some View {
        let info = enriched(w, award: award)
        let showStats = !Self.isFielding(award)
        return Button {
            resolveAndPush(w)
        } label: {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 12) {
                    Text(String(w.year))
                        .font(.body.weight(.bold))
                        .monospacedDigit()
                        .foregroundStyle(tint)
                        .frame(width: 52, alignment: .leading)
                    Text(w.name)
                        .font(.body.weight(.semibold))
                        .foregroundStyle(.primary)
                        .lineLimit(1)
                        .truncationMode(.tail)
                    if !info.position.isEmpty {
                        Text(info.position)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 6)
                    if resolvingId == w.id {
                        ProgressView().controlSize(.small)
                    }
                }
                if showStats {
                    if let pitching = info.pitchingValues {
                        // Two-way MVP → two labeled lines.
                        statCells(keys: Self.battingStatLabels, values: info.values, prefix: "BAT")
                        statCells(keys: Self.pitchingStatLabels, values: pitching, prefix: "PIT")
                    } else {
                        let keys = info.isPitcher ? Self.pitchingStatLabels : Self.battingStatLabels
                        statCells(keys: keys, values: info.values)
                    }
                }
            }
            .padding(.vertical, 12)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    /// Bare value cells, right-aligned in the fixed columns (no inline
    /// labels — the group header names them). An optional `prefix`
    /// ("BAT"/"PIT") sits in a left gutter for two-way rows; the value
    /// block stays pinned to the trailing edge either way, so it always
    /// aligns with the header.
    private func statCells(
        keys: [String], values: [String: Double], prefix: String? = nil,
    ) -> some View {
        HStack(spacing: 0) {
            if let prefix {
                Text(prefix)
                    .font(.caption2.weight(.bold))
                    .foregroundStyle(.secondary)
                    .frame(width: Self.twoWayLabelWidth, alignment: .leading)
            }
            Spacer(minLength: 8)
            HStack(spacing: Self.statColumnSpacing) {
                ForEach(Array(zip(keys, Self.statColumnWidths).enumerated()), id: \.offset) { item in
                    Text(formatTeamStatValue(values[item.element.0], stat: item.element.0))
                        .font(.caption.weight(.semibold))
                        .monospacedDigit()
                        .foregroundStyle(.primary)
                        .frame(width: item.element.1, alignment: .trailing)
                }
            }
        }
    }

    /// Bulk-prefetch every winner's batting + pitching career once,
    /// when the Awards tab first appears. Awards data is static, so we
    /// pull all ~30–40 unique players up front and then render every
    /// row synchronously from cache — switching pills is instant and
    /// no race condition is possible because nothing fetches during
    /// render. Runs once (guarded by `prefetched`).
    ///
    /// Concurrency is bounded (chunked) rather than firing every id at
    /// once: 40 players × 2 endpoints would be ~80 simultaneous
    /// requests, which is exactly the rate-limit storm this redesign
    /// is meant to avoid.
    private func prefetchAll() async {
        guard !prefetched else { return }
        prefetched = true
        isLoadingStats = true

        let api = APIClient.shared
        // Unique MLBAM ids across every award group (MVP + CY + ROY +
        // GG + SS combined).
        let ids = Array(Set(awards.flatMap { $0.winners.map(\.player_id) }))

        let maxConcurrent = 8
        for chunk in stride(from: 0, to: ids.count, by: maxConcurrent).map({
            Array(ids[$0 ..< min($0 + maxConcurrent, ids.count)])
        }) {
            await withTaskGroup(
                of: (Int, PlayerCareerStats?, PitcherCareerStats?).self
            ) { group in
                for id in chunk {
                    group.addTask { await Self.fetchCareers(id: id, api: api) }
                }
                for await (id, batting, pitching) in group {
                    if let batting  { battingCareerCache[id]  = batting }
                    if let pitching { pitchingCareerCache[id] = pitching }
                }
            }
        }

        isLoadingStats = false
    }

    /// Fetch one player's batting + pitching career concurrently.
    /// `nonisolated` so it can run off the main actor inside the task
    /// group; both sides are best-effort (nil on 404 / failure).
    nonisolated private static func fetchCareers(
        id: Int, api: APIClient,
    ) async -> (Int, PlayerCareerStats?, PitcherCareerStats?) {
        async let b = api.getPlayerCareerStats(playerId: id)
        async let p = api.getPitcherCareerStats(playerId: id)
        return (id, (try? await b) ?? nil, (try? await p) ?? nil)
    }

    /// Synchronous award-year line for one winner, read straight from
    /// the pre-fetched caches. Decides batting vs pitching by the
    /// award-year innings pitched (a real pitching season, IP ≥ 10,
    /// shows the pitching line — so a pitcher-MVP / ROY like Kershaw or
    /// Newcombe reads correctly), except Silver Slugger which always
    /// shows batting, and Gold Glove which shows no stat line at all.
    /// Position comes from whichever career bio we have. Empty values
    /// (a fetch that failed / 404'd) render as "—".
    private func enriched(_ w: TeamAwardWinner, award: String) -> AwardStatLine {
        let batting  = battingCareerCache[w.player_id]
        let pitching = pitchingCareerCache[w.player_id]
        let position = InjuryReportSheet.abbreviatePosition(
            batting?.bio?.position ?? pitching?.bio?.position
        )

        // Gold Glove carries no stat line — position only.
        guard !Self.isFielding(award) else {
            return AwardStatLine(position: position, isPitcher: false, values: [:])
        }

        let batSeason = batting?.seasons?.first  { $0.year == w.year }
        let pitSeason = pitching?.seasons?.first { $0.year == w.year }
        let isSilverSlugger = (award == "Silver Slugger")
        let pitchedEnough = (pitSeason?.IP ?? 0) >= Self.pitcherIPThreshold

        // Two-way (Ohtani): MVP only — a real bat (PA > 150) AND a real
        // arm (IP >= 10) in the award year → show both lines. Scoped to
        // MVP so a pitcher-CY / ROY's token hitting doesn't double up,
        // and Silver Slugger stays batting-only.
        let isMVP = (award == "MVP" || award == "Most Valuable Player")
        let isTwoWay = isMVP
            && (batSeason?.PA ?? 0) > 150
            && (pitSeason?.IP ?? 0) >= 10
        if isTwoWay, let b = batSeason, let p = pitSeason {
            return AwardStatLine(
                position: position, isPitcher: false,
                values: Self.battingValues(b),
                pitchingValues: Self.pitchingValues(p),
            )
        }

        if !isSilverSlugger, let s = pitSeason, pitchedEnough {
            // Genuine pitching season → pitching line.
            return AwardStatLine(position: position, isPitcher: true, values: Self.pitchingValues(s))
        } else if let s = batSeason {
            // Default (and Silver Slugger always) → batting line.
            return AwardStatLine(position: position, isPitcher: false, values: Self.battingValues(s))
        } else if !isSilverSlugger, let s = pitSeason {
            // No batting row for the year but a (sub-threshold)
            // pitching row exists — surface it rather than a blank row.
            return AwardStatLine(position: position, isPitcher: true, values: Self.pitchingValues(s))
        }
        return AwardStatLine(position: position, isPitcher: false, values: [:])
    }

    /// Push the player's profile, resolving the MLBAM id on tap (the
    /// awards payload only carries id + name).
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

    /// Role + stat are bindings owned by the sheet so the selection
    /// persists across a push/pop into a player profile.
    @Binding var role: Role
    @Binding var stat: String
    @State private var leaders: [LeaderCard] = []
    @State private var isLoading: Bool = false

    private static let battingStats:  [String] = [
        "WAR", "HR", "AVG", "RBI", "OPS", "SLG", "OBP",
        "H", "R", "BB", "SB", "2B", "3B", "SO", "PA", "AB",
    ]
    private static let pitchingStats: [String] = [
        "WAR", "W", "SO", "ERA", "WHIP", "SV", "IP",
        "BB", "H", "HR", "SO/9", "CG", "SHO",
    ]

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
        // Load only when we have no rows yet — on a pop-back from a
        // profile `leaders` is still populated, so we skip the refetch
        // and (crucially) never reset `stat`.
        .task {
            if leaders.isEmpty { await load() }
        }
        .onChange(of: role) { _, _ in
            // Clear stale rows immediately so the role flip doesn't
            // flash the previous side's numbers mid-fetch.
            leaders = []
            // Keep the selected stat if it exists on the new side
            // (e.g. WAR heads both); otherwise fall back to that
            // side's first stat. Re-load directly when the stat
            // didn't change (no `onChange(stat)` to ride on).
            if currentStats.contains(stat) {
                Task { await load() }
            } else {
                stat = currentStats.first ?? "WAR"
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
            Text(formatTeamStatValue(card.value, stat: card.stat))
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
