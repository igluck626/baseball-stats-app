//
//  AwardVotingView.swift
//  BaseballStats
//
//  Sheet presented when the user taps an MVP / Cy Young / Rookie of
//  the Year chiclet on a career-table row. Shows the full voting
//  leaderboard for that (award, year, league) triple — ranked list
//  of players with their points won, points max, and first-place
//  votes. Tapping a row drills into that player's profile.
//
//  Lives next to PlayerProfileView since it's only ever presented
//  from there; the parent owns the @State that drives the sheet.
//

import Combine
import SwiftUI

@MainActor
final class AwardVotingViewModel: ObservableObject {
    let destination: AwardVotingDestination
    @Published var response: AwardVotingResponse?
    @Published var isLoading = false
    @Published var error: String?

    private let api: APIClient

    init(destination: AwardVotingDestination, api: APIClient = .shared) {
        self.destination = destination
        self.api = api
    }

    func load() async {
        isLoading = true
        error = nil
        do {
            response = try await api.getAwardVoting(
                award: destination.award,
                year: destination.year,
                league: destination.league
            )
        } catch {
            self.error = error.localizedDescription
            response = nil
        }
        isLoading = false
    }
}

struct AwardVotingView: View {
    @StateObject private var vm: AwardVotingViewModel
    @Environment(\.dismiss) private var dismiss

    init(destination: AwardVotingDestination) {
        _vm = StateObject(wrappedValue: AwardVotingViewModel(destination: destination))
    }

    var body: some View {
        NavigationStack {
            content
                .navigationTitle(navigationTitle)
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button("Done") { dismiss() }
                    }
                }
                .navigationDestination(for: PlayerSearchResult.self) { player in
                    PlayerProfileView(player: player)
                }
        }
        .task { await vm.load() }
    }

    /// "AL MVP · 2023" style — short enough to fit comfortably in
    /// the inline title bar.
    private var navigationTitle: String {
        let award = vm.destination.award
        let lg = vm.destination.league == "ML" ? "" : "\(vm.destination.league) "
        return "\(lg)\(award) · \(vm.destination.year)"
    }

    @ViewBuilder
    private var content: some View {
        if vm.isLoading && vm.response == nil {
            ProgressView()
                .controlSize(.large)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if let error = vm.error, vm.response == nil {
            ContentUnavailableView {
                Label("Couldn't load voting", systemImage: "exclamationmark.triangle")
            } description: {
                Text(error)
            } actions: {
                Button("Try Again") { Task { await vm.load() } }
                    .buttonStyle(.borderedProminent)
            }
        } else if let response = vm.response, !response.entries.isEmpty {
            votingList(response: response)
        } else {
            ContentUnavailableView {
                Label("No voting results", systemImage: "list.number")
            } description: {
                Text("No \(vm.destination.award) voting results recorded for \(vm.destination.league) \(String(vm.destination.year)).")
            }
        }
    }

    private func votingList(response: AwardVotingResponse) -> some View {
        List {
            ForEach(response.entries) { entry in
                ZStack {
                    NavigationLink(value: entry.player) { EmptyView() }
                        .opacity(0)
                    AwardVotingRow(entry: entry)
                }
                .listRowSeparatorTint(Color(.systemGray4))
            }
        }
        .listStyle(.insetGrouped)
        .scrollContentBackground(.hidden)
    }
}

/// Single voting row — rank badge + player name (with team / debut
/// year subtitle) + points-won total + first-place vote count.
/// Visual hierarchy parallels the LeaderboardRow so the two list
/// surfaces feel related.
private struct AwardVotingRow: View {
    let entry: AwardVotingEntry

    var body: some View {
        // .top alignment keeps the rank badge and points column
        // anchored to the top of the name when the subtitle wraps to
        // a second line (Ohtani-style two-way stat rows). Without it
        // they'd float to the vertical center of the taller VStack
        // and the chevron would drift mid-name.
        HStack(alignment: .top, spacing: 10) {
            rankBadge
            VStack(alignment: .leading, spacing: 2) {
                Text(entry.player.name)
                    .font(.headline)
                    .lineLimit(1)
                    .minimumScaleFactor(0.75)
                if let subtitle {
                    Text(subtitle)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        // Allow up to two lines for long stat
                        // strings — pitchers' "2.41 ERA · 16-7 ·
                        // 233 SO · 6.8 WAR" runs past iPhone width
                        // on tight points-column rows, and two-way
                        // players insert a "\n" break themselves
                        // to put batting on line 1 / pitching on
                        // line 2.
                        .lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer()
            VStack(alignment: .trailing, spacing: 2) {
                Text(pointsWonText)
                    .font(.subheadline.weight(.semibold))
                    .monospacedDigit()
                if let votesFirst = entry.votes_first, votesFirst > 0 {
                    Text("\(votesFirst) 1st-place")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .monospacedDigit()
                }
            }
            Image(systemName: "chevron.right")
                .font(.footnote.weight(.semibold))
                .foregroundStyle(.tertiary)
                // Inline the chevron with the player name baseline
                // rather than the top edge of the row.
                .padding(.top, 2)
        }
        // Bumped 4 → 6 so two-line subtitles have a small breathing
        // gap above and below the row.
        .padding(.vertical, 6)
    }

    /// Top-3 ranks get the accent fill (matches the LeagueRankings
    /// card convention); 4+ render as a plain neutral chip.
    private var rankBadge: some View {
        let isTopThree = entry.rank <= 3
        return Text("\(entry.rank)")
            .font(.callout.weight(.bold))
            .monospacedDigit()
            .foregroundStyle(isTopThree ? Color.white : Color.primary)
            .frame(width: 28, height: 28)
            .background(
                Circle().fill(
                    isTopThree
                        ? Color.accentColor
                        : Color(.secondarySystemFill)
                )
            )
    }

    /// Compact season-stat line for the row subtitle. Picks the
    /// right format based on which side(s) of the ball the player
    /// has data on:
    ///   • Batting only  — ".301 · 44 HR · 130 RBI · 8.2 WAR · 132 OPS+"
    ///   • Pitching only — "2.41 ERA · 16-7 · 233 SO · 6.8 WAR · 158 ERA+"
    ///   • Two-way       — slimmed lines (WAR and OPS+/ERA+ both
    ///     dropped) so the row still fits in two lines without
    ///     overflowing. Explicit "\n" between segments so the
    ///     wrap point is deterministic. Cy Young rows are forced
    ///     pitching-only server-side, so no two-way branch fires.
    private var subtitle: String? {
        guard let stats = entry.season_stats else { return nil }
        let batting = stats.batting.flatMap { isMeaningfulBatting($0) ? $0 : nil }
        let pitching = stats.pitching.flatMap { isMeaningfulPitching($0) ? $0 : nil }

        if let b = batting, let p = pitching {
            let batLine = formatBattingLine(b, includeExtended: false)
            let pitLine = formatPitchingLine(p, includeExtended: false)
            switch (batLine, pitLine) {
            case let (.some(bl), .some(pl)): return "\(bl)\n\(pl)"
            case let (.some(bl), .none):     return bl
            case let (.none, .some(pl)):     return pl
            default:                         return nil
            }
        }
        if let b = batting  { return formatBattingLine(b, includeExtended: true) }
        if let p = pitching { return formatPitchingLine(p, includeExtended: true) }
        return nil
    }

    /// `includeExtended` gates WAR and OPS+ — both are dropped on
    /// two-way rows to keep each side's line short enough to fit.
    private func formatBattingLine(
        _ b: SeasonStatsBlock.SeasonBatting,
        includeExtended: Bool
    ) -> String? {
        var pieces: [String?] = [
            formatAvg(b.AVG),
            b.HR.map  { "\($0) HR" },
            b.RBI.map { "\($0) RBI" },
        ]
        if includeExtended {
            pieces.append(formatWar(b.WAR))
            pieces.append(formatOpsPlus(b.OPSplus))
        }
        let s = pieces.compactMap { $0 }.joined(separator: " · ")
        return s.isEmpty ? nil : s
    }

    /// `includeExtended` gates WAR and ERA+ — both are dropped on
    /// two-way rows to keep each side's line short enough to fit.
    private func formatPitchingLine(
        _ p: SeasonStatsBlock.SeasonPitching,
        includeExtended: Bool
    ) -> String? {
        var pieces: [String?] = [
            formatEra(p.ERA),
            formatWL(p.W, p.L),
            p.SO.map { "\($0) SO" },
        ]
        if includeExtended {
            pieces.append(formatWar(p.WAR))
            pieces.append(formatEraPlus(p.ERAplus))
        }
        let s = pieces.compactMap { $0 }.joined(separator: " · ")
        return s.isEmpty ? nil : s
    }

    /// Filters out the single-PH-AB / one-relief-inning rows that
    /// happen when a player has a season row but didn't actually
    /// play that side of the ball meaningfully. Voting subtitles
    /// should reflect a real season, not a token appearance.
    private func isMeaningfulBatting(_ b: SeasonStatsBlock.SeasonBatting) -> Bool {
        (b.PA ?? 0) >= 30
    }

    private func isMeaningfulPitching(_ p: SeasonStatsBlock.SeasonPitching) -> Bool {
        (p.IP ?? 0) >= 10
    }

    /// ".301" — leading-zero stripped per Baseball Reference style.
    private func formatAvg(_ v: Double?) -> String? {
        guard let v else { return nil }
        let s = String(format: "%.3f", v)
        if s.hasPrefix("0.")  { return String(s.dropFirst()) }
        if s.hasPrefix("-0.") { return "-" + String(s.dropFirst(2)) }
        return s
    }

    private func formatEra(_ v: Double?) -> String? {
        guard let v else { return nil }
        return String(format: "%.2f ERA", v)
    }

    private func formatWL(_ w: Int?, _ l: Int?) -> String? {
        guard let w, let l else { return nil }
        return "\(w)-\(l)"
    }

    private func formatWar(_ v: Double?) -> String? {
        guard let v else { return nil }
        return String(format: "%.1f WAR", v)
    }

    /// "132 OPS+" — rounded to integer per Baseball Reference style.
    private func formatOpsPlus(_ v: Double?) -> String? {
        guard let v else { return nil }
        return "\(Int(v.rounded())) OPS+"
    }

    /// "158 ERA+" — same integer treatment as OPS+.
    private func formatEraPlus(_ v: Double?) -> String? {
        guard let v else { return nil }
        return "\(Int(v.rounded())) ERA+"
    }

    /// Vote total formatted to one decimal — Lahman's pointsWon
    /// frequently includes half-points from older ballot rules.
    /// "302.0 / 420" style; missing max collapses to bare points.
    private var pointsWonText: String {
        guard let won = entry.points_won else { return "—" }
        let wonStr = String(format: "%g", won)
        if let max = entry.points_max {
            return "\(wonStr) / \(String(format: "%g", max))"
        }
        return wonStr
    }
}
