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

/// Season counting-stat totals for one player, keyed off BDL id
/// in `BoxScoreViewModel.seasonTotals`. Populated lazily after
/// the box-score load for the small set of batters who had a
/// notable play (HR / 2B / 3B) in this game — the notable lines
/// render "(N)" off these numbers.
struct PlayerSeasonTotals {
    let hr:      Int
    let doubles: Int
    let triples: Int
}

@MainActor
final class BoxScoreViewModel: ObservableObject {
    let game: Game

    @Published var boxScore: BoxScoreResponse?
    /// Live-game synthesized snapshot — Stage C of the BDL migration
    /// will populate this from BDL plays + plate appearances.
    /// Stage A (this commit) leaves it nil for live games; the
    /// view falls back to "—" placeholders in the live situation
    /// card until Stage C lands.
    @Published var live: LiveFeedResponse?
    /// Season totals for batters who had a HR / 2B / 3B in this
    /// game. Keyed by BDL player id (the id the box score uses
    /// for `BoxPlayer.person.id`). Populated by `loadNotableSeasonTotals`
    /// after `loadBoxScore` lands, so the notable-plays section
    /// renders "(N)" instead of just names.
    @Published var seasonTotals: [Int: PlayerSeasonTotals] = [:]
    @Published var isLoading = false
    @Published var error: String?

    private let bdl: BallDontLieClient
    private let api: APIClient
    private var liveTask: Task<Void, Never>?

    init(game: Game, bdl: BallDontLieClient = .shared, api: APIClient = .shared) {
        self.game = game
        self.bdl  = bdl
        self.api  = api
    }

    func load() async {
        isLoading = true
        error = nil
        // Same fetch for live + final — BDL's `/stats?game_ids[]=`
        // returns per-player lines whether the game is in progress
        // or done. For live games we ALSO pull the plays + PA
        // streams in parallel; the synthesizer in Scores.swift
        // produces the legacy `LiveFeedResponse` shape the live
        // situation card consumes.
        if game.phase == .live {
            async let boxTask  = loadBoxScore()
            async let liveTask = loadLiveState()
            _ = await boxTask
            _ = await liveTask
        } else {
            await loadBoxScore()
        }
        isLoading = false
        // Kick off the season-totals fetch in the background once
        // the box score has landed. Doesn't block the main view —
        // notable lines render names-only until totals arrive,
        // then re-render with the parenthetical.
        await loadNotableSeasonTotals()
    }

    /// For each batter with a HR / 2B / 3B in this game, look up
    /// their season totals via the two-hop backend bridge:
    /// BDL id → MLBAM id (via `resolveBDLPlayerId`) → season stats
    /// (via `getPlayerCurrentStats`). Runs all fetches in parallel.
    /// Silent on per-player failures — the notable line just shows
    /// names-only for unresolved players.
    private func loadNotableSeasonTotals() async {
        guard let bs = boxScore else { return }
        // Collect BDL ids for batters with a notable play across
        // both teams. De-duped via Set.
        var ids: Set<Int> = []
        for side in [bs.teams.away, bs.teams.home] {
            for (_, bp) in side.players {
                let b = bp.stats?.batting
                if (b?.homeRuns ?? 0) > 0
                    || (b?.doubles ?? 0) > 0
                    || (b?.triples ?? 0) > 0 {
                    ids.insert(bp.person.id)
                }
            }
        }
        guard !ids.isEmpty else { return }

        let fetched: [(Int, PlayerSeasonTotals)] = await withTaskGroup(
            of: (Int, PlayerSeasonTotals?).self
        ) { group in
            for bdlId in ids {
                group.addTask { [bdl, api] in
                    do {
                        // Two hops: BDL → MLBAM → backend stats.
                        let player = try await bdl.resolveBDLPlayerId(bdlId)
                        let stats  = try await api.getPlayerCurrentStats(
                            playerId: player.player_id,
                        )
                        guard let s = stats?.standard else { return (bdlId, nil) }
                        return (bdlId, PlayerSeasonTotals(
                            hr:      s.HR      ?? 0,
                            doubles: s.doubles ?? 0,
                            triples: s.triples ?? 0,
                        ))
                    } catch {
                        return (bdlId, nil)
                    }
                }
            }
            var out: [(Int, PlayerSeasonTotals)] = []
            for await (bdlId, totals) in group {
                if let t = totals { out.append((bdlId, t)) }
            }
            return out
        }
        for (bdlId, totals) in fetched {
            self.seasonTotals[bdlId] = totals
        }
    }

    private func loadLiveState() async {
        async let playsTask = bdl.getPlays(gameId: game.gamePk)
        async let pasTask   = bdl.getPlateAppearances(gameId: game.gamePk)
        guard let plays = try? await playsTask else {
            live = nil
            return
        }
        let pas = (try? await pasTask) ?? []
        live = plays.toLiveFeedResponse(plateAppearances: pas)
    }

    private func loadBoxScore() async {
        do {
            // BDL game IDs round-trip through `game.gamePk` — the
            // Phase 2 `BDLGame.toGame()` projection put the BDL id
            // there. Fetch the per-player stat lines AND the
            // starting lineup in parallel; the lineup drives the
            // batting/pitching order in the synthesizer.
            async let statsTask  = bdl.getGameStats(gameId: game.gamePk)
            async let lineupTask = bdl.getGameLineup(gameId: game.gamePk)
            let lineup = (try? await lineupTask) ?? []
            // Season-stats fetch depends on the lineup ids, so it
            // sequences AFTER lineup but runs concurrently with the
            // stats await (per-game /stats endpoint is independent).
            // Failures degrade to "—" placeholders rather than
            // blocking the box score from rendering.
            async let seasonTask = loadLineupSeasonStats(lineup: lineup)
            let stats  = try await statsTask
            let seasonStatsByPid = await seasonTask

            // Resolve which BDL team object pairs with each side of
            // the game. The `Game.teams.{away,home}.team` carries
            // MLBAM ids (set by `BDLTeam.toTeamInfo()`); reverse-
            // lookup via the static map gives us the BDL team
            // shape the synthesizer needs. When resolution fails
            // (BDL team id not in our hardcoded map, or stats
            // payload lacks a usable nested team — both happen
            // periodically as BDL adds new ids), we build stub
            // BDLTeam objects from the Game's existing TeamInfo
            // so the box score still renders. Logos may degrade
            // but the batting/pitching tables work fine.
            let (awayBDL, homeBDL) = bdlTeams(forGame: game, fromStats: stats)
            boxScore = stats.toBoxScoreResponse(
                awayTeam:         awayBDL,
                homeTeam:         homeBDL,
                awayBDLTeamId:    game.bdlAwayTeamId,
                homeBDLTeamId:    game.bdlHomeTeamId,
                lineup:           lineup,
                seasonStatsByPid: seasonStatsByPid,
            )
        } catch {
            self.error = error.localizedDescription
            boxScore = nil
        }
    }

    /// Bulk-fetch season AVG / OPS / ERA for every player in the
    /// starting lineup. Used by the placeholder rows so a starter
    /// who hasn't batted yet still shows their season rate stats.
    /// Returns an empty dict on failure (placeholders fall back to
    /// "—" via the synthesizer's nil-coalesce).
    private func loadLineupSeasonStats(
        lineup: [BDLGameLineup],
    ) async -> [Int: BDLSeasonStat] {
        let ids = Array(Set(lineup.map(\.player.id)))
        guard !ids.isEmpty else { return [:] }
        // BDL standings / season stats are season-keyed; use the
        // year the game was actually played in (game.startDate falls
        // back to "now" when BDL ships an unparseable date).
        let season = Calendar.current.component(.year, from: game.startDate ?? Date())
        do {
            let rows = try await bdl.getSeasonStats(playerIds: ids, season: season)
            return Dictionary(
                rows.map { ($0.player.id, $0) },
                uniquingKeysWith: { a, _ in a },
            )
        } catch {
            return [:]
        }
    }

    /// Re-derive `BDLTeam` objects for the two sides from the stats
    /// payload (each row carries its team's `BDLTeam` via the player
    /// nesting). When resolution fails for either side, falls back
    /// to a stub built from the game's existing `TeamInfo` so the
    /// synthesizer always has inputs and the box-score view never
    /// blocks on a team-lookup miss. Stubs lose the BDL `id` (set
    /// to 0) — logos go through the MLBAM bridge instead, so they
    /// still resolve via `Game.teams.{home,away}.team`.
    private func bdlTeams(
        forGame game: Game, fromStats stats: [BDLPlayerStat],
    ) -> (away: BDLTeam, home: BDLTeam) {
        // Try to pull from the stats nesting: any row's `player.team`
        // is the BDLTeam for that player's side this game.
        let byName: [String: BDLTeam] = Dictionary(
            stats.compactMap { s -> (String, BDLTeam)? in
                guard let t = s.player.team else { return nil }
                return (t.name, t)
            },
            uniquingKeysWith: { a, _ in a },
        )

        let awayName = game.teams.away.team.abbreviation ?? game.teams.away.team.name
        let homeName = game.teams.home.team.abbreviation ?? game.teams.home.team.name

        // Names ship differently depending on path — try a few
        // shapes (BDL `name` is the short franchise, `abbreviation`
        // is "NYY" etc., `displayName` is "New York Yankees").
        func resolve(_ ref: String, mlbId: Int) -> BDLTeam? {
            if let t = byName[ref] { return t }
            return byName.values.first { t in
                t.abbreviation == ref || t.displayName == ref || t.name == ref
            } ?? byName.values.first { t in
                mlbTeamId(forBDLId: t.id) == mlbId
            }
        }

        let away = resolve(awayName, mlbId: game.teams.away.team.id)
                   ?? Self.stubBDLTeam(from: game.teams.away.team)
        let home = resolve(homeName, mlbId: game.teams.home.team.id)
                   ?? Self.stubBDLTeam(from: game.teams.home.team)
        return (away, home)
    }

    /// Synthesize a minimal `BDLTeam` from a legacy `TeamInfo` for
    /// the fallback path above. The id is left at 0 (BDL doesn't
    /// know about this stub, by construction) — every other
    /// downstream consumer reads `name`/`displayName`/`abbreviation`,
    /// which we have, or hops through the MLBAM bridge for logos.
    private static func stubBDLTeam(from info: TeamInfo) -> BDLTeam {
        let abbr = info.abbreviation ?? String(info.name.prefix(3)).uppercased()
        return BDLTeam(
            id:                0,
            slug:              nil,
            abbreviation:      abbr,
            displayName:       info.name,
            shortDisplayName:  nil,
            name:              info.name,
            location:          "",
            league:            nil,
            division:          nil,
        )
    }

    /// 30s polling loop for live games — re-fetches the box score
    /// (per-player stat lines + linescore inputs) and the live
    /// situation streams (plays + PAs → live card data). Self-
    /// terminates when the synthesized inningState reports the
    /// game has gone final.
    func startLivePolling() {
        guard game.phase == .live else { return }
        stopLivePolling()
        liveTask = Task { @MainActor [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 30 * 1_000_000_000)
                guard !Task.isCancelled, let self else { return }
                async let boxTask  = self.loadBoxScore()
                async let liveTask = self.loadLiveState()
                _ = await boxTask
                _ = await liveTask
                let state = self.live?.liveData.linescore?.inningState?.lowercased()
                if state == "final" || state == "game over" { return }
            }
        }
    }

    func stopLivePolling() {
        liveTask?.cancel()
        liveTask = nil
    }

    /// Resolve a BDL-id-keyed player → our backend's PlayerSearchResult.
    /// Returns nil if the player isn't BDL-mapped in our DB (the
    /// bootstrap walk is still adding bdl_ids to historical rows);
    /// caller surfaces that as "Profile not available."
    func playerProfile(bdlId: Int) async -> PlayerSearchResult? {
        (try? await bdl.resolveBDLPlayerId(bdlId)) ?? nil
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
    /// User override for the team-selector segmented control. nil
    /// → use `defaultSide` (home for final, offensive team for
    /// live). The actual rendered side is `currentSide`.
    @State private var selectedSide: TeamSide?

    /// Which team's batting + pitching table to render. The view
    /// shows one team at a time instead of stacking both — toggled
    /// via the segmented control at the top.
    enum TeamSide: String, Hashable, Identifiable, CaseIterable {
        case away, home
        var id: String { rawValue }
    }

    init(game: Game, path: Binding<NavigationPath>) {
        _vm = StateObject(wrappedValue: BoxScoreViewModel(game: game))
        _path = path
    }

    /// Default-selected team when the user hasn't tapped the
    /// segmented control yet. Final → home (the venue's team is
    /// the natural anchor). Live → whichever side is batting; for
    /// top of the inning the away team is offense, otherwise home.
    /// Preview / other → home (no strong default; consistent with
    /// final so the picker doesn't surprise the user pre-game).
    private var defaultSide: TeamSide {
        switch vm.game.phase {
        case .live:
            // `isTopInning == true` → away team batting → away
            // offensive; default the box-score view to it so the
            // user lands on the side that's currently active.
            let isTop = vm.live?.liveData.linescore?.isTopInning
                ?? vm.game.linescore?.isTopInning
                ?? false
            return isTop ? .away : .home
        case .final, .preview, .other:
            return .home
        }
    }

    private var currentSide: TeamSide {
        selectedSide ?? defaultSide
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
                    teamPicker(bs: bs)
                    teamSection(side: currentSide, bs: bs)
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
            TeamLogoView(team: side.team, size: 56)
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

    /// Segmented control sitting above the per-team box-score
    /// tables. Labels show each team's full short name so the
    /// active side reads cleanly at a glance; the segmented style
    /// matches the Recent Games window picker on the player
    /// profile so the control feels like part of the same family.
    private func teamPicker(bs: BoxScoreResponse) -> some View {
        Picker(
            "Team",
            selection: Binding(
                get: { currentSide },
                set: { selectedSide = $0 }
            )
        ) {
            Text(bs.teams.away.team.name).tag(TeamSide.away)
            Text(bs.teams.home.team.name).tag(TeamSide.home)
        }
        .pickerStyle(.segmented)
    }

    private func teamSection(side: TeamSide, bs: BoxScoreResponse) -> some View {
        let team = side == .away ? bs.teams.away : bs.teams.home
        return VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                TeamLogoView(team: team.team, size: 28)
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
            notableBlock(rows: rows)
        }
    }

    /// Per-column widths for the batting table. Kept in one place
    /// so header + row stay aligned and the total stays under the
    /// iPhone safe-width — `nameCol + sum(statCols)` should clear
    /// ~360pt with room for safe-area padding so OPS lands without
    /// horizontal scrolling.
    private enum BattingCol {
        static let name: CGFloat = 96
        static let ab:   CGFloat = 22
        static let r:    CGFloat = 22
        static let h:    CGFloat = 22
        static let rbi:  CGFloat = 28
        static let bb:   CGFloat = 22
        static let so:   CGFloat = 22
        static let avg:  CGFloat = 36
        static let ops:  CGFloat = 36
    }

    private var battingHeader: some View {
        HStack(spacing: 0) {
            Text("").frame(width: BattingCol.name, alignment: .leading)
            battingHeaderCell("AB",  width: BattingCol.ab)
            battingHeaderCell("R",   width: BattingCol.r)
            battingHeaderCell("H",   width: BattingCol.h)
            battingHeaderCell("RBI", width: BattingCol.rbi)
            battingHeaderCell("BB",  width: BattingCol.bb)
            battingHeaderCell("SO",  width: BattingCol.so)
            battingHeaderCell("AVG", width: BattingCol.avg)
            battingHeaderCell("OPS", width: BattingCol.ops)
        }
    }

    private func battingHeaderCell(_ label: String, width: CGFloat) -> some View {
        Text(label)
            .font(.caption2.weight(.bold))
            .foregroundStyle(.secondary)
            .frame(width: width, alignment: .trailing)
            .monospacedDigit()
    }

    private func battingRow(_ p: BoxPlayer) -> some View {
        let b = p.stats?.batting
        let avg = p.seasonStats?.batting?.avg ?? "—"
        let ops = p.seasonStats?.batting?.ops ?? "—"
        return Button { tapPlayer(id: p.person.id, name: p.person.fullName) } label: {
            HStack(spacing: 0) {
                playerLabel(p, isPitcher: false)
                    .frame(width: BattingCol.name, alignment: .leading)
                cell(b?.atBats,      width: BattingCol.ab)
                cell(b?.runs,        width: BattingCol.r)
                cell(b?.hits,        width: BattingCol.h)
                cell(b?.rbi,         width: BattingCol.rbi)
                cell(b?.baseOnBalls, width: BattingCol.bb)
                cell(b?.strikeOuts,  width: BattingCol.so)
                Text(avg).font(.caption).monospacedDigit()
                    .frame(width: BattingCol.avg, alignment: .trailing)
                Text(ops).font(.caption).monospacedDigit()
                    .frame(width: BattingCol.ops, alignment: .trailing)
            }
            .padding(.vertical, 2)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    /// "Notable" highlights below the batting table — 2B / 3B / HR
    /// per category, only rendered when at least one batter on the
    /// team logged the outcome this game. Season totals come from
    /// `seasonStats.batting.{doubles,triples,homeRuns}` so the line
    /// reads as "Acuña Jr. (12)" — the parenthetical year-to-date
    /// count gives the user instant context without leaving the
    /// box score. Multiple players with the same outcome are
    /// comma-joined, ordered by their batting-order appearance.
    @ViewBuilder
    private func notableBlock(rows: [BoxPlayer]) -> some View {
        let doubles  = rows.filter { ($0.stats?.batting?.doubles  ?? 0) > 0 }
        let triples  = rows.filter { ($0.stats?.batting?.triples  ?? 0) > 0 }
        let homeRuns = rows.filter { ($0.stats?.batting?.homeRuns ?? 0) > 0 }
        if !doubles.isEmpty || !triples.isEmpty || !homeRuns.isEmpty {
            VStack(alignment: .leading, spacing: 2) {
                // Season totals are fetched lazily by the view-
                // model (two-hop via /by-bdl-id + /stats/current).
                // Until they arrive (or for unresolved players),
                // the parenthetical is omitted — names render
                // alone. Once `vm.seasonTotals` updates with a
                // hit, the SwiftUI re-render adds "(N)".
                if !doubles.isEmpty {
                    notableLine(
                        label:    "2B",
                        players:  doubles,
                        totalKey: \.doubles,
                        gameKey:  \.doubles,
                    )
                }
                if !triples.isEmpty {
                    notableLine(
                        label:    "3B",
                        players:  triples,
                        totalKey: \.triples,
                        gameKey:  \.triples,
                    )
                }
                if !homeRuns.isEmpty {
                    notableLine(
                        label:    "HR",
                        players:  homeRuns,
                        totalKey: \.hr,
                        gameKey:  \.homeRuns,
                    )
                }
            }
            .padding(.top, 4)
        }
    }

    private func notableLine(
        label: String,
        players: [BoxPlayer],
        totalKey: KeyPath<PlayerSeasonTotals, Int>,
        gameKey:  KeyPath<BoxBatting,         Int?>,
    ) -> some View {
        // `vm.seasonTotals[bdl_id]` holds the PRE-game season total
        // (snapshot from our backend, taken at the start of the
        // box-score load). Today's game count is on `bp.stats.batting`.
        // Display = pre-game + today, so a player who hit his 5th HR
        // today shows "(5)" rather than the pre-game "(4)".
        let pieces: [String] = players.map { bp in
            let last = lastName(bp.person.fullName)
            guard let preGame = vm.seasonTotals[bp.person.id]?[keyPath: totalKey] else {
                return last
            }
            let today = bp.stats?.batting?[keyPath: gameKey] ?? 0
            return "\(last) (\(preGame + today))"
        }
        return (Text("\(label): ").font(.caption2.weight(.bold))
                + Text(pieces.joined(separator: ", ")).font(.caption2))
            .foregroundStyle(.secondary)
            .lineLimit(2)
            .fixedSize(horizontal: false, vertical: true)
    }

    /// "Acuña Jr." — keep the last token of a hyphenated/multi-word
    /// surname (handles Vladimir Guerrero Jr., Ronald Acuña Jr.,
    /// J.D. Martinez). Mirrors the `lastName` helper used in the
    /// decisions row on Final game cards.
    private func lastName(_ full: String) -> String {
        full.split(separator: " ").last.map(String.init) ?? full
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

    private func cell(_ v: Int?, width: CGFloat = 28) -> some View {
        Text(v.map(String.init) ?? "-")
            .font(.caption)
            .monospacedDigit()
            .frame(width: width, alignment: .trailing)
    }

    private func shortName(_ full: String) -> String {
        let parts = full.split(separator: " ")
        guard let first = parts.first, parts.count >= 2 else { return full }
        // `lastNameWithSuffix` handles the Jr./Sr./II/III/IV cases —
        // "Fernando Tatis Jr." → "Tatis Jr." rather than "Jr.".
        return "\(first.prefix(1)). \(lastNameWithSuffix(full))"
    }

    // MARK: - Navigation

    private func tapPlayer(id: Int, name: String) {
        // `id` is a BDL player id (BoxScoreResponse synthesized
        // from BDL keys players by BDL id, not MLBAM). The resolve
        // call hops through our backend's `/players/by-bdl-id/{id}`
        // to land on an MLBAM-keyed PlayerSearchResult that the
        // existing profile destination can consume.
        guard pendingPlayerLookup == nil else { return }
        pendingPlayerLookup = id
        navigationError = nil
        Task { @MainActor in
            let player = await vm.playerProfile(bdlId: id)
            pendingPlayerLookup = nil
            if let player {
                path.append(player)
                return
            }
            // 404 → player's bdl_id isn't mapped in our DB yet.
            // The mapping bootstrap walk is still extending into
            // historical rows; toast wording reflects that ("yet")
            // rather than implying permanent unavailability.
            navigationError = "\(name)'s profile isn't available yet."
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
