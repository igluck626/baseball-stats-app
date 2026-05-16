//
//  BoxScoreView.swift
//  BaseballStats
//
//  Detail view pushed from a Scores tab game card. Surfaces the
//  game header (logos + score + status), inning-by-inning
//  linescore, and per-team batting + pitching lineups.
//
//  Player rows are tappable: the row's MLB id maps to our DB via
//  `APIClient.getPlayerByMlbId`, then we push the existing
//  `PlayerProfileView`. Lookups that miss (player not in our DB)
//  show a brief inline error and stay on this screen.
//

import Combine
import SwiftUI

@MainActor
final class BoxScoreViewModel: ObservableObject {
    let game: Game

    @Published var boxScore: BoxScoreResponse?
    /// Populated only for live games — refreshed every 30s by the
    /// live-feed polling loop. The box-score subtree of the live
    /// feed is projected into `boxScore` so the same render path
    /// works for both modes.
    @Published var live: LiveFeedResponse?
    @Published var isLoading = false
    @Published var error: String?

    private let mlb: MLBStatsAPIClient
    private let api: APIClient
    private var liveTask: Task<Void, Never>?

    init(game: Game, mlb: MLBStatsAPIClient = .shared, api: APIClient = .shared) {
        self.game = game
        self.mlb = mlb
        self.api = api
    }

    func load() async {
        isLoading = true
        error = nil
        if game.phase == .live {
            await loadLive()
        } else {
            await loadStatic()
        }
        isLoading = false
    }

    private func loadStatic() async {
        do {
            boxScore = try await mlb.getBoxScore(gamePk: game.gamePk)
        } catch {
            self.error = error.localizedDescription
            boxScore = nil
        }
    }

    private func loadLive() async {
        do {
            let feed = try await mlb.getLiveFeed(gamePk: game.gamePk)
            live = feed
            if let teams = feed.liveData.boxscore?.teams {
                boxScore = BoxScoreResponse(teams: teams)
            }
        } catch {
            self.error = error.localizedDescription
            live = nil
        }
    }

    /// Starts a 30s polling loop for live games. Idempotent + self-
    /// terminating when `liveData.linescore.inningState` reports
    /// "Final" / "Game Over". Caller stops it on disappear.
    func startLivePolling() {
        guard game.phase == .live else { return }
        stopLivePolling()
        liveTask = Task { @MainActor [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 30 * 1_000_000_000)
                guard !Task.isCancelled, let self else { return }
                await self.loadLive()
                let state = self.live?.liveData.linescore?.inningState?.lowercased()
                if state == "final" || state == "game over" { return }
            }
        }
    }

    func stopLivePolling() {
        liveTask?.cancel()
        liveTask = nil
    }

    /// Resolve an MLB-id-keyed player → our backend's PlayerSearchResult.
    /// Returns nil if the player isn't in our DB; the caller surfaces
    /// that as "Profile not available."
    func playerProfile(mlbId: Int) async -> PlayerSearchResult? {
        (try? await api.getPlayerByMlbId(mlbId)) ?? nil
    }
}

struct BoxScoreView: View {
    @StateObject private var vm: BoxScoreViewModel
    /// Parent (`ScoresView`) owns the NavigationStack path; we append
    /// to it when the user taps a player so the existing
    /// `.navigationDestination(for: PlayerSearchResult.self)` on
    /// ScoresView fires and pushes the profile view.
    @Binding var path: NavigationPath
    @State private var pendingPlayerLookup: Int?
    @State private var navigationError: String?

    init(game: Game, path: Binding<NavigationPath>) {
        _vm = StateObject(wrappedValue: BoxScoreViewModel(game: game))
        _path = path
    }

    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                headerCard
                if vm.game.phase == .live, let live = vm.live?.liveData {
                    liveSituationCard(live)
                }
                if let bs = vm.boxScore {
                    linescoreCard
                    teamSection(side: .away, bs: bs)
                    teamSection(side: .home, bs: bs)
                } else if vm.isLoading {
                    ProgressView().controlSize(.large)
                        .frame(maxWidth: .infinity, minHeight: 120)
                } else if let error = vm.error {
                    Text(error)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                        .padding(.horizontal, 24)
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 16)
        }
        .navigationTitle(navigationTitle)
        .navigationBarTitleDisplayMode(.inline)
        .task {
            await vm.load()
            vm.startLivePolling()
        }
        .onDisappear { vm.stopLivePolling() }
        .overlay(alignment: .top) {
            VStack(spacing: 6) {
                if pendingPlayerLookup != nil {
                    ProgressView()
                        .controlSize(.small)
                        .padding(8)
                        .background(.ultraThinMaterial, in: Capsule())
                }
                if let navigationError {
                    Text(navigationError)
                        .font(.caption.weight(.medium))
                        .foregroundStyle(.primary)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 8)
                        .background(.ultraThinMaterial, in: Capsule())
                        .overlay(
                            Capsule().stroke(Color(.separator).opacity(0.4), lineWidth: 0.5)
                        )
                        .shadow(color: .black.opacity(0.08), radius: 6, x: 0, y: 2)
                        .transition(.move(edge: .top).combined(with: .opacity))
                }
            }
            .padding(.top, 8)
            .animation(.easeInOut(duration: 0.2), value: navigationError)
        }
    }

    private var navigationTitle: String {
        let away = vm.game.teams.away.team.abbreviation
            ?? String(vm.game.teams.away.team.name.prefix(3)).uppercased()
        let home = vm.game.teams.home.team.abbreviation
            ?? String(vm.game.teams.home.team.name.prefix(3)).uppercased()
        return "\(away) @ \(home)"
    }

    // MARK: - Header

    private var headerCard: some View {
        VStack(spacing: 10) {
            // LIVE badge sits centered above the score row so it
            // owns the headline visual; the team-by-score row stays
            // symmetric below.
            if vm.game.phase == .live {
                LiveBadge()
            }
            HStack(spacing: 12) {
                teamHeader(side: vm.game.teams.away)
                Spacer()
                Text(centerStatus)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(vm.game.phase == .live ? Color.red : Color.secondary)
                Spacer()
                teamHeader(side: vm.game.teams.home)
            }
            if let venue = vm.game.venue?.name {
                Text(venue)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 14)
        .frame(maxWidth: .infinity)
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 16))
        .shadow(color: .black.opacity(0.06), radius: 6, x: 0, y: 2)
    }

    /// Live in-game card — surfaces the current matchup, base
    /// runners, and count. Only rendered while `phase == .live`
    /// (the view-body branch is already gated on that), so we can
    /// freely assume the linescore has live state.
    private func liveSituationCard(_ live: LiveData) -> some View {
        let ls = live.linescore
        let play = live.plays?.currentPlay
        let batter = play?.matchup?.batter ?? ls?.offense?.batter
        let pitcher = play?.matchup?.pitcher ?? ls?.defense?.pitcher
        let balls = play?.count?.balls ?? ls?.balls ?? 0
        let strikes = play?.count?.strikes ?? ls?.strikes ?? 0
        let outs = play?.count?.outs ?? ls?.outs ?? 0
        let inningArrow = (ls?.isTopInning).map { $0 ? "▲" : "▼" } ?? ""
        let inningOrd = ls?.currentInningOrdinal ?? "—"
        return VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Text("\(inningArrow) \(inningOrd)")
                    .font(.subheadline.weight(.bold))
                    .foregroundStyle(.red)
                    .monospacedDigit()
                Spacer()
                Text("\(balls)-\(strikes) · \(outs) out\(outs == 1 ? "" : "s")")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
            HStack(alignment: .center, spacing: 16) {
                BaseRunnerView(
                    first:  ls?.offense?.first  != nil,
                    second: ls?.offense?.second != nil,
                    third:  ls?.offense?.third  != nil,
                    size: 44
                )
                VStack(alignment: .leading, spacing: 4) {
                    if let pitcher {
                        Text("Pitching: \(pitcher.fullName)")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                    if let batter {
                        Text("Batting: \(batter.fullName)")
                            .font(.caption.weight(.semibold))
                            .lineLimit(1)
                    }
                }
                Spacer(minLength: 0)
            }
            if let desc = lastPlayDescription(play) {
                Text(desc)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 14)
        .frame(maxWidth: .infinity)
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 16))
        .shadow(color: .black.opacity(0.06), radius: 6, x: 0, y: 2)
    }

    /// Prefer the resolved PA description; fall back to the last
    /// individual pitch event's description for mid-PA states.
    private func lastPlayDescription(_ play: LivePlay?) -> String? {
        if let desc = play?.result?.description, !desc.isEmpty { return desc }
        return play?.playEvents?.compactMap(\.details?.description).last
    }

    private func teamHeader(side: GameTeam) -> some View {
        VStack(spacing: 4) {
            AsyncImage(url: side.team.logoURL) { image in
                image.resizable().scaledToFit()
            } placeholder: {
                Circle().fill(Color(.secondarySystemFill))
            }
            .frame(width: 56, height: 56)
            Text(side.team.abbreviation ?? String(side.team.name.prefix(3)).uppercased())
                .font(.subheadline.weight(.bold))
            Text(side.score.map(String.init) ?? "—")
                .font(.title.weight(.bold))
                .monospacedDigit()
        }
    }

    private var centerStatus: String {
        switch vm.game.phase {
        case .final:   return "FINAL"
        case .live:    return vm.game.linescore?.currentInningOrdinal.map { "LIVE · \($0)" } ?? "LIVE"
        case .preview: return vm.game.startDate.map { Self.timeFormatter.string(from: $0) } ?? "SCHEDULED"
        case .other:   return vm.game.status.detailedState.uppercased()
        }
    }

    // MARK: - Linescore

    private var linescoreCard: some View {
        let innings = vm.game.linescore?.innings ?? []
        let totals = vm.game.linescore?.teams
        let inningCount = max(innings.count, vm.game.linescore?.scheduledInnings ?? 9)
        return VStack(alignment: .leading, spacing: 8) {
            Text("Linescore").font(.headline)
            ScrollView(.horizontal, showsIndicators: false) {
                VStack(alignment: .leading, spacing: 4) {
                    linescoreRow(label: "", cells: (1...inningCount).map { String($0) },
                                 totals: ["R", "H", "E"], bold: true, secondary: true)
                    Divider()
                    linescoreRow(
                        label: vm.game.teams.away.team.abbreviation ?? "AWAY",
                        cells: (1...inningCount).map { i in
                            cellValue(innings.first(where: { $0.num == i })?.away?.runs)
                        },
                        totals: [
                            cellValue(totals?.away?.runs),
                            cellValue(totals?.away?.hits),
                            cellValue(totals?.away?.errors),
                        ],
                        bold: false, secondary: false
                    )
                    linescoreRow(
                        label: vm.game.teams.home.team.abbreviation ?? "HOME",
                        cells: (1...inningCount).map { i in
                            cellValue(innings.first(where: { $0.num == i })?.home?.runs)
                        },
                        totals: [
                            cellValue(totals?.home?.runs),
                            cellValue(totals?.home?.hits),
                            cellValue(totals?.home?.errors),
                        ],
                        bold: false, secondary: false
                    )
                }
                .padding(.horizontal, 4)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 16))
        .shadow(color: .black.opacity(0.06), radius: 6, x: 0, y: 2)
    }

    private func linescoreRow(label: String, cells: [String], totals: [String],
                              bold: Bool, secondary: Bool) -> some View {
        HStack(spacing: 0) {
            Text(label)
                .font(.caption.weight(bold ? .bold : .semibold))
                .frame(width: 50, alignment: .leading)
            ForEach(cells.indices, id: \.self) { i in
                Text(cells[i])
                    .font(.caption.weight(bold ? .bold : .regular))
                    .frame(width: 22, alignment: .trailing)
                    .monospacedDigit()
            }
            Spacer().frame(width: 8)
            ForEach(totals.indices, id: \.self) { i in
                Text(totals[i])
                    .font(.caption.weight(.bold))
                    .frame(width: 22, alignment: .trailing)
                    .monospacedDigit()
            }
        }
        .foregroundStyle(secondary ? Color.secondary : Color.primary)
    }

    private func cellValue(_ v: Int?) -> String {
        guard let v else { return "-" }
        return String(v)
    }

    // MARK: - Per-team batting + pitching

    private enum Side { case away, home }

    private func teamSection(side: Side, bs: BoxScoreResponse) -> some View {
        let team = side == .away ? bs.teams.away : bs.teams.home
        return VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                AsyncImage(url: team.team.logoURL) { image in
                    image.resizable().scaledToFit()
                } placeholder: {
                    Circle().fill(Color(.secondarySystemFill))
                }
                .frame(width: 28, height: 28)
                Text(team.team.name).font(.headline)
            }
            battingTable(team: team)
            pitchingTable(team: team)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 16))
        .shadow(color: .black.opacity(0.06), radius: 6, x: 0, y: 2)
    }

    private func battingTable(team: BoxScoreTeam) -> some View {
        let rows = team.batters.compactMap { id -> BoxPlayer? in
            team.players["ID\(id)"]
        }.filter { $0.stats?.batting?.atBats != nil || ($0.stats?.batting?.baseOnBalls ?? 0) > 0 }

        return VStack(alignment: .leading, spacing: 4) {
            Text("BATTING").font(.caption.weight(.bold)).foregroundStyle(.secondary)
            ScrollView(.horizontal, showsIndicators: false) {
                VStack(alignment: .leading, spacing: 2) {
                    battingHeader
                    Divider().opacity(0.4)
                    ForEach(rows, id: \.person.id) { player in
                        battingRow(player)
                    }
                }
            }
        }
    }

    private var battingHeader: some View {
        HStack(spacing: 0) {
            Text("").frame(width: 140, alignment: .leading)
            ForEach(["AB", "R", "H", "RBI", "BB", "SO", "AVG"], id: \.self) { c in
                Text(c)
                    .font(.caption2.weight(.bold))
                    .foregroundStyle(.secondary)
                    .frame(width: c == "AVG" ? 40 : 28, alignment: .trailing)
                    .monospacedDigit()
            }
        }
    }

    private func battingRow(_ p: BoxPlayer) -> some View {
        let b = p.stats?.batting
        let avg = p.seasonStats?.batting?.avg ?? "—"
        return Button { tapPlayer(id: p.person.id, name: p.person.fullName) } label: {
            HStack(spacing: 0) {
                playerLabel(p, isPitcher: false)
                    .frame(width: 140, alignment: .leading)
                cell(b?.atBats)
                cell(b?.runs)
                cell(b?.hits)
                cell(b?.rbi)
                cell(b?.baseOnBalls)
                cell(b?.strikeOuts)
                Text(avg).font(.caption).monospacedDigit()
                    .frame(width: 40, alignment: .trailing)
            }
            .padding(.vertical, 2)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func pitchingTable(team: BoxScoreTeam) -> some View {
        let rows = team.pitchers.compactMap { id -> BoxPlayer? in
            team.players["ID\(id)"]
        }
        return VStack(alignment: .leading, spacing: 4) {
            Text("PITCHING").font(.caption.weight(.bold)).foregroundStyle(.secondary)
            ScrollView(.horizontal, showsIndicators: false) {
                VStack(alignment: .leading, spacing: 2) {
                    pitchingHeader
                    Divider().opacity(0.4)
                    ForEach(rows, id: \.person.id) { player in
                        pitchingRow(player)
                    }
                }
            }
        }
    }

    private var pitchingHeader: some View {
        HStack(spacing: 0) {
            Text("").frame(width: 140, alignment: .leading)
            ForEach(["IP", "H", "R", "ER", "BB", "SO", "ERA"], id: \.self) { c in
                Text(c)
                    .font(.caption2.weight(.bold))
                    .foregroundStyle(.secondary)
                    .frame(width: c == "ERA" ? 44 : 28, alignment: .trailing)
                    .monospacedDigit()
            }
        }
    }

    private func pitchingRow(_ p: BoxPlayer) -> some View {
        let pit = p.stats?.pitching
        let era = p.seasonStats?.pitching?.era ?? "—"
        return Button { tapPlayer(id: p.person.id, name: p.person.fullName) } label: {
            HStack(spacing: 0) {
                playerLabel(p, isPitcher: true)
                    .frame(width: 140, alignment: .leading)
                Text(pit?.inningsPitched ?? "-").font(.caption).monospacedDigit()
                    .frame(width: 28, alignment: .trailing)
                cell(pit?.hits)
                cell(pit?.runs)
                cell(pit?.earnedRuns)
                cell(pit?.baseOnBalls)
                cell(pit?.strikeOuts)
                Text(era).font(.caption).monospacedDigit()
                    .frame(width: 44, alignment: .trailing)
            }
            .padding(.vertical, 2)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func playerLabel(_ p: BoxPlayer, isPitcher: Bool) -> some View {
        HStack(spacing: 4) {
            Text(shortName(p.person.fullName))
                .font(.caption.weight(.medium))
                .lineLimit(1)
            if let pos = p.position?.abbreviation, !isPitcher {
                Text(pos)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func cell(_ v: Int?) -> some View {
        Text(v.map(String.init) ?? "-")
            .font(.caption)
            .monospacedDigit()
            .frame(width: 28, alignment: .trailing)
    }

    private func shortName(_ full: String) -> String {
        let parts = full.split(separator: " ")
        guard let first = parts.first, let last = parts.dropFirst().last else { return full }
        return "\(first.prefix(1)). \(last)"
    }

    // MARK: - Navigation

    private func tapPlayer(id: Int, name: String) {
        guard pendingPlayerLookup == nil else { return }
        pendingPlayerLookup = id
        navigationError = nil
        Task { @MainActor in
            let player = await vm.playerProfile(mlbId: id)
            pendingPlayerLookup = nil
            if let player {
                path.append(player)
                return
            }
            // 404 → player isn't in our DB yet. The nightly batch
            // adds new call-ups from bref + MLB Stats API on its
            // next run, so the toast wording sets the expectation
            // ("yet") rather than implying permanent unavailability.
            navigationError = "\(name)'s profile isn't available yet."
            // Auto-dismiss the toast after a few seconds so a
            // stranded message doesn't persist on the box score.
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            if navigationError != nil { navigationError = nil }
        }
    }

    private static let timeFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "h:mm a"
        return f
    }()
}
