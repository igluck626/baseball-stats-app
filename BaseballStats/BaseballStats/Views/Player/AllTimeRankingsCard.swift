//
//  AllTimeRankingsCard.swift
//  BaseballStats
//
//  Career-tab card surfacing where the player ranks in MLB's all-time
//  career leaderboards on a fixed set of headline stats. Sister
//  component to LeagueRankingsCard, but career-scoped:
//    • Calls /leaderboards?mode=career (no league filter)
//    • Probes the top 100 instead of the top 25 — career records span
//      150 years of baseball, so top-100 is the meaningful cohort
//    • Renders a tiered rank badge (gold 1–10, blue 11–25, neutral
//      26–100) to highlight headline finishes at a glance
//
//  Card hides itself when zero stats qualify, same defensive pattern
//  as LeagueRankingsCard — including the ZStack + Color.clear
//  sentinel so .task always fires on first mount even when the
//  conditional body collapses to empty.
//

import Combine
import SwiftUI

// MARK: - Model

/// One career-ranking row for display. No league field — career
/// rankings are MLB-wide.
struct AllTimeRanking: Identifiable, Hashable {
    let stat: String   // "WAR", "HR", "AVG", "ERA", "IP", …
    let rank: Int      // 1...100
    let value: Double?

    var id: String { stat }
}

// MARK: - ViewModel

@MainActor
final class AllTimeRankingsViewModel: ObservableObject {
    let playerId: Int
    let isPitcher: Bool

    @Published var rankings: [AllTimeRanking] = []
    @Published var isLoading = false
    @Published var error: String?
    /// True once the load has completed at least once. The view uses
    /// this to distinguish "still spinning, render nothing yet"
    /// from "finished and found nothing — collapse the card."
    @Published var didLoad = false

    private let api: APIClient

    /// Career stat catalogs in display order. Tighter list than the
    /// season-mode catalog: every entry is its own /leaderboards
    /// call (~500ms server-side per stat in career mode), so trimming
    /// to the most-meaningful stats keeps the card from hanging at
    /// load. Dropped from prior revision: SB / OBP / SLG (batter),
    /// WHIP / SV (pitcher) — still surfaced on the season-mode
    /// Leaderboards screen for users who want them.
    static let battingStats:  [String] = [
        "WAR", "HR", "H", "RBI", "BB", "AVG", "OPS",
    ]
    static let pitchingStats: [String] = [
        "WAR", "SO", "W", "ERA", "IP",
    ]

    init(playerId: Int, isPitcher: Bool, api: APIClient = .shared) {
        self.playerId = playerId
        self.isPitcher = isPitcher
        self.api = api
    }

    /// Fan out one /leaderboards?mode=career call per stat (limit=100,
    /// no league filter) in parallel; keep every stat where this
    /// player appears in the response. The catalog order is preserved
    /// in the output regardless of which task finishes first.
    func load() async {
        isLoading = true
        error = nil

        let stats = isPitcher ? Self.pitchingStats : Self.battingStats
        let playerType = isPitcher ? "pitcher" : "batter"

        let results: [AllTimeRanking] = await withTaskGroup(of: AllTimeRanking?.self) { group in
            for stat in stats {
                group.addTask { [api, playerId] in
                    do {
                        let response = try await api.getLeaderboard(
                            stat:       stat,
                            year:       nil,
                            playerType: playerType,
                            mode:       "career",
                            league:     nil,
                            team:       nil,
                            limit:      100
                        )
                        guard let leaders = response?.leaders else { return nil }
                        guard let hit = leaders.first(where: { $0.player.player_id == playerId })
                        else { return nil }
                        return AllTimeRanking(
                            stat: stat,
                            rank: hit.rank,
                            value: hit.value
                        )
                    } catch {
                        // Per-stat failure shouldn't tank the whole
                        // card — just omit the stat from the result
                        // and keep whatever else landed.
                        return nil
                    }
                }
            }
            var hits: [String: AllTimeRanking] = [:]
            for await maybe in group {
                if let r = maybe { hits[r.stat] = r }
            }
            return stats.compactMap { hits[$0] }
        }

        rankings = results
        didLoad = true
        isLoading = false
    }
}

// MARK: - View

/// Glass card listing the player's all-time career rankings. Renders
/// nothing until the first load completes; afterward, hidden iff
/// zero stats qualified.
struct AllTimeRankingsCard: View {
    @StateObject private var vm: AllTimeRankingsViewModel
    /// Used by row taps to push the user into the Leaderboards tab
    /// with mode=career + the tapped stat preselected.
    @EnvironmentObject private var navigation: AppNavigation

    init(playerId: Int, isPitcher: Bool) {
        _vm = StateObject(wrappedValue: AllTimeRankingsViewModel(
            playerId: playerId, isPitcher: isPitcher
        ))
    }

    var body: some View {
        // ZStack + zero-height Color.clear sentinel — anchors the
        // view in the hierarchy even when both conditional branches
        // resolve to empty (initial state: isLoading=false,
        // didLoad=false, rankings=[]). SwiftUI strips lifecycle
        // modifiers like .task from a body that collapses to
        // EmptyView, so without the sentinel `load()` would never
        // fire on first mount.
        ZStack {
            Color.clear.frame(height: 0)
            if vm.isLoading && !vm.didLoad {
                loadingBody
            } else if !vm.rankings.isEmpty {
                loadedBody
            }
            // didLoad && rankings.isEmpty → only the sentinel
            // renders; the card collapses and the parent layout
            // closes the gap.
        }
        .task { await vm.load() }
    }

    private var loadingBody: some View {
        VStack(spacing: 10) {
            HStack {
                Text("All-Time Rankings").font(.headline)
                Spacer()
                ProgressView().controlSize(.small)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .frame(maxWidth: .infinity)
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 20))
        .shadow(color: .black.opacity(0.06), radius: 8, x: 0, y: 2)
    }

    private var loadedBody: some View {
        VStack(spacing: 0) {
            HStack {
                Text("All-Time Rankings").font(.headline)
                Spacer()
            }
            .padding(.horizontal, 14)
            .padding(.top, 12)
            .padding(.bottom, 8)

            VStack(spacing: 0) {
                ForEach(Array(vm.rankings.enumerated()), id: \.element.id) { idx, ranking in
                    rankingRow(ranking)
                    if idx != vm.rankings.indices.last {
                        Divider().opacity(0.4)
                    }
                }
            }
        }
        .frame(maxWidth: .infinity)
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 20))
        .shadow(color: .black.opacity(0.06), radius: 8, x: 0, y: 2)
    }

    private func rankingRow(_ r: AllTimeRanking) -> some View {
        Button {
            // Push to the Leaderboards tab with the tapped stat
            // preselected in career mode. Same player kind as this
            // card — the user is viewing a batter's batting career
            // card or a pitcher's pitching career card.
            navigation.openLeaderboard(
                mode: .career,
                playerKind: vm.isPitcher ? .pitcher : .batter,
                stat: r.stat
            )
        } label: {
            HStack(spacing: 12) {
                rankBadge(rank: r.rank)
                Text(verbatim: "All-Time · \(r.stat)")
                    .font(.subheadline)
                    .lineLimit(1)
                    .foregroundStyle(.primary)
                Spacer()
                Text(formatAllTimeValue(stat: r.stat, value: r.value))
                    .font(.subheadline.weight(.semibold))
                    .monospacedDigit()
                    .foregroundStyle(.primary)
                Image(systemName: "chevron.right")
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(.tertiary)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 10)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    /// Tiered rank badge. Gold for an all-time top-10 finish (the
    /// rarefied air), blue for 11–25 (clearly elite), neutral chip
    /// for 26–100 (notable but not headline). Wider than the
    /// LeagueRankings badge to comfortably hold a 3-digit "100".
    private func rankBadge(rank: Int) -> some View {
        let style = Self.rankBadgeStyle(for: rank)
        return Text("\(rank)")
            .font(.caption.weight(.bold))
            .monospacedDigit()
            .foregroundStyle(style.foreground)
            .frame(width: 30, height: 30)
            .background(Circle().fill(style.background))
    }

    private struct BadgeStyle {
        let background: Color
        let foreground: Color
    }

    /// Muted gold that reads well on both light + dark mode without
    /// going full yellow (which clashes with the existing accent
    /// blue and is harder to read with white text).
    private static let goldColor = Color(red: 0.85, green: 0.65, blue: 0.13)

    private static func rankBadgeStyle(for rank: Int) -> BadgeStyle {
        switch rank {
        case ...10:
            return BadgeStyle(background: goldColor, foreground: .white)
        case 11...25:
            return BadgeStyle(background: .blue,     foreground: .white)
        default:
            return BadgeStyle(background: Color(.secondarySystemFill),
                              foreground: .primary)
        }
    }
}

// MARK: - Formatters

/// Per-stat value formatting for the career rankings.
///   • WAR — one decimal (162.8). Career WAR caps at ~183 so no
///     thousands separator needed.
///   • ERA / WHIP — two decimals (1.82). Same scale → no separator.
///   • AVG / OBP / SLG / OPS — three decimals, Baseball-Reference
///     style with the leading zero stripped (.342).
///   • IP — one decimal with locale separator ("7,356.0") for Cy
///     Young types whose career IP runs into five figures.
///   • Counting stats — integers with locale separator ("4,256" for
///     Pete Rose's hits, "762" for Bonds' HRs).
private func formatAllTimeValue(stat: String, value: Double?) -> String {
    guard let value else { return "—" }
    switch stat {
    case "WAR":
        return String(format: "%.1f", value)
    case "ERA", "WHIP":
        return String(format: "%.2f", value)
    case "AVG", "OBP", "SLG", "OPS":
        let s = String(format: "%.3f", value)
        if s.hasPrefix("0.")  { return String(s.dropFirst()) }
        if s.hasPrefix("-0.") { return "-" + String(s.dropFirst(2)) }
        return s
    case "IP":
        // .formatted respects the user's locale — comma in en-US,
        // space in fr-FR, etc. Pinned to one fractional digit so
        // the .0/.1/.2 partial-inning notation survives.
        return value.formatted(.number.precision(.fractionLength(1)))
    default:
        // HR / H / RBI / SB / BB / SO / W / SV — integer counting
        // stats. `.formatted(.number)` adds the locale's thousands
        // separator above 999. Round-trip through Int first so we
        // never emit a misleading decimal like "762.0".
        return Int(value.rounded()).formatted(.number)
    }
}
