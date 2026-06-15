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
//  Phase 2 adds Year Range and Age Range modes — each player's stats are
//  re-aggregated from the subset of seasons inside the selected range, so
//  ComparePlayer stores the raw season rows (plus birth year for Age) and
//  the table values are computed per mode/range.
//
//  Phase 3 adds Per-162 mode — full-career counting stats normalized to a
//  single-season pace (batters to 162 games, pitchers to 200 IP), so it
//  reads as "per full season." Rate stats pass through unscaled.
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
    /// True for the modes that need a From/To range control.
    var usesRange: Bool { self == .yearRange || self == .ageRange }
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
/// profile navigation + identity) plus the raw per-season rows for the
/// comparison side — range modes re-aggregate a filtered subset of these,
/// so the stats are computed on demand rather than stored.
struct ComparePlayer: Identifiable {
    let result: PlayerSearchResult
    let isPitcher: Bool
    /// Full season rows for the comparison side; the other array is empty.
    let battingSeasons: [CareerSeason]
    let pitchingSeasons: [PitcherCareerSeason]
    /// From the career bio (falls back to the search result). nil → Age
    /// Range mode can't place this player and the column shows "—".
    let birthYear: Int?

    var id: Int { result.player_id }
    var name: String { result.name }
    var teamCode: String? { result.teamCode }

    /// All dated season years for this player (both sides, though only one
    /// is populated). Used for span/overlap math.
    var seasonYears: [Int] {
        battingSeasons.compactMap(\.year) + pitchingSeasons.compactMap(\.year)
    }

    /// First...last season year; nil when no season carries a year.
    var yearSpan: ClosedRange<Int>? {
        guard let lo = seasonYears.min(), let hi = seasonYears.max() else { return nil }
        return lo...hi
    }
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

    /// Active From/To selections for the range modes. Reset to sensible
    /// defaults whenever the roster changes (`recomputeRangeDefaults`).
    @Published var yearFrom: Int = 0
    @Published var yearTo: Int = 0
    @Published var ageFrom: Int = 20
    @Published var ageTo: Int = 30

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

    // MARK: Range bounds + defaults

    /// Years present across all players (min...max); nil before any data.
    var yearBounds: ClosedRange<Int>? {
        let years = players.flatMap(\.seasonYears)
        guard let lo = years.min(), let hi = years.max() else { return nil }
        return lo...hi
    }

    /// Ages present across players that carry a birth year (min...max).
    var ageBounds: ClosedRange<Int>? {
        var ages: [Int] = []
        for p in players {
            guard let by = p.birthYear else { continue }
            ages.append(contentsOf: p.seasonYears.map { $0 - by })
        }
        guard let lo = ages.min(), let hi = ages.max() else { return nil }
        return lo...hi
    }

    /// True when Age Range can't place at least one player (no birth year).
    var hasMissingBirthYear: Bool { players.contains { $0.birthYear == nil } }

    /// Reset the From/To selections for the current roster. Year defaults
    /// to the overlap of every player's span (the years they all share),
    /// falling back to the full span when there's no overlap. Age defaults
    /// to 20–30 clamped into the available age range.
    func recomputeRangeDefaults() {
        if let yb = yearBounds {
            let starts = players.compactMap { $0.yearSpan?.lowerBound }
            let ends   = players.compactMap { $0.yearSpan?.upperBound }
            if let latestStart = starts.max(), let earliestEnd = ends.min(),
               latestStart <= earliestEnd {
                yearFrom = latestStart; yearTo = earliestEnd
            } else {
                yearFrom = yb.lowerBound; yearTo = yb.upperBound
            }
        }
        if let ab = ageBounds {
            ageFrom = min(max(20, ab.lowerBound), ab.upperBound)
            ageTo   = max(min(30, ab.upperBound), ab.lowerBound)
            if ageFrom > ageTo { ageFrom = ab.lowerBound; ageTo = ab.upperBound }
        }
    }

    func remove(_ player: ComparePlayer) {
        players.removeAll { $0.id == player.id }
        if players.isEmpty { comparisonType = nil }
        recomputeRangeDefaults()
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
                recomputeRangeDefaults()
                return
            }
            // For a locked comparison, never silently switch sides.
            if comparisonType != nil { break }
        }

        let noun = (comparisonType ?? .batter).noun
        addError = "\(result.name) has no \(noun) career stats to compare."
    }

    // MARK: Display values (mode + range aware)

    /// Per-player stat maps for the current mode/range, keyed by player id.
    /// An empty map (every stat renders "—") means the player has no
    /// seasons in the active range (or no birth year in Age Range).
    fileprivate func displayValues() -> [Int: [String: Double?]] {
        var out: [Int: [String: Double?]] = [:]
        for p in players { out[p.id] = values(for: p) }
        return out
    }

    private func values(for player: ComparePlayer) -> [String: Double?] {
        let base: [String: Double?]
        if player.isPitcher {
            let seasons = player.pitchingSeasons.filter {
                inRange(year: $0.year, birthYear: player.birthYear)
            }
            guard !seasons.isEmpty else { return [:] }
            base = Self.aggregatePitching(seasons)
        } else {
            let seasons = player.battingSeasons.filter {
                inRange(year: $0.year, birthYear: player.birthYear)
            }
            guard !seasons.isEmpty else { return [:] }
            base = Self.aggregateBatting(seasons)
        }
        // Per-162 normalizes the (full-career, since inRange admits every
        // season for this mode) totals to a single-season pace.
        if mode == .per162 {
            return Self.normalizedPer162(base, isPitcher: player.isPitcher)
        }
        return base
    }

    /// Counting stats that scale with playing time (everything but the
    /// slash-line / ERA / WHIP rates). WAR is included — it's a counting
    /// stat, so a per-season WAR pace is exactly the point of the mode.
    private static let scalableBatting: Set<String> =
        ["WAR", "G", "PA", "AB", "H", "2B", "3B", "HR", "RBI", "SB", "BB", "SO"]
    private static let scalablePitching: Set<String> =
        ["WAR", "W", "L", "G", "GS", "SV", "IP", "H", "SO", "BB"]

    /// Scale counting stats to a full single-season workload — batters to
    /// 162 games, pitchers to 200 IP (162 team games don't map to a
    /// pitcher's appearances, least of all a reliever's). Rate stats
    /// (AVG/OBP/SLG/OPS, ERA/WHIP) pass through unscaled. The divisor (G or
    /// IP) lands exactly on the target after scaling. Returns all "—" when
    /// the divisor is zero.
    private static func normalizedPer162(
        _ base: [String: Double?],
        isPitcher: Bool,
    ) -> [String: Double?] {
        let divisorKey = isPitcher ? "IP" : "G"
        let target: Double = isPitcher ? 200 : 162
        guard let denom = base[divisorKey].flatMap({ $0 }), denom > 0 else { return [:] }
        let factor = target / denom
        let scalable = isPitcher ? scalablePitching : scalableBatting
        var out = base
        for (key, value) in base where scalable.contains(key) {
            if let value { out[key] = value * factor }
        }
        return out
    }

    /// Whether a season counts under the active mode/range. Career and
    /// Per-162 (which normalizes full-career totals) include everything.
    private func inRange(year: Int?, birthYear: Int?) -> Bool {
        guard let year else { return false }
        switch mode {
        case .career, .per162:
            return true
        case .yearRange:
            return year >= yearFrom && year <= yearTo
        case .ageRange:
            guard let birthYear else { return false }
            let age = year - birthYear
            return age >= ageFrom && age <= ageTo
        }
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
            // Require real plate work so a pure pitcher's empty batting
            // line doesn't masquerade as a batter.
            guard (Self.aggregateBatting(seasons)["AB"].flatMap { $0 } ?? 0) >= 1 else { return nil }
            return ComparePlayer(
                result: result, isPitcher: false,
                battingSeasons: seasons, pitchingSeasons: [],
                birthYear: career.bio?.birth_year ?? result.birth_year,
            )

        case .pitcher:
            guard let career = try? await api.getPitcherCareerStats(playerId: result.player_id),
                  let seasons = career.seasons, !seasons.isEmpty else { return nil }
            guard (Self.aggregatePitching(seasons)["IP"].flatMap { $0 } ?? 0) >= 1 else { return nil }
            return ComparePlayer(
                result: result, isPitcher: true,
                battingSeasons: [], pitchingSeasons: seasons,
                birthYear: career.bio?.birth_year ?? result.birth_year,
            )
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
        VStack(spacing: 8) {
            Picker("Mode", selection: $vm.mode) {
                ForEach(ComparisonMode.allCases) { mode in
                    Text(mode.rawValue).tag(mode)
                }
            }
            .pickerStyle(.segmented)
            if vm.mode.usesRange && !vm.players.isEmpty {
                rangeControls
            }
            if let subtitle = activeRangeSubtitle {
                Text(subtitle)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
            }
        }
    }

    // MARK: Range controls

    @ViewBuilder
    private var rangeControls: some View {
        switch vm.mode {
        case .yearRange:
            if let bounds = vm.yearBounds {
                let years = Array(bounds)
                HStack(spacing: 12) {
                    Picker("From", selection: $vm.yearFrom) {
                        ForEach(years, id: \.self) { Text(String($0)).tag($0) }
                    }
                    .pickerStyle(.menu)
                    Image(systemName: "arrow.right")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Picker("To", selection: $vm.yearTo) {
                        ForEach(years, id: \.self) { Text(String($0)).tag($0) }
                    }
                    .pickerStyle(.menu)
                }
                // Keep To ≥ From in both directions.
                .onChange(of: vm.yearFrom) { _, new in if vm.yearTo < new { vm.yearTo = new } }
                .onChange(of: vm.yearTo) { _, new in if vm.yearFrom > new { vm.yearFrom = new } }
            }
        case .ageRange:
            if let bounds = vm.ageBounds {
                VStack(spacing: 6) {
                    HStack(spacing: 16) {
                        Stepper("From \(vm.ageFrom)", value: $vm.ageFrom, in: bounds)
                        Stepper("To \(vm.ageTo)", value: $vm.ageTo, in: bounds)
                    }
                    .font(.subheadline)
                    if vm.hasMissingBirthYear {
                        Text("Players without a birth year on file show “—” in Age Range.")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }
                .onChange(of: vm.ageFrom) { _, new in if vm.ageTo < new { vm.ageTo = new } }
                .onChange(of: vm.ageTo) { _, new in if vm.ageFrom > new { vm.ageFrom = new } }
            } else {
                Text("No age data available for these players.")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        default:
            EmptyView()
        }
    }

    /// "2018–2023" / "Age 20–25" / "Per 162 Games" describing the active
    /// mode; nil for Career (plain totals, no qualifier).
    private var activeRangeSubtitle: String? {
        switch vm.mode {
        case .career:
            return nil
        case .per162:
            // Pitchers normalize to a 200-IP workload, batters to 162 G.
            return (vm.comparisonType?.isPitcher ?? false) ? "Per 200 IP" : "Per 162 Games"
        case .yearRange:
            guard vm.yearBounds != nil else { return nil }
            return "\(vm.yearFrom)–\(vm.yearTo)"
        case .ageRange:
            guard vm.ageBounds != nil else { return nil }
            return "Age \(vm.ageFrom)–\(vm.ageTo)"
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
        // Compute the mode/range-aware values once per render; shared by
        // every cell and by the best-value resolution.
        let values = vm.displayValues()
        return HStack(spacing: 0) {
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
                        playerColumn(player, values: values)
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

    private func playerColumn(
        _ player: ComparePlayer,
        values: [Int: [String: Double?]],
    ) -> some View {
        VStack(spacing: 0) {
            playerHeader(player)
            ForEach(vm.stats) { stat in
                statCell(player: player, stat: stat, values: values)
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

    private func statCell(
        player: ComparePlayer,
        stat: CompareStat,
        values: [Int: [String: Double?]],
    ) -> some View {
        let isPitcher = vm.comparisonType?.isPitcher ?? false
        let myValue = values[player.id]?[stat.label] ?? nil
        let best = isBest(player: player, stat: stat, isPitcher: isPitcher, values: values)
        return Text(formatted(myValue, format: stat.format))
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
    private func isBest(
        player: ComparePlayer,
        stat: CompareStat,
        isPitcher: Bool,
        values: [Int: [String: Double?]],
    ) -> Bool {
        let all: [Double] = vm.players.compactMap { values[$0.id]?[stat.label] ?? nil }
        guard all.count >= 2 else { return false }
        guard let mine = values[player.id]?[stat.label] ?? nil else { return false }
        let direction = StatDirection.direction(for: stat.label, isPitcher: isPitcher)
        let best = direction == .higherBetter ? all.max() : all.min()
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
