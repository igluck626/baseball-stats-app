//
//  HomeView.swift
//  BaseballStats
//
//  Home tab root. Two states:
//  - No favorite team yet → embedded TeamPickerView.
//  - Favorite team set → hero card (with embedded recent/upcoming
//    game strip) + compact team-leaders card + favorite players.
//
//  Settings (team picker) lives in a trailing toolbar button; the
//  inline navigation title shows the team logo + city/name so the
//  user always knows whose context is on screen.
//

import SwiftUI

struct HomeView: View {
    @StateObject private var vm = HomeViewModel()
    @ObservedObject private var store = FavoriteTeamStore.shared
    @ObservedObject private var favoritesStore = FavoritePlayersStore.shared
    @State private var navigationPath = NavigationPath()
    @State private var showingPicker = false
    @State private var showingAddPlayer = false
    @State private var showingSchedule = false
    @State private var showingLeadersSheet = false
    @State private var showingRosterSheet = false
    @State private var showingHistorySheet = false
    @State private var showingInjurySheet = false
    @State private var isEditingFavorites = false

    var body: some View {
        NavigationStack(path: $navigationPath) {
            ZStack {
                backgroundGradient
                content
            }
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { toolbarContent }
            .navigationDestination(for: Game.self) { game in
                BoxScoreView(
                    game:           game,
                    teamStandings:  vm.teamStandings,
                    teamRecords:    vm.teamRecords,
                    path:           $navigationPath,
                )
            }
            .navigationDestination(for: PlayerSearchResult.self) { player in
                PlayerProfileView(player: player)
            }
            .sheet(isPresented: $showingPicker) {
                TeamPickerView { _ in showingPicker = false }
            }
            .sheet(isPresented: $showingAddPlayer) {
                AddFavoritePlayerSheet { picked in
                    favoritesStore.add(picked.player_id)
                }
            }
            .sheet(isPresented: $showingSchedule) {
                if let bdlId = store.bdlTeamId,
                   let entry = MLBTeamCatalog.entry(forBDLId: bdlId) {
                    ScheduleSheet(
                        favorite:      entry,
                        teamStandings: vm.teamStandings,
                        teamRecords:   vm.teamRecords,
                    )
                }
            }
            .sheet(isPresented: $showingLeadersSheet) {
                if let bdlId = store.bdlTeamId,
                   let entry = MLBTeamCatalog.entry(forBDLId: bdlId) {
                    TeamLeadersSheet(entry: entry, vm: vm)
                        .presentationDetents([.large])
                }
            }
            .sheet(isPresented: $showingRosterSheet) {
                if let bdlId = store.bdlTeamId,
                   let entry = MLBTeamCatalog.entry(forBDLId: bdlId) {
                    RosterSheet(
                        entry:     entry,
                        roster:    vm.roster,
                        isLoading: vm.isLoadingRoster,
                    )
                    .presentationDetents([.large])
                }
            }
            .sheet(isPresented: $showingHistorySheet) {
                if let bdlId = store.bdlTeamId,
                   let entry = MLBTeamCatalog.entry(forBDLId: bdlId) {
                    TeamHistorySheet(
                        entry:            entry,
                        history:          vm.teamHistory,
                        postseasonByYear: vm.postseasonByYear,
                        isLoading:        vm.isLoadingHistory,
                    )
                    .presentationDetents([.large])
                }
            }
            .sheet(isPresented: $showingInjurySheet) {
                if let bdlId = store.bdlTeamId,
                   let entry = MLBTeamCatalog.entry(forBDLId: bdlId) {
                    InjuryReportSheet(
                        entry:     entry,
                        players:   vm.injuredPlayers,
                        resolved:  vm.injuredPlayersResolved,
                        isLoading: vm.isLoadingInjuries,
                    )
                    .presentationDetents([.large])
                }
            }
        }
        .task(id: store.bdlTeamId) {
            guard let bdlId = store.bdlTeamId else { return }
            await vm.load(bdlTeamId: bdlId)
            await vm.loadInjuries(bdlTeamId: bdlId)
            await vm.loadTeamLeaders(bdlTeamId: bdlId)
            await vm.loadRoster(bdlTeamId: bdlId)
            await vm.loadTeamHistory(bdlTeamId: bdlId)
            vm.startAutoRefresh(bdlTeamId: bdlId)
        }
        .task(id: favoritesStore.playerIds) {
            await vm.loadFavoritePlayers(ids: favoritesStore.playerIds)
        }
        .onDisappear { vm.stopAutoRefresh() }
    }

    // MARK: - Chrome

    /// Team-color hex resolved off the current favorite. Falls back
    /// to `.accentColor` when no favorite is set, the BDL id can't
    /// be mapped to a Lahman entry, or the Lahman code isn't in the
    /// `TeamColors` dict (extreme historical / typo cases).
    /// Reading `store.bdlTeamId` through `@ObservedObject` makes
    /// every consumer of `teamColor` reactively rebuild when the
    /// user picks a different team.
    private var teamColor: Color {
        guard let bdlId = store.bdlTeamId,
              let entry = MLBTeamCatalog.entry(forBDLId: bdlId),
              let color = TeamColors.color(for: entry.lahmanCode)
        else { return .accentColor }
        return color
    }

    /// Full-screen team-color wash. Strong (0.85) at the top, fading
    /// gradually through 0.45 → 0.20 → 0.08 → 0.03 so it never hits
    /// an abrupt white cutoff and the bottom of the screen still
    /// carries a whisper of the team's color rather than reverting
    /// to plain system background. Sits on a `Color(.systemBackground)`
    /// base so the very-low-opacity final stop blends to a tinted
    /// neutral rather than transparent void.
    private var backgroundGradient: some View {
        ZStack {
            Color(.systemBackground)
                .ignoresSafeArea()
            LinearGradient(
                colors: [
                    teamColor.opacity(0.85),
                    teamColor.opacity(0.45),
                    teamColor.opacity(0.20),
                    teamColor.opacity(0.08),
                    teamColor.opacity(0.03),
                ],
                startPoint: .top,
                endPoint: .bottom,
            )
            .ignoresSafeArea()
        }
    }

    @ToolbarContentBuilder
    private var toolbarContent: some ToolbarContent {
        ToolbarItem(placement: .principal) {
            if let bdlId = store.bdlTeamId,
               let entry = MLBTeamCatalog.entry(forBDLId: bdlId) {
                HStack(spacing: 6) {
                    TeamLogoView(team: entry.teamInfo, size: 22)
                    Text(entry.fullName)
                        .font(.headline.weight(.semibold))
                        .lineLimit(1)
                }
            }
        }
        ToolbarItem(placement: .topBarTrailing) {
            if store.bdlTeamId != nil {
                Button {
                    showingPicker = true
                } label: {
                    Image(systemName: "gearshape")
                        .font(.body.weight(.semibold))
                }
                .accessibilityLabel("Settings")
            }
        }
    }

    @ViewBuilder
    private var content: some View {
        if store.bdlTeamId == nil {
            TeamPickerView()
        } else if vm.isLoading && !vm.didLoad {
            loadingSkeleton
        } else if let bdlId = store.bdlTeamId,
                  let entry = MLBTeamCatalog.entry(forBDLId: bdlId) {
            loadedContent(entry: entry)
        }
    }

    @ViewBuilder
    private func loadedContent(entry: MLBTeamCatalog.Entry) -> some View {
        let tint = TeamColors.color(for: entry.lahmanCode) ?? .accentColor
        ScrollView {
            VStack(spacing: 14) {
                TeamHeroCard(
                    entry:        entry,
                    record:       vm.teamRecord,
                    standing:     vm.teamStanding,
                    streakCode:   vm.teamStreakCode,
                    lastTenW:     vm.teamLastTenW,
                    lastTenL:     vm.teamLastTenL,
                    lastGame:     vm.lastGame,
                    nextGame:     vm.nextGame,
                    stripGames:   vm.recentAndUpcoming,
                    onSchedule:   { showingSchedule = true },
                    onTapStripGame: { game in navigationPath.append(game) },
                )

                TeamLeadersSection(
                    leaders:     vm.teamLeaders,
                    isLoading:   vm.isLoadingLeaders,
                    tint:        tint,
                    onSeeAll:    { showingLeadersSheet = true },
                    onTapPlayer: { player in
                        navigationPath.append(player)
                    },
                )

                RosterSection(
                    roster:    vm.roster,
                    isLoading: vm.isLoadingRoster,
                    tint:      tint,
                    onSeeAll:  { showingRosterSheet = true },
                )

                if !vm.injuredPlayers.isEmpty {
                    InjuryReportSection(
                        count:    vm.injuredPlayers.count,
                        tint:     tint,
                        onSeeAll: { showingInjurySheet = true },
                    )
                }

                TeamHistorySection(
                    tint:     tint,
                    onSeeAll: { showingHistorySheet = true },
                )

                FavoritePlayersSection(
                    favorites:    vm.favoritePlayers,
                    isLoading:    vm.isLoadingFavorites,
                    isEditing:    $isEditingFavorites,
                    tint:         tint,
                    onAdd:        { showingAddPlayer = true },
                    onRemove:     { id in favoritesStore.remove(id) },
                    onTapPlayer:  { player in
                        navigationPath.append(player)
                    },
                )
            }
            .padding(.top, 12)
            .padding(.bottom, 16)
        }
        .refreshable {
            if let bdlId = store.bdlTeamId {
                await vm.load(bdlTeamId: bdlId)
            }
        }
    }

    private var loadingSkeleton: some View {
        VStack(spacing: 14) {
            RoundedRectangle(cornerRadius: 18)
                .fill(Color(.systemGray5))
                .frame(height: 340)
                .padding(.horizontal, 16)
            RoundedRectangle(cornerRadius: 18)
                .fill(Color(.systemGray5))
                .frame(height: 280)
                .padding(.horizontal, 16)
            Spacer()
        }
        .padding(.top, 12)
    }
}

// MARK: - Team Hero Card (with embedded game strip)

/// Top-of-tab card. Hosts the team's bio block (logo + name + record
/// + division + streak/L10), the last/next game rows, and a compact
/// horizontal scroll of recent/upcoming games — all within a single
/// rounded-rectangle card so the related context lives together.
private struct TeamHeroCard: View {
    let entry: MLBTeamCatalog.Entry
    let record: TeamRecord?
    let standing: TeamStandingInfo?
    let streakCode: String?
    let lastTenW: Int?
    let lastTenL: Int?
    let lastGame: Game?
    let nextGame: Game?
    let stripGames: [Game]
    let onSchedule: () -> Void
    let onTapStripGame: (Game) -> Void

    private var teamColor: Color {
        TeamColors.color(for: entry.lahmanCode) ?? Color.accentColor
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            if hasL10OrStreak {
                streakLine
            }
            Divider().opacity(0.4)
            lastGameRow
            nextGameRow
            if !stripGames.isEmpty {
                Divider().opacity(0.4)
                gameStrip
            }
            scheduleLink
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        // Pure `.ultraThinMaterial` — no flat team-color overlay.
        // The glass picks up the saturated team gradient bleeding
        // through from the background layer, producing a naturally-
        // tinted card that matches the player profile chrome
        // without an explicit color fill on top.
        .background(
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .fill(.ultraThinMaterial)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .strokeBorder(teamColor.opacity(0.25), lineWidth: 1)
        )
        .shadow(color: .black.opacity(0.06), radius: 8, x: 0, y: 3)
        .padding(.horizontal, 16)
    }

    private var header: some View {
        HStack(alignment: .center, spacing: 16) {
            TeamLogoView(team: entry.teamInfo, size: 88)
                .shadow(color: .black.opacity(0.1), radius: 4, y: 2)

            VStack(alignment: .leading, spacing: 4) {
                Text(entry.fullName)
                    .font(.title3.weight(.bold))
                    .lineLimit(1)
                    .minimumScaleFactor(0.85)
                Text(recordText)
                    .font(.title2.weight(.semibold))
                    .foregroundStyle(.primary)
                    .monospacedDigit()
                Text(divisionText)
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
        }
    }

    private var hasL10OrStreak: Bool {
        streakCode != nil || (lastTenW != nil && lastTenL != nil)
    }

    private var streakLine: some View {
        HStack(spacing: 12) {
            if let w = lastTenW, let l = lastTenL {
                pill(label: "L10", value: "\(w)-\(l)")
            }
            if let code = streakCode, !code.isEmpty {
                pill(
                    label: "STREAK",
                    value: code,
                    tint: code.hasPrefix("W") ? .green : .red,
                )
            }
            Spacer(minLength: 0)
        }
    }

    private func pill(label: String, value: String, tint: Color? = nil) -> some View {
        HStack(spacing: 4) {
            Text(label)
                .font(.caption2.weight(.bold))
                .tracking(0.5)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.caption.weight(.bold))
                .monospacedDigit()
                .foregroundStyle(tint ?? .primary)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(
            Capsule().fill(Color(.systemFill).opacity(0.5))
        )
    }

    private var recordText: String {
        guard let w = record?.wins, let l = record?.losses else { return "—" }
        return "\(w)-\(l)"
    }

    private var divisionText: String {
        standing?.displayString ?? "—"
    }

    @ViewBuilder
    private var lastGameRow: some View {
        if let last = lastGame {
            let didWin = HomeGameUtils.favoriteWon(game: last, favoriteBDLId: entry.bdlTeamId)
            HStack(spacing: 10) {
                resultBadge(text: didWin ? "W" : "L", color: didWin ? .green : .red)
                Text(HomeGameUtils.lastGameLine(game: last, favoriteBDLId: entry.bdlTeamId))
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.primary)
                Spacer()
                Text(HomeGameUtils.shortRelativeDate(game: last))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        } else {
            Text("No recent games")
                .font(.subheadline)
                .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private var nextGameRow: some View {
        if let next = nextGame {
            HStack(spacing: 10) {
                Image(systemName: next.phase == .live ? "dot.radiowaves.left.and.right" : "calendar")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(next.phase == .live ? .red : teamColor)
                    .frame(width: 26, height: 26)
                    .background(
                        Circle().fill(
                            (next.phase == .live ? Color.red : teamColor).opacity(0.12)
                        )
                    )
                Text(HomeGameUtils.nextGameLine(game: next, favoriteBDLId: entry.bdlTeamId))
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                Spacer()
            }
        } else {
            Text("Season schedule TBA")
                .font(.subheadline)
                .foregroundStyle(.secondary)
        }
    }

    /// Compact horizontal scroll of recent/upcoming games (live or
    /// final or upcoming). On first appearance, scrolls to the
    /// most relevant card (live → next upcoming → most recent
    /// final). Tapping a card pushes the box-score destination on
    /// the parent's nav stack.
    private var gameStrip: some View {
        ScrollViewReader { proxy in
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(stripGames) { game in
                        Button { onTapStripGame(game) } label: {
                            CompactGameStripCard(
                                game:          game,
                                favoriteBDLId: entry.bdlTeamId,
                                tint:          teamColor,
                            )
                        }
                        .buttonStyle(.plain)
                        .id(game.id)
                    }
                }
            }
            .onAppear {
                if let target = anchorGame() {
                    withAnimation(.easeOut(duration: 0.25)) {
                        proxy.scrollTo(target.id, anchor: .center)
                    }
                }
            }
        }
    }

    private func anchorGame() -> Game? {
        if let live = stripGames.first(where: { $0.phase == .live }) { return live }
        let today = Calendar.current.startOfDay(for: Date())
        if let upcoming = stripGames.first(where: { ($0.startDate ?? .distantPast) >= today }) {
            return upcoming
        }
        return stripGames.last
    }

    private var scheduleLink: some View {
        HStack {
            Spacer()
            Button(action: onSchedule) {
                HStack(spacing: 3) {
                    Text("Full Schedule")
                    Image(systemName: "chevron.right")
                        .font(.caption2.weight(.bold))
                }
                .font(.caption.weight(.semibold))
                .foregroundStyle(teamColor)
            }
        }
    }

    private func resultBadge(text: String, color: Color) -> some View {
        Text(text)
            .font(.caption.weight(.bold))
            .foregroundStyle(.white)
            .frame(width: 26, height: 26)
            .background(Circle().fill(color))
    }
}

// MARK: - Compact game strip card (embedded in hero card)

/// 110×100 strip card that lives inside the hero card. Smaller than
/// the original standalone strip card — the embedded context means
/// the team logo and broader info are already on screen, so each
/// card just needs to communicate opponent + result + when.
private struct CompactGameStripCard: View {
    let game: Game
    let favoriteBDLId: Int
    let tint: Color

    private var opponent: GameTeam {
        if game.bdlHomeTeamId == favoriteBDLId {
            return game.teams.away
        }
        return game.teams.home
    }

    private var isHomeGame: Bool {
        game.bdlHomeTeamId == favoriteBDLId
    }

    var body: some View {
        VStack(spacing: 4) {
            HStack(spacing: 3) {
                Text(isHomeGame ? "vs" : "@")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.secondary)
                TeamLogoView(team: opponent.team, size: 24)
                Text(opponent.team.abbreviation ?? String(opponent.team.name.prefix(3)).uppercased())
                    .font(.caption.weight(.semibold))
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
            }
            bodyByPhase
        }
        .padding(.vertical, 8)
        .padding(.horizontal, 8)
        .frame(width: 110, height: 100)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(.thinMaterial)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(borderColor, lineWidth: game.phase == .live ? 1.2 : 0.5)
        )
    }

    private var borderColor: Color {
        game.phase == .live ? .red.opacity(0.55) : Color.gray.opacity(0.25)
    }

    @ViewBuilder
    private var bodyByPhase: some View {
        switch game.phase {
        case .final:    finalContent
        case .live:     liveContent
        case .preview, .other: upcomingContent
        }
    }

    private var finalContent: some View {
        let didWin = HomeGameUtils.favoriteWon(game: game, favoriteBDLId: favoriteBDLId)
        let (favScore, oppScore) = HomeGameUtils.scores(game: game, favoriteBDLId: favoriteBDLId)
        return VStack(spacing: 2) {
            Text("\(favScore ?? 0)-\(oppScore ?? 0)")
                .font(.subheadline.weight(.bold))
                .foregroundStyle(.primary)
                .monospacedDigit()
            Text(didWin ? "W" : "L")
                .font(.caption2.weight(.bold))
                .foregroundStyle(.white)
                .padding(.horizontal, 6)
                .padding(.vertical, 1)
                .background(Capsule().fill(didWin ? Color.green : Color.red))
            Text(HomeGameUtils.shortRelativeDate(game: game))
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
    }

    private var liveContent: some View {
        let (favScore, oppScore) = HomeGameUtils.scores(game: game, favoriteBDLId: favoriteBDLId)
        return VStack(spacing: 2) {
            Text("\(favScore ?? 0)-\(oppScore ?? 0)")
                .font(.subheadline.weight(.bold))
                .foregroundStyle(.primary)
                .monospacedDigit()
            HStack(spacing: 3) {
                Circle().fill(.red).frame(width: 5, height: 5)
                Text("LIVE")
                    .font(.caption2.weight(.bold))
                    .foregroundStyle(.red)
            }
            Text(HomeGameUtils.inningLine(game: game))
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
    }

    private var upcomingContent: some View {
        VStack(spacing: 2) {
            Text(HomeGameUtils.localTime(game: game))
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.primary)
                .monospacedDigit()
            Text(HomeGameUtils.timezoneAbbreviation())
                .font(.caption2)
                .foregroundStyle(.secondary)
            Text(HomeGameUtils.shortRelativeDate(game: game))
                .font(.caption2.weight(.semibold))
                .foregroundStyle(tint)
        }
    }
}

// MARK: - Section header (shared)

private struct HomeSectionHeader<Trailing: View>: View {
    let title: String
    let tint: Color
    @ViewBuilder let trailing: Trailing

    var body: some View {
        HStack(spacing: 10) {
            Capsule()
                .fill(tint)
                .frame(width: 4, height: 18)
            Text(title)
                .font(.title3.weight(.bold))
            Spacer()
            trailing
        }
        .padding(.horizontal, 16)
    }
}

extension HomeSectionHeader where Trailing == EmptyView {
    init(title: String, tint: Color) {
        self.init(title: title, tint: tint) { EmptyView() }
    }
}

// MARK: - Shared game-projection helpers

enum HomeGameUtils {
    static func favoriteWon(game: Game, favoriteBDLId: Int) -> Bool {
        let favScore = (game.bdlHomeTeamId == favoriteBDLId)
            ? game.teams.home.score : game.teams.away.score
        let oppScore = (game.bdlHomeTeamId == favoriteBDLId)
            ? game.teams.away.score : game.teams.home.score
        guard let f = favScore, let o = oppScore else { return false }
        return f > o
    }

    static func scores(game: Game, favoriteBDLId: Int) -> (Int?, Int?) {
        if game.bdlHomeTeamId == favoriteBDLId {
            return (game.teams.home.score, game.teams.away.score)
        }
        return (game.teams.away.score, game.teams.home.score)
    }

    static func opponentAbbr(game: Game, favoriteBDLId: Int) -> String {
        let opp = (game.bdlHomeTeamId == favoriteBDLId) ? game.teams.away : game.teams.home
        return opp.team.abbreviation ?? String(opp.team.name.prefix(3)).uppercased()
    }

    static func lastGameLine(game: Game, favoriteBDLId: Int) -> String {
        let (fav, opp) = scores(game: game, favoriteBDLId: favoriteBDLId)
        let prefix = (game.bdlHomeTeamId == favoriteBDLId) ? "vs" : "@"
        return "\(fav ?? 0)-\(opp ?? 0) \(prefix) \(opponentAbbr(game: game, favoriteBDLId: favoriteBDLId))"
    }

    static func nextGameLine(game: Game, favoriteBDLId: Int) -> String {
        let venue  = (game.bdlHomeTeamId == favoriteBDLId) ? "vs" : "@"
        let opp    = opponentAbbr(game: game, favoriteBDLId: favoriteBDLId)
        switch game.phase {
        case .live:
            return "\(venue) \(opp) · \(inningLine(game: game))"
        case .preview, .other:
            let when = relativeDayLabel(game: game)
            let time = localTime(game: game)
            return "\(when) \(time) \(timezoneAbbreviation()) \(venue) \(opp)"
        case .final:
            return "Recent · \(venue) \(opp)"
        }
    }

    static func relativeDayLabel(game: Game) -> String {
        guard let start = game.startDate else { return "Soon" }
        let cal = Calendar.current
        if cal.isDateInToday(start)    { return "Tonight" }
        if cal.isDateInTomorrow(start) { return "Tomorrow" }
        return shortDateFormatter.string(from: start)
    }

    static func shortRelativeDate(game: Game) -> String {
        guard let start = game.startDate else { return "—" }
        let cal = Calendar.current
        if cal.isDateInToday(start)     { return "Today" }
        if cal.isDateInYesterday(start) { return "Yesterday" }
        if cal.isDateInTomorrow(start)  { return "Tomorrow" }
        return shortDateFormatter.string(from: start)
    }

    static func localTime(game: Game) -> String {
        guard let start = game.startDate else { return "—" }
        return timeFormatter.string(from: start)
    }

    static func inningLine(game: Game) -> String {
        guard let l = game.linescore, let inning = l.currentInning else { return "Live" }
        let state = (l.inningState ?? "").lowercased()
        let prefix = state.hasPrefix("bot") ? "Bot" : "Top"
        return "\(prefix) \(inning)"
    }

    static func timezoneAbbreviation() -> String {
        TimeZone.current.abbreviation() ?? ""
    }

    private static let timeFormatter: DateFormatter = {
        let f = DateFormatter()
        f.timeZone = .current
        f.dateFormat = "h:mm a"
        return f
    }()

    private static let shortDateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.timeZone = .current
        f.dateFormat = "EEE MMM d"
        return f
    }()
}

// MARK: - Team Leaders Section (compact card)

/// Compact in-card team leaders: batting/pitching segmented toggle
/// at the top, four stat subsections (HOME RUNS, BATTING AVG, …)
/// each with the top three players on the favorite team. Tapping a
/// row navigates to the player profile; "See All Stats ›" opens
/// the deeper sheet with the full per-category list.
private struct TeamLeadersSection: View {
    let leaders: TeamLeaders?
    let isLoading: Bool
    let tint: Color
    let onSeeAll: () -> Void
    let onTapPlayer: (PlayerSearchResult) -> Void

    enum Role: String, Hashable { case batting, pitching }

    @State private var role: Role = .batting

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HomeSectionHeader(title: "Team Leaders", tint: tint) {
                Button(action: onSeeAll) {
                    HStack(spacing: 3) {
                        Text("See All Stats")
                        Image(systemName: "chevron.right")
                            .font(.caption2.weight(.bold))
                    }
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(tint)
                }
            }

            VStack(spacing: 12) {
                Picker("Role", selection: $role) {
                    Text("Batting").tag(Role.batting)
                    Text("Pitching").tag(Role.pitching)
                }
                .pickerStyle(.segmented)

                if isLoading && leaders == nil {
                    loadingRows
                } else {
                    let groups = (role == .batting ? leaders?.batting : leaders?.pitching) ?? []
                    if groups.isEmpty {
                        Text("No leaders yet")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .padding(.vertical, 12)
                            .frame(maxWidth: .infinity)
                    } else {
                        VStack(alignment: .leading, spacing: 14) {
                            ForEach(groups) { group in
                                statGroupSection(group)
                            }
                        }
                    }
                }
            }
            .padding(14)
            // Pure glass — the team-color background gradient bleeds
            // through the material for a naturally tinted surface.
            .background(
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .fill(.ultraThinMaterial)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .strokeBorder(tint.opacity(0.25), lineWidth: 1)
            )
            .shadow(color: .black.opacity(0.05), radius: 6, x: 0, y: 2)
            .padding(.horizontal, 16)
        }
    }

    private var loadingRows: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(0..<4, id: \.self) { _ in
                RoundedRectangle(cornerRadius: 4)
                    .fill(Color(.systemGray5))
                    .frame(height: 14)
                    .frame(maxWidth: .infinity, alignment: .leading)
                ForEach(0..<3, id: \.self) { _ in
                    HStack(spacing: 10) {
                        Circle()
                            .fill(Color(.systemGray5))
                            .frame(width: 28, height: 28)
                        RoundedRectangle(cornerRadius: 4)
                            .fill(Color(.systemGray5))
                            .frame(height: 12)
                        Spacer()
                        RoundedRectangle(cornerRadius: 4)
                            .fill(Color(.systemGray5))
                            .frame(width: 36, height: 12)
                    }
                }
            }
        }
    }

    @ViewBuilder
    private func statGroupSection(_ group: StatLeaderGroup) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(statDisplayName(group.stat))
                .font(.caption2.weight(.bold))
                .tracking(0.5)
                .foregroundStyle(.secondary)
            VStack(spacing: 4) {
                ForEach(Array(group.cards.enumerated()), id: \.offset) { idx, card in
                    Button {
                        onTapPlayer(card.player)
                    } label: {
                        leaderRow(rank: idx + 1, card: card)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    private func leaderRow(rank: Int, card: LeaderCard) -> some View {
        HStack(spacing: 10) {
            Text("#\(rank)")
                .font(.caption.weight(.bold))
                .monospacedDigit()
                .foregroundStyle(rank == 1 ? tint : .secondary)
                .frame(width: 24, alignment: .leading)
            Text(card.player.name)
                .font(.subheadline.weight(.medium))
                .lineLimit(1)
                .minimumScaleFactor(0.8)
            Spacer(minLength: 8)
            Text(formatLeaderValue(card.value, stat: card.stat))
                .font(.subheadline.weight(.bold))
                .monospacedDigit()
                .foregroundStyle(.primary)
        }
        .contentShape(Rectangle())
    }
}

/// Long-form display name for the compact leader section headers.
/// Falls through to uppercased stat key for anything not in the
/// curated list (so future additions don't crash; they just get
/// a less-pretty label).
private func statDisplayName(_ stat: String) -> String {
    switch stat {
    case "AVG":  return "BATTING AVG"
    case "HR":   return "HOME RUNS"
    case "RBI":  return "RBI"
    case "OPS":  return "OPS"
    case "ERA":  return "ERA"
    case "W":    return "WINS"
    case "SO":   return "STRIKEOUTS"
    case "WHIP": return "WHIP"
    default:     return stat.uppercased()
    }
}

/// Shared value formatter for the compact leader rows, the roster
/// strip/sheet, and `TeamLeadersSheet`'s static helper. Returns
/// "—" for nil, formats batting rates without the leading zero,
/// pitcher rates to two decimals, IP in baseball outs notation, and
/// every counting stat as a plain integer. Module-internal so
/// other Home-tab files (`RosterSheet`) can reuse it.
func formatLeaderValue(_ v: Double?, stat: String) -> String {
    guard let v else { return "—" }
    switch stat {
    case "AVG", "OBP", "SLG", "OPS":
        let s = String(format: "%.3f", v)
        if s.hasPrefix("0.")  { return String(s.dropFirst()) }
        if s.hasPrefix("-0.") { return "-" + String(s.dropFirst(2)) }
        return s
    case "ERA", "WHIP", "FIP":
        return String(format: "%.2f", v)
    case "HR", "RBI", "SO", "W", "L", "SV", "H", "BB", "R", "SB",
         "2B", "3B", "CG", "SHO":
        return String(Int(v.rounded()))
    case "IP":
        let whole = Int(v.rounded(.down))
        let frac  = v - Double(whole)
        let outs: String
        if      frac < 0.17 { outs = ".0" }
        else if frac < 0.5  { outs = ".1" }
        else                { outs = ".2" }
        return "\(whole)\(outs)"
    default:
        return String(format: "%.1f", v)
    }
}

// MARK: - Roster Section (header-only entry point)

/// Header-only entry point for the active-roster surface. Renders a
/// `HomeSectionHeader` with a trailing "See All ›" button that opens
/// `RosterSheet` — the sheet does the actual table rendering. We
/// dropped the inline preview cards: they didn't add information
/// the section header doesn't already imply.
private struct RosterSection: View {
    let roster: [RosterPlayer]
    let isLoading: Bool
    let tint: Color
    let onSeeAll: () -> Void

    var body: some View {
        HomeSectionHeader(title: "Roster", tint: tint) {
            Button(action: onSeeAll) {
                HStack(spacing: 3) {
                    Text("See All")
                    Image(systemName: "chevron.right")
                        .font(.caption2.weight(.bold))
                }
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(tint)
            }
        }
    }
}

// MARK: - Team History Section (header-only entry point)

/// Header-only section for the franchise's season-by-season history.
/// Tapping "See All ›" opens `TeamHistorySheet` — there's no inline
/// preview here because the history table only reads well at the
/// sheet's full width, and a truncated single-row preview adds
/// noise without information.
private struct TeamHistorySection: View {
    let tint: Color
    let onSeeAll: () -> Void

    var body: some View {
        HomeSectionHeader(title: "History", tint: tint) {
            Button(action: onSeeAll) {
                HStack(spacing: 3) {
                    Text("See All")
                    Image(systemName: "chevron.right")
                        .font(.caption2.weight(.bold))
                }
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(tint)
            }
        }
    }
}

// MARK: - Injury Report Section (header-only entry point)

/// Header-only entry point matching the Roster + History sections.
/// Renders `HomeSectionHeader`-style chrome inline (so the count can
/// sit next to the title in a separate `.secondary` color) plus a
/// trailing "See All ›" button. The caller hides this section
/// entirely when there are no injuries.
private struct InjuryReportSection: View {
    let count: Int
    let tint: Color
    let onSeeAll: () -> Void

    var body: some View {
        HStack(spacing: 10) {
            Capsule()
                .fill(tint)
                .frame(width: 4, height: 18)
            Text("Injury Report")
                .font(.title3.weight(.bold))
            Text("(\(count))")
                .font(.title3.weight(.bold))
                .foregroundStyle(.secondary)
                .monospacedDigit()
            Spacer()
            Button(action: onSeeAll) {
                HStack(spacing: 3) {
                    Text("See All")
                    Image(systemName: "chevron.right")
                        .font(.caption2.weight(.bold))
                }
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(tint)
            }
        }
        .padding(.horizontal, 16)
    }
}

// MARK: - Favorite Players Section

private struct FavoritePlayersSection: View {
    let favorites: [FavoritePlayerDisplay]
    let isLoading: Bool
    @Binding var isEditing: Bool
    let tint: Color
    let onAdd: () -> Void
    let onRemove: (Int) -> Void
    let onTapPlayer: (PlayerSearchResult) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HomeSectionHeader(title: "My Players", tint: tint) {
                trailingControl
            }
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 10) {
                    if isLoading && favorites.isEmpty {
                        ForEach(0..<3, id: \.self) { _ in
                            FavoritePlayerTile.placeholder
                        }
                    }
                    ForEach(favorites) { fav in
                        favoriteCard(fav)
                    }
                    AddFavoriteTile(onAdd: onAdd)
                }
                .padding(.horizontal, 16)
            }
        }
    }

    @ViewBuilder
    private var trailingControl: some View {
        if !favorites.isEmpty {
            Button(isEditing ? "Done" : "Edit") {
                withAnimation(.easeInOut(duration: 0.15)) {
                    isEditing.toggle()
                }
            }
            .font(.subheadline.weight(.semibold))
        }
    }

    @ViewBuilder
    private func favoriteCard(_ fav: FavoritePlayerDisplay) -> some View {
        if isEditing {
            FavoritePlayerTile(fav: fav, showRemoveBadge: true, onRemove: {
                onRemove(fav.player.player_id)
            })
        } else {
            Button { onTapPlayer(fav.player) } label: {
                FavoritePlayerTile(fav: fav, showRemoveBadge: false, onRemove: {})
            }
            .buttonStyle(.plain)
        }
    }
}

private struct FavoritePlayerTile: View {
    let fav: FavoritePlayerDisplay
    let showRemoveBadge: Bool
    let onRemove: () -> Void

    var body: some View {
        ZStack(alignment: .topLeading) {
            VStack(spacing: 6) {
                headshot
                Text(displayName)
                    .font(.subheadline.weight(.semibold))
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
                Text(positionLine)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                Text(fav.statLine)
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.75)
                    .padding(.top, 2)
            }
            .padding(.vertical, 10)
            .padding(.horizontal, 8)
            .frame(width: 132, height: 156)
            .background(
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .fill(.ultraThinMaterial)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .strokeBorder(.quaternary, lineWidth: 0.5)
            )

            if showRemoveBadge {
                Button(action: onRemove) {
                    Image(systemName: "minus.circle.fill")
                        .symbolRenderingMode(.palette)
                        .foregroundStyle(.white, Color.red)
                        .font(.system(size: 22))
                        .padding(4)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .offset(x: -6, y: -6)
            }
        }
    }

    static var placeholder: some View {
        VStack(spacing: 6) {
            Circle().fill(Color(.systemGray5)).frame(width: 50, height: 50)
            RoundedRectangle(cornerRadius: 4).fill(Color(.systemGray5)).frame(width: 80, height: 12)
            RoundedRectangle(cornerRadius: 4).fill(Color(.systemGray5)).frame(width: 56, height: 10)
            RoundedRectangle(cornerRadius: 4).fill(Color(.systemGray5)).frame(width: 100, height: 10)
        }
        .padding(.vertical, 10)
        .padding(.horizontal, 8)
        .frame(width: 132, height: 156)
        .background(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .fill(.ultraThinMaterial)
        )
    }

    private var headshot: some View {
        AsyncImage(url: fav.player.largeHeadshotURL) { phase in
            switch phase {
            case .success(let image): image.resizable().scaledToFill()
            default:
                Image(systemName: "person.crop.circle.fill")
                    .resizable().scaledToFit()
                    .foregroundStyle(.tertiary)
            }
        }
        .frame(width: 50, height: 50)
        .background(Circle().fill(.ultraThinMaterial))
        .clipShape(Circle())
        .overlay(Circle().strokeBorder(.quaternary, lineWidth: 0.5))
    }

    private var displayName: String {
        let parts = fav.player.name.split(separator: " ", maxSplits: 1)
        guard let first = parts.first, parts.count > 1 else {
            return fav.player.name
        }
        let last = lastNameWithSuffix(fav.player.name)
        return "\(first.prefix(1)). \(last)"
    }

    private var positionLine: String {
        let pos = fav.player.position.flatMap { $0.isEmpty ? nil : $0 }
        let team = fav.player.teamCode.flatMap { $0.isEmpty ? nil : teamAbbreviation(for: $0) }
        switch (pos, team) {
        case let (p?, t?): return "\(p) · \(t)"
        case let (p?, nil): return p
        case let (nil, t?): return t
        default: return "—"
        }
    }
}

private struct AddFavoriteTile: View {
    let onAdd: () -> Void

    var body: some View {
        Button(action: onAdd) {
            VStack(spacing: 8) {
                Image(systemName: "plus.circle.fill")
                    .font(.system(size: 36))
                    .foregroundStyle(Color.accentColor)
                Text("Add Player")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.primary)
            }
            .frame(width: 132, height: 156)
            .background(
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .fill(.ultraThinMaterial)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .strokeBorder(
                        Color.accentColor.opacity(0.35),
                        style: StrokeStyle(lineWidth: 1, dash: [4, 4]),
                    )
            )
        }
        .buttonStyle(.plain)
    }
}

#Preview {
    HomeView()
}
