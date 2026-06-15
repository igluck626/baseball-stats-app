//
//  PlayerCompareView.swift
//  BaseballStats
//
//  Phase 1 of the Player Comparison feature: pick 2–4 same-type players
//  (all batters or all pitchers) and compare their CAREER TOTALS side by
//  side, with the best value in each stat row highlighted.
//
//  The comparison TYPE (batter vs pitcher) is decided by the first
//  player added and locked until every player is removed. Two-way
//  players (Ohtani) can join either kind of comparison — we fetch
//  whichever career (batting or pitching) matches the locked type.
//
//  Later phases add the other modes (Year Range / Age Range / Per-162);
//  the mode picker here is a stub with only Career functional.
//

import Combine
import SwiftUI

// MARK: - Domain types

/// Which career a comparison is built from. Set by the first player and
/// held until the roster empties.
enum ComparisonType {
    case batter
    case pitcher

    var isPitcher: Bool { self == .pitcher }
    var noun: String { self == .pitcher ? "pitching" : "batting" }
}

/// Comparison modes. Phase 1 ships Career only; the rest are stubbed so
/// the picker shape is in place for later phases.
enum ComparisonMode: String, CaseIterable, Identifiable {
    case career    = "Career"
    case yearRange = "Year Range"
    case ageRange  = "Age Range"
    case per162    = "Per 162"

    var id: String { rawValue }
    /// Only Career is wired up in Phase 1.
    var isAvailable: Bool { self == .career }
}

/// Whether a higher or lower value wins the best-cell highlight.
enum StatDirection {
    case higherBetter
    case lowerBetter

    static func direction(for stat: String, isPitcher: Bool) -> StatDirection {
        let lowerBetterBatting: Set<String> = ["SO"]
        let lowerBetterPitching: Set<String> = ["ERA", "L", "BB", "WHIP", "H"]
        if isPitcher {
            return lowerBetterPitching.contains(stat) ? .lowerBetter : .higherBetter
        } else {
            return lowerBetterBatting.contains(stat) ? .lowerBetter : .higherBetter
        }
    }
}

/// How a stat value is rendered in a cell.
fileprivate enum StatFormat {
    case int          // 156
    case dec1         // 73.4  (WAR, IP)
    case dec2         // 2.94  (ERA, WHIP)
    case rate3        // .314  (AVG/OBP/SLG/OPS)
}

/// One row of the comparison table — a stat label plus how to format it.
fileprivate struct CompareStat: Identifiable {
    let label: String
    let format: StatFormat
    var id: String { label }
}

/// One player in the comparison. Carries the original search result (for
/// profile navigation + identity) and the aggregated career stat map
/// keyed by display label. `values[label]` is nil when the stat is
/// undefined for this player (e.g. a rate with a zero denominator).
struct ComparePlayer: Identifiable {
    let result: PlayerSearchResult
    let isPitcher: Bool
    let values: [String: Double?]

    var id: Int { result.player_id }
    var name: String { result.name }
    var teamCode: String? { result.teamCode }
}

// MARK: - View model

@MainActor
final class PlayerCompareViewModel: ObservableObject {
    @Published var players: [ComparePlayer] = []
    @Published var comparisonType: ComparisonType?
    @Published var mode: ComparisonMode = .career
    @Published var isAdding = false
    /// Surfaced when a picked player can't join the locked comparison
    /// type (e.g. a pure pitcher added to a batter comparison).
    @Published var addError: String?

    private let api: APIClient
    static let maxPlayers = 4

    init(api: APIClient = .shared) {
        self.api = api
    }

    var canAddMore: Bool { players.count < Self.maxPlayers }
    var hasComparison: Bool { players.count >= 2 }

    /// Stat rows for the current comparison type (batting vs pitching).
    fileprivate var stats: [CompareStat] {
        (comparisonType?.isPitcher ?? false) ? Self.pitchingStats : Self.battingStats
    }

    func remove(_ player: ComparePlayer) {
        players.removeAll { $0.id == player.id }
        if players.isEmpty { comparisonType = nil }
    }

    /// Add a picked search result. The first player infers and locks the
    /// comparison type; later players must match it (two-way players
    /// match either). Rejects with `addError` when there's no career of
    /// the required type.
    func add(_ result: PlayerSearchResult) async {
        guard canAddMore else { return }
        guard !players.contains(where: { $0.id == result.player_id }) else { return }

        isAdding = true
        defer { isAdding = false }

        // Candidate types to try, in order. For a locked comparison only
        // the locked type is valid; for the first player we try the
        // position-inferred primary first, then fall back to the other
        // side (covers mis-labelled positions and two-way players).
        let candidates: [ComparisonType]
        if let locked = comparisonType {
            candidates = [locked]
        } else {
            let primary = Self.inferPrimaryType(result)
            candidates = [primary, primary == .batter ? .pitcher : .batter]
        }

        for type in candidates {
            if let player = await build(result: result, type: type) {
                if comparisonType == nil { comparisonType = type }
                players.append(player)
                return
            }
            // For a locked comparison, never silently switch sides.
            if comparisonType != nil { break }
        }

        let noun = (comparisonType ?? .batter).noun
        addError = "\(result.name) has no \(noun) career stats to compare."
    }

    // MARK: Build + aggregate

    /// Fetch the career matching `type`, aggregate it, and validate it
    /// carries meaningful data. Returns nil when the player has no usable
    /// career of that type (the mismatch signal).
    private func build(result: PlayerSearchResult, type: ComparisonType) async -> ComparePlayer? {
        switch type {
        case .batter:
            guard let career = try? await api.getPlayerCareerStats(playerId: result.player_id),
                  let seasons = career.seasons, !seasons.isEmpty else { return nil }
            let values = Self.aggregateBatting(seasons)
            // Require real plate work so a pure pitcher's empty batting
            // line doesn't masquerade as a batter.
            guard (values["AB"].flatMap { $0 } ?? 0) >= 1 else { return nil }
            return ComparePlayer(result: result, isPitcher: false, values: values)

        case .pitcher:
            guard let career = try? await api.getPitcherCareerStats(playerId: result.player_id),
                  let seasons = career.seasons, !seasons.isEmpty else { return nil }
            let values = Self.aggregatePitching(seasons)
            guard (values["IP"].flatMap { $0 } ?? 0) >= 1 else { return nil }
            return ComparePlayer(result: result, isPitcher: true, values: values)
        }
    }

    /// Position-based first guess at a player's primary type. Two-way and
    /// mis-labelled cases are caught by the fallback in `add`.
    private static func inferPrimaryType(_ result: PlayerSearchResult) -> ComparisonType {
        if result.is_pitcher == true { return .pitcher }
        let pitcherPositions: Set<String> = ["P", "SP", "RP", "CL", "CP", "RHP", "LHP"]
        if let pos = result.position?.uppercased(), pitcherPositions.contains(pos) {
            return .pitcher
        }
        return .batter
    }

    private static func aggregateBatting(_ seasons: [CareerSeason]) -> [String: Double?] {
        func sumInt(_ key: (CareerSeason) -> Int?) -> Double {
            seasons.reduce(0.0) { $0 + Double(key($1) ?? 0) }
        }
        let g   = sumInt { $0.G }
        let pa  = sumInt { $0.PA }
        let ab  = sumInt { $0.AB }
        let h   = sumInt { $0.H }
        let dbl = sumInt { $0.doubles }
        let trp = sumInt { $0.triples }
        let hr  = sumInt { $0.HR }
        let rbi = sumInt { $0.RBI }
        let sb  = sumInt { $0.SB }
        let bb  = sumInt { $0.BB }
        let so  = sumInt { $0.SO }
        let hbp = sumInt { $0.HBP }
        let sf  = sumInt { $0.SF }
        // Total bases: stored when present, else derived (H + 2B + 2·3B + 3·HR).
        let tb = seasons.reduce(0.0) { acc, s in
            if let t = s.TB { return acc + Double(t) }
            let sH = Double(s.H ?? 0), s2 = Double(s.doubles ?? 0)
            let s3 = Double(s.triples ?? 0), sHR = Double(s.HR ?? 0)
            return acc + (sH + s2 + 2 * s3 + 3 * sHR)
        }
        let war = seasons.reduce(0.0) { $0 + ($1.WAR ?? 0) }

        let avg: Double? = ab > 0 ? h / ab : nil
        let obpDen = ab + bb + hbp + sf
        let obp: Double? = obpDen > 0 ? (h + bb + hbp) / obpDen : nil
        let slg: Double? = ab > 0 ? tb / ab : nil
        let ops: Double? = (obp != nil && slg != nil) ? obp! + slg! : nil

        return [
            "WAR": war, "G": g, "PA": pa, "AB": ab, "H": h,
            "2B": dbl, "3B": trp, "HR": hr, "RBI": rbi, "SB": sb,
            "BB": bb, "SO": so,
            "AVG": avg, "OBP": obp, "SLG": slg, "OPS": ops,
        ]
    }

    private static func aggregatePitching(_ seasons: [PitcherCareerSeason]) -> [String: Double?] {
        func sumInt(_ key: (PitcherCareerSeason) -> Int?) -> Double {
            seasons.reduce(0.0) { $0 + Double(key($1) ?? 0) }
        }
        let w  = sumInt { $0.W }
        let l  = sumInt { $0.L }
        let g  = sumInt { $0.G }
        let gs = sumInt { $0.GS }
        let sv = sumInt { $0.SV }
        let h  = sumInt { $0.H }
        let so = sumInt { $0.SO }
        let bb = sumInt { $0.BB }
        let er = sumInt { $0.ER }
        // Innings: summed as the app does elsewhere (raw decimal), so the
        // ERA/WHIP math matches WindowSnapshot.computePitching.
        let ip = seasons.reduce(0.0) { $0 + ($1.IP ?? 0) }
        let war = seasons.reduce(0.0) { $0 + ($1.WAR ?? 0) }

        let era: Double? = ip > 0 ? er * 9 / ip : nil
        let whip: Double? = ip > 0 ? (h + bb) / ip : nil

        return [
            "WAR": war, "W": w, "L": l, "ERA": era, "G": g,
            "GS": gs, "SV": sv, "IP": ip, "H": h, "SO": so,
            "BB": bb, "WHIP": whip,
        ]
    }

    // MARK: Stat row definitions

    private static let battingStats: [CompareStat] = [
        .init(label: "WAR", format: .dec1),
        .init(label: "G",   format: .int),
        .init(label: "PA",  format: .int),
        .init(label: "AB",  format: .int),
        .init(label: "H",   format: .int),
        .init(label: "2B",  format: .int),
        .init(label: "3B",  format: .int),
        .init(label: "HR",  format: .int),
        .init(label: "RBI", format: .int),
        .init(label: "SB",  format: .int),
        .init(label: "BB",  format: .int),
        .init(label: "SO",  format: .int),
        .init(label: "AVG", format: .rate3),
        .init(label: "OBP", format: .rate3),
        .init(label: "SLG", format: .rate3),
        .init(label: "OPS", format: .rate3),
    ]

    private static let pitchingStats: [CompareStat] = [
        .init(label: "WAR",  format: .dec1),
        .init(label: "W",    format: .int),
        .init(label: "L",    format: .int),
        .init(label: "ERA",  format: .dec2),
        .init(label: "G",    format: .int),
        .init(label: "GS",   format: .int),
        .init(label: "SV",   format: .int),
        .init(label: "IP",   format: .dec1),
        .init(label: "H",    format: .int),
        .init(label: "SO",   format: .int),
        .init(label: "BB",   format: .int),
        .init(label: "WHIP", format: .dec2),
    ]
}

// MARK: - Layout constants

private enum CompareLayout {
    static let labelWidth:   CGFloat = 58
    static let columnWidth:  CGFloat = 116
    static let headerHeight: CGFloat = 76
    static let rowHeight:    CGFloat = 34
}

// MARK: - Main view

struct PlayerCompareView: View {
    @StateObject private var vm = PlayerCompareViewModel()
    @State private var showingSearch = false
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 16) {
                    modePicker
                    if vm.players.isEmpty {
                        emptyPrompt
                    } else {
                        comparisonCard
                        if !vm.hasComparison {
                            Text("Add at least one more \(typeNoun) to compare.")
                                .font(.footnote)
                                .foregroundStyle(.secondary)
                        }
                    }
                    addPlayerButton
                }
                .padding(16)
            }
            .navigationTitle("Compare")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { dismiss() }
                }
            }
            .navigationDestination(for: PlayerSearchResult.self) { player in
                PlayerProfileView(player: player)
            }
            .sheet(isPresented: $showingSearch) {
                ComparePlayerSearchSheet { picked in
                    Task { await vm.add(picked) }
                }
            }
            .overlay {
                if vm.isAdding {
                    ProgressView()
                        .controlSize(.large)
                        .padding(24)
                        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 16))
                }
            }
            .alert(
                "Can't add player",
                isPresented: Binding(
                    get: { vm.addError != nil },
                    set: { if !$0 { vm.addError = nil } }
                )
            ) {
                Button("OK", role: .cancel) { vm.addError = nil }
            } message: {
                Text(vm.addError ?? "")
            }
        }
    }

    private var typeNoun: String {
        (vm.comparisonType?.isPitcher ?? false) ? "pitcher" : "batter"
    }

    // MARK: Mode picker (Career functional; rest stubbed)

    private var modePicker: some View {
        VStack(spacing: 6) {
            Picker("Mode", selection: $vm.mode) {
                ForEach(ComparisonMode.allCases) { mode in
                    Text(mode.rawValue).tag(mode)
                }
            }
            .pickerStyle(.segmented)
            if !vm.mode.isAvailable {
                Text("\(vm.mode.rawValue) mode is coming soon — showing Career totals.")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
    }

    // MARK: Empty prompt

    private var emptyPrompt: some View {
        VStack(spacing: 10) {
            Image(systemName: "person.2.crop.square.stack")
                .font(.system(size: 40))
                .foregroundStyle(.secondary)
            Text("Compare Players")
                .font(.headline)
            Text("Add 2–4 players of the same type (all batters or all pitchers) to compare their careers.")
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .padding(.horizontal, 24)
        }
        .frame(maxWidth: .infinity, minHeight: 200)
    }

    // MARK: Add-player button

    @ViewBuilder
    private var addPlayerButton: some View {
        if vm.canAddMore {
            Button {
                showingSearch = true
            } label: {
                Label("Add Player", systemImage: "plus.circle.fill")
                    .font(.subheadline.weight(.semibold))
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
                    .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 14))
            }
            .buttonStyle(.plain)
        }
    }

    // MARK: Comparison table

    private var comparisonCard: some View {
        HStack(spacing: 0) {
            // Frozen stat-label column.
            VStack(spacing: 0) {
                Color.clear.frame(height: CompareLayout.headerHeight)
                ForEach(vm.stats) { stat in
                    Text(stat.label)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                        .frame(width: CompareLayout.labelWidth, height: CompareLayout.rowHeight, alignment: .leading)
                        .padding(.leading, 12)
                }
            }
            .frame(width: CompareLayout.labelWidth + 12)
            .background(.ultraThinMaterial)
            .zIndex(1)

            // Player columns — scroll horizontally when 3–4 players
            // overflow the screen.
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 0) {
                    ForEach(vm.players) { player in
                        playerColumn(player)
                        if player.id != vm.players.last?.id {
                            Divider()
                        }
                    }
                }
            }
        }
        .background(.ultraThinMaterial)
        .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
        .shadow(color: .black.opacity(0.06), radius: 8, y: 3)
    }

    private func playerColumn(_ player: ComparePlayer) -> some View {
        VStack(spacing: 0) {
            playerHeader(player)
            ForEach(vm.stats) { stat in
                statCell(player: player, stat: stat)
            }
        }
        .frame(width: CompareLayout.columnWidth)
    }

    private func playerHeader(_ player: ComparePlayer) -> some View {
        let tint = TeamColors.color(for: player.teamCode) ?? .accentColor
        return ZStack(alignment: .topTrailing) {
            NavigationLink(value: player.result) {
                VStack(spacing: 4) {
                    Text(player.name)
                        .font(.subheadline.weight(.bold))
                        .lineLimit(2)
                        .minimumScaleFactor(0.7)
                        .multilineTextAlignment(.center)
                        .foregroundStyle(.primary)
                    Text(teamLabel(player.teamCode))
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(tint)
                }
                .frame(maxWidth: .infinity)
                .frame(height: CompareLayout.headerHeight)
                .padding(.horizontal, 6)
                .background(tint.opacity(0.16))
                .overlay(alignment: .bottom) {
                    Rectangle().fill(tint).frame(height: 2)
                }
            }
            .buttonStyle(.plain)

            Button {
                vm.remove(player)
            } label: {
                Image(systemName: "xmark.circle.fill")
                    .font(.system(size: 16))
                    .foregroundStyle(.secondary)
                    .padding(4)
            }
            .buttonStyle(.plain)
        }
    }

    private func statCell(player: ComparePlayer, stat: CompareStat) -> some View {
        let isPitcher = vm.comparisonType?.isPitcher ?? false
        let best = isBest(player: player, stat: stat, isPitcher: isPitcher)
        return Text(formatted(player.values[stat.label] ?? nil, format: stat.format))
            .font(.subheadline.weight(best ? .bold : .regular))
            .monospacedDigit()
            .foregroundStyle(.primary)
            .frame(width: CompareLayout.columnWidth, height: CompareLayout.rowHeight)
            .background(best ? compareBestTint : Color.clear)
    }

    /// Gold best-value tint — readable in both light and dark mode and
    /// independent of any one team's color.
    private var compareBestTint: Color {
        Color(red: 0.85, green: 0.65, blue: 0.13).opacity(0.22)
    }

    // MARK: Best-value resolution

    /// True when `player` holds the best value in this stat row. Ties
    /// highlight every tied player. Players missing the stat are ignored
    /// and never win.
    private func isBest(player: ComparePlayer, stat: CompareStat, isPitcher: Bool) -> Bool {
        let values: [Double] = vm.players.compactMap { $0.values[stat.label] ?? nil }
        guard values.count >= 2 else { return false }
        guard let mine = player.values[stat.label] ?? nil else { return false }
        let direction = StatDirection.direction(for: stat.label, isPitcher: isPitcher)
        let best = direction == .higherBetter ? values.max() : values.min()
        guard let best else { return false }
        return abs(mine - best) < 0.0000001
    }

    // MARK: Formatting

    private func formatted(_ value: Double?, format: StatFormat) -> String {
        guard let value else { return "—" }
        switch format {
        case .int:
            return String(Int(value.rounded()))
        case .dec1:
            return String(format: "%.1f", value)
        case .dec2:
            return String(format: "%.2f", value)
        case .rate3:
            let s = String(format: "%.3f", value)
            if s.hasPrefix("0.")  { return String(s.dropFirst()) }
            if s.hasPrefix("-0.") { return "-" + String(s.dropFirst(2)) }
            return s
        }
    }

    private func teamLabel(_ code: String?) -> String {
        guard let code, !code.isEmpty else { return "—" }
        return teamAbbreviation(for: code)
    }
}

// MARK: - Add-player search sheet

/// Lightweight player search reused for picking a player to add. Shares
/// `SearchViewModel`'s debounce + `searchPlayers` and the existing row.
private struct ComparePlayerSearchSheet: View {
    let onSelect: (PlayerSearchResult) -> Void

    @StateObject private var vm = SearchViewModel()
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Group {
                if vm.isLoading && vm.results.isEmpty {
                    ProgressView().controlSize(.large)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                } else if vm.results.isEmpty {
                    ContentUnavailableView(
                        "Search Players",
                        systemImage: "magnifyingglass",
                        description: Text("Type a name to find a player to add.")
                    )
                } else {
                    List(vm.results) { player in
                        Button {
                            onSelect(player)
                            dismiss()
                        } label: {
                            PlayerSearchResultRow(player: player)
                        }
                        .buttonStyle(.plain)
                    }
                    .listStyle(.insetGrouped)
                }
            }
            .navigationTitle("Add Player")
            .navigationBarTitleDisplayMode(.inline)
            .searchable(
                text: $vm.searchText,
                placement: .navigationBarDrawer(displayMode: .always),
                prompt: "Search players"
            )
            .textInputAutocapitalization(.words)
            .autocorrectionDisabled()
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Cancel") { dismiss() }
                }
            }
        }
    }
}

#Preview {
    PlayerCompareView()
}
