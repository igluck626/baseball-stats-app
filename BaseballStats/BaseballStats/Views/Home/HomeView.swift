//
//  HomeView.swift
//  BaseballStats
//
//  Home tab root. Two states:
//  - No favorite team yet → embedded TeamPickerView.
//  - Favorite team set → hero card + recent/upcoming game strip.
//
//  Hero card surfaces the team's record + division standing + last
//  result + next game. Strip is a horizontal scroll of up to 7
//  cards spanning recent finals through the next handful of
//  upcoming games, auto-scrolled so today's game (or the most
//  recent one) sits roughly in the center on first paint.
//
//  Navigation: tapping any strip card pushes a BoxScoreView via
//  the same `.navigationDestination(for: Game.self)` pattern the
//  Scores tab uses.
//

import SwiftUI

struct HomeView: View {
    @StateObject private var vm = HomeViewModel()
    @ObservedObject private var store = FavoriteTeamStore.shared
    @ObservedObject private var favoritesStore = FavoritePlayersStore.shared
    @State private var navigationPath = NavigationPath()
    @State private var showingPicker = false
    @State private var showingAddPlayer = false
    @State private var isEditingFavorites = false

    var body: some View {
        NavigationStack(path: $navigationPath) {
            ZStack {
                backgroundGradient
                content
            }
            // No nav title — the hero card is the visual anchor for
            // this tab. Inline display mode keeps the bar collapsed so
            // the toolbar slot stays available for future controls
            // without occupying vertical space.
            .navigationBarTitleDisplayMode(.inline)
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
        }
        .task(id: store.bdlTeamId) {
            // Re-runs whenever the favorite changes — initial mount,
            // first-time pick, or change-via-settings all funnel
            // through here.
            guard let bdlId = store.bdlTeamId else { return }
            await vm.load(bdlTeamId: bdlId)
            await vm.loadTeamLeaders(bdlTeamId: bdlId)
            vm.startAutoRefresh(bdlTeamId: bdlId)
        }
        .task(id: favoritesStore.playerIds) {
            // Re-runs whenever the favorites list changes — initial
            // mount or any add/remove via the sheet / edit mode.
            await vm.loadFavoritePlayers(ids: favoritesStore.playerIds)
        }
        .onDisappear { vm.stopAutoRefresh() }
    }

    // MARK: - Chrome

    private var backgroundGradient: some View {
        LinearGradient(
            colors: [Color(.systemGray6), Color(.systemBackground)],
            startPoint: .top, endPoint: .bottom,
        )
        .ignoresSafeArea()
    }

    @ViewBuilder
    private var content: some View {
        if store.bdlTeamId == nil {
            // First-launch / cleared-favorite state. Embedding the
            // picker inline (rather than sheet-only) avoids an extra
            // tap before the user can do anything on the tab.
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
        ScrollView {
            VStack(spacing: 16) {
                TeamHeroCard(
                    entry:        entry,
                    record:       vm.teamRecord,
                    standing:     vm.teamStanding,
                    lastGame:     vm.lastGame,
                    nextGame:     vm.nextGame,
                    onSettings:   { showingPicker = true },
                    onSchedule:   {},   // Phase-1 stub
                )

                RecentUpcomingStrip(
                    games:    vm.recentAndUpcoming,
                    favorite: entry,
                    onTap:    { game in navigationPath.append(game) },
                )

                TeamLeadersSection(
                    leaders:     vm.teamLeaders,
                    isLoading:   vm.isLoadingLeaders,
                    onTapPlayer: { player in
                        navigationPath.append(player)
                    },
                )

                FavoritePlayersSection(
                    favorites:    vm.favoritePlayers,
                    isLoading:    vm.isLoadingFavorites,
                    isEditing:    $isEditingFavorites,
                    onAdd:        { showingAddPlayer = true },
                    onRemove:     { id in favoritesStore.remove(id) },
                    onTapPlayer:  { player in
                        navigationPath.append(player)
                    },
                )
            }
            // Larger top inset now that the nav title is gone — gives
            // the hero card breathing room from the status bar's
            // safe-area edge instead of sitting flush against it.
            .padding(.top, 20)
            .padding(.bottom, 12)
        }
        .refreshable {
            if let bdlId = store.bdlTeamId {
                await vm.load(bdlTeamId: bdlId)
            }
        }
    }

    private var loadingSkeleton: some View {
        VStack(spacing: 16) {
            RoundedRectangle(cornerRadius: 18)
                .fill(Color(.systemGray5))
                .frame(height: 220)
                .padding(.horizontal, 16)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 12) {
                    ForEach(0..<5, id: \.self) { _ in
                        RoundedRectangle(cornerRadius: 14)
                            .fill(Color(.systemGray5))
                            .frame(width: 132, height: 132)
                    }
                }
                .padding(.horizontal, 16)
            }
            Spacer()
        }
        .padding(.top, 12)
    }
}

// MARK: - Team Hero Card

/// The big card at the top of the Home tab. Logo + record on top,
/// last result + next game on the bottom, settings gear in the
/// top-right corner.
private struct TeamHeroCard: View {
    let entry: MLBTeamCatalog.Entry
    let record: TeamRecord?
    let standing: TeamStandingInfo?
    let lastGame: Game?
    let nextGame: Game?
    let onSettings: () -> Void
    let onSchedule: () -> Void

    private var teamColor: Color {
        TeamColors.color(for: entry.lahmanCode) ?? Color.accentColor
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            header
            Divider().opacity(0.4)
            lastGameRow
            nextGameRow
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
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
        HStack(alignment: .center, spacing: 14) {
            TeamLogoView(team: entry.teamInfo, size: 72)

            VStack(alignment: .leading, spacing: 4) {
                Text(entry.fullName)
                    .font(.title3.weight(.bold))
                    .lineLimit(1)
                    .minimumScaleFactor(0.85)
                Text(recordAndStandingLine)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }

            Spacer(minLength: 4)

            VStack(alignment: .trailing, spacing: 8) {
                Button(action: onSettings) {
                    Image(systemName: "gear")
                        .font(.system(size: 17, weight: .semibold))
                        .foregroundStyle(.secondary)
                        .padding(6)
                        .contentShape(Rectangle())
                }
                Button(action: onSchedule) {
                    HStack(spacing: 2) {
                        Text("Schedule")
                        Image(systemName: "chevron.right")
                            .font(.caption2.weight(.bold))
                    }
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(teamColor)
                }
            }
        }
    }

    /// "(32-19) · 1st NL West" — collapses gracefully when either
    /// piece is missing (early-spring / standings fetch failed).
    private var recordAndStandingLine: String {
        let recordPart: String? = {
            guard let w = record?.wins, let l = record?.losses else { return nil }
            return "(\(w)-\(l))"
        }()
        let standingPart = standing?.displayString
        switch (recordPart, standingPart) {
        case let (r?, s?): return "\(r) · \(s)"
        case let (r?, nil): return r
        case let (nil, s?): return s
        default: return "—"
        }
    }

    @ViewBuilder
    private var lastGameRow: some View {
        if let last = lastGame {
            let didWin = HomeGameUtils.favoriteWon(game: last, favoriteBDLId: entry.bdlTeamId)
            HStack(spacing: 8) {
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
            HStack(spacing: 8) {
                Image(systemName: next.phase == .live ? "dot.radiowaves.left.and.right" : "calendar")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(next.phase == .live ? .red : teamColor)
                    .frame(width: 22)
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

    private func resultBadge(text: String, color: Color) -> some View {
        Text(text)
            .font(.caption.weight(.bold))
            .foregroundStyle(.white)
            .frame(width: 22, height: 22)
            .background(Circle().fill(color))
    }
}

// MARK: - Recent/Upcoming Strip

private struct RecentUpcomingStrip: View {
    let games: [Game]
    let favorite: MLBTeamCatalog.Entry
    let onTap: (Game) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Recent & Upcoming")
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.secondary)
                .padding(.horizontal, 16)

            if games.isEmpty {
                Text("No games in window")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 16)
            } else {
                ScrollViewReader { proxy in
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: 10) {
                            ForEach(games) { game in
                                Button { onTap(game) } label: {
                                    GameStripCard(
                                        game:          game,
                                        favoriteBDLId: favorite.bdlTeamId,
                                    )
                                }
                                .buttonStyle(.plain)
                                .id(game.id)
                            }
                        }
                        .padding(.horizontal, 16)
                    }
                    .onAppear {
                        // Center on the most relevant card: live game
                        // if any, else the first game whose date is
                        // today or future, else the latest final.
                        if let target = anchorGame() {
                            withAnimation(.easeOut(duration: 0.25)) {
                                proxy.scrollTo(target.id, anchor: .center)
                            }
                        }
                    }
                }
            }
        }
    }

    private func anchorGame() -> Game? {
        if let live = games.first(where: { $0.phase == .live }) { return live }
        let today = Calendar.current.startOfDay(for: Date())
        if let upcoming = games.first(where: { ($0.startDate ?? .distantPast) >= today }) {
            return upcoming
        }
        return games.last
    }
}

// MARK: - Game Strip Card

private struct GameStripCard: View {
    let game: Game
    let favoriteBDLId: Int

    private var opponent: GameTeam {
        // The non-favorite side is the opponent. `bdl{Away,Home}TeamId`
        // gives us the authoritative BDL ids on the synthesized Game.
        if game.bdlHomeTeamId == favoriteBDLId {
            return game.teams.away
        }
        return game.teams.home
    }

    private var isHomeGame: Bool {
        game.bdlHomeTeamId == favoriteBDLId
    }

    private var teamColor: Color {
        guard let lahman = bdlToLahmanTeamId[favoriteBDLId] else { return .accentColor }
        return TeamColors.color(for: lahman) ?? .accentColor
    }

    var body: some View {
        VStack(spacing: 6) {
            HStack(spacing: 4) {
                Text(isHomeGame ? "vs" : "@")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.secondary)
                TeamLogoView(team: opponent.team, size: 28)
                Text(opponent.team.abbreviation ?? String(opponent.team.name.prefix(3)).uppercased())
                    .font(.caption.weight(.semibold))
                    .lineLimit(1)
            }
            .frame(maxWidth: .infinity)

            bodyByPhase
        }
        .padding(.vertical, 10)
        .padding(.horizontal, 10)
        .frame(width: 132, height: 132)
        .background(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .fill(.ultraThinMaterial)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .strokeBorder(borderColor, lineWidth: game.phase == .live ? 1.5 : 0.5)
        )
        .shadow(color: .black.opacity(0.05), radius: 4, x: 0, y: 2)
    }

    private var borderColor: Color {
        game.phase == .live ? .red.opacity(0.5) : Color.gray.opacity(0.25)
    }

    @ViewBuilder
    private var bodyByPhase: some View {
        switch game.phase {
        case .final:
            finalContent
        case .live:
            liveContent
        case .preview, .other:
            upcomingContent
        }
    }

    private var finalContent: some View {
        let didWin = HomeGameUtils.favoriteWon(game: game, favoriteBDLId: favoriteBDLId)
        let (favScore, oppScore) = HomeGameUtils.scores(game: game, favoriteBDLId: favoriteBDLId)
        return VStack(spacing: 4) {
            Text(didWin ? "W" : "L")
                .font(.caption2.weight(.bold))
                .foregroundStyle(.white)
                .padding(.horizontal, 8)
                .padding(.vertical, 2)
                .background(Capsule().fill(didWin ? Color.green : Color.red))
            Text("\(favScore ?? 0)-\(oppScore ?? 0)")
                .font(.title3.weight(didWin ? .bold : .semibold))
                .foregroundStyle(didWin ? .primary : .secondary)
                .monospacedDigit()
            Text(HomeGameUtils.shortRelativeDate(game: game))
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
    }

    private var liveContent: some View {
        let (favScore, oppScore) = HomeGameUtils.scores(game: game, favoriteBDLId: favoriteBDLId)
        return VStack(spacing: 4) {
            HStack(spacing: 4) {
                Circle()
                    .fill(.red)
                    .frame(width: 6, height: 6)
                Text("LIVE")
                    .font(.caption2.weight(.bold))
                    .foregroundStyle(.red)
            }
            Text("\(favScore ?? 0)-\(oppScore ?? 0)")
                .font(.title3.weight(.bold))
                .foregroundStyle(.primary)
                .monospacedDigit()
            Text(HomeGameUtils.inningLine(game: game))
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
    }

    private var upcomingContent: some View {
        VStack(spacing: 4) {
            Text(HomeGameUtils.shortRelativeDate(game: game))
                .font(.caption.weight(.semibold))
                .foregroundStyle(teamColor)
            Text(HomeGameUtils.localTime(game: game))
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.primary)
                .monospacedDigit()
            Text(HomeGameUtils.timezoneAbbreviation())
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
    }
}

// MARK: - Shared game-projection helpers

/// Display helpers shared between the hero card and the strip
/// cards. Anchored on the favorite's BDL id so the same routine
/// can answer "who's the opponent" / "did we win" / etc. without
/// each call site duplicating the home/away branching.
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

    /// "W 5-4 vs SD" / "L 2-7 @ LAA" for a final game.
    static func lastGameLine(game: Game, favoriteBDLId: Int) -> String {
        let (fav, opp) = scores(game: game, favoriteBDLId: favoriteBDLId)
        let prefix = (game.bdlHomeTeamId == favoriteBDLId) ? "vs" : "@"
        return "\(fav ?? 0)-\(opp ?? 0) \(prefix) \(opponentAbbr(game: game, favoriteBDLId: favoriteBDLId))"
    }

    /// "Tonight 7:10 PM PT vs COL" / "Tomorrow 1:10 PM PT @ COL" /
    /// "Live · vs COL · Top 5".
    static func nextGameLine(game: Game, favoriteBDLId: Int) -> String {
        let venue  = (game.bdlHomeTeamId == favoriteBDLId) ? "vs" : "@"
        let opp    = opponentAbbr(game: game, favoriteBDLId: favoriteBDLId)
        switch game.phase {
        case .live:
            return "Live · \(venue) \(opp) · \(inningLine(game: game))"
        case .preview, .other:
            let when = relativeDayLabel(game: game)
            let time = localTime(game: game)
            return "\(when) \(time) \(timezoneAbbreviation()) \(venue) \(opp)"
        case .final:
            return "Recent · \(venue) \(opp)"
        }
    }

    /// "Today" / "Tomorrow" / "Mon May 18" — used by the hero
    /// row's next-game line. Falls back to "Soon" when the game's
    /// startDate can't be parsed.
    static func relativeDayLabel(game: Game) -> String {
        guard let start = game.startDate else { return "Soon" }
        let cal = Calendar.current
        if cal.isDateInToday(start)    { return "Tonight" }
        if cal.isDateInTomorrow(start) { return "Tomorrow" }
        return shortDateFormatter.string(from: start)
    }

    /// Compact strip-card date label: "Today" / "Yesterday" /
    /// "Tomorrow" / "Mon May 18".
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

    /// Inning-state line for a live card: "Top 5" / "Bot 9".
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

// MARK: - Team Leaders Section

private struct TeamLeadersSection: View {
    let leaders: TeamLeaders?
    let isLoading: Bool
    let onTapPlayer: (PlayerSearchResult) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            sectionHeader(title: "Team Leaders", trailing: nil)

            leaderRow(title: "Batting", cards: leaders?.batting)
            leaderRow(title: "Pitching", cards: leaders?.pitching)
        }
    }

    @ViewBuilder
    private func leaderRow(title: String, cards: [LeaderCard]?) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title.uppercased())
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.secondary)
                .padding(.horizontal, 16)

            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 10) {
                    if let cards, !cards.isEmpty {
                        ForEach(cards) { card in
                            Button { onTapPlayer(card.player) } label: {
                                LeaderTile(card: card)
                            }
                            .buttonStyle(.plain)
                        }
                    } else if isLoading {
                        ForEach(0..<4, id: \.self) { _ in LeaderTile.placeholder }
                    } else {
                        Text("No leaders yet")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 6)
                    }
                }
                .padding(.horizontal, 16)
            }
        }
    }
}

private struct LeaderTile: View {
    let card: LeaderCard

    var body: some View {
        VStack(spacing: 6) {
            headshot(url: card.player.largeHeadshotURL)
            Text(card.stat.uppercased())
                .font(.caption2.weight(.bold))
                .foregroundStyle(.secondary)
            Text(LeaderTile.formatValue(card.value, stat: card.stat))
                .font(.title3.weight(.bold))
                .foregroundStyle(.primary)
                .monospacedDigit()
            Text(LeaderTile.lastName(from: card.player.name))
                .font(.caption.weight(.semibold))
                .lineLimit(1)
                .minimumScaleFactor(0.8)
        }
        .padding(.vertical, 10)
        .padding(.horizontal, 8)
        .frame(width: 104, height: 148)
        .background(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .fill(.ultraThinMaterial)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .strokeBorder(.quaternary, lineWidth: 0.5)
        )
    }

    /// Skeleton tile while leader fetches are in flight.
    static var placeholder: some View {
        VStack(spacing: 6) {
            Circle().fill(Color(.systemGray5)).frame(width: 44, height: 44)
            RoundedRectangle(cornerRadius: 4).fill(Color(.systemGray5)).frame(width: 28, height: 8)
            RoundedRectangle(cornerRadius: 4).fill(Color(.systemGray5)).frame(width: 56, height: 14)
            RoundedRectangle(cornerRadius: 4).fill(Color(.systemGray5)).frame(width: 64, height: 10)
        }
        .padding(.vertical, 10)
        .padding(.horizontal, 8)
        .frame(width: 104, height: 148)
        .background(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .fill(.ultraThinMaterial)
        )
    }

    private func headshot(url: URL?) -> some View {
        AsyncImage(url: url) { phase in
            switch phase {
            case .success(let image): image.resizable().scaledToFill()
            default:
                Image(systemName: "person.crop.circle.fill")
                    .resizable().scaledToFit()
                    .foregroundStyle(.tertiary)
            }
        }
        .frame(width: 44, height: 44)
        .background(Circle().fill(.ultraThinMaterial))
        .clipShape(Circle())
        .overlay(Circle().strokeBorder(.quaternary, lineWidth: 0.5))
    }

    /// Same precision rules the Leaderboards tab uses — keeps the
    /// Home strip aligned with the canonical formatting elsewhere
    /// in the app. Unknown stat → 1-decimal as a safe default.
    static func formatValue(_ v: Double?, stat: String) -> String {
        guard let v else { return "—" }
        switch stat {
        case "AVG", "OBP", "SLG", "OPS":
            return String(format: "%.3f", v)
        case "ERA", "WHIP", "FIP":
            return String(format: "%.2f", v)
        case "HR", "RBI", "SO", "W", "L", "SV", "H", "BB", "R", "SB":
            return String(Int(v.rounded()))
        default:
            return String(format: "%.1f", v)
        }
    }

    static func lastName(from full: String) -> String {
        lastNameWithSuffix(full)
    }
}

// MARK: - Favorite Players Section

private struct FavoritePlayersSection: View {
    let favorites: [FavoritePlayerDisplay]
    let isLoading: Bool
    @Binding var isEditing: Bool
    let onAdd: () -> Void
    let onRemove: (Int) -> Void
    let onTapPlayer: (PlayerSearchResult) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            sectionHeader(
                title: "My Players",
                trailing: trailingControl,
            )

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

    private var trailingControl: AnyView {
        // Hide Edit when nothing's added yet — no point offering a
        // mode that has nothing to act on.
        guard !favorites.isEmpty else { return AnyView(EmptyView()) }
        return AnyView(
            Button(isEditing ? "Done" : "Edit") {
                withAnimation(.easeInOut(duration: 0.15)) {
                    isEditing.toggle()
                }
            }
            .font(.subheadline.weight(.semibold))
        )
    }

    /// Edit mode disables the navigation push so the tap is owned by
    /// the minus badge. Regular mode wraps the tile in a button that
    /// pushes the profile.
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

    /// "S. Ohtani" / "M. Trout". Hangs the last-name suffix
    /// (Jr./Sr./II/…) onto the trailing part via the existing
    /// helper used elsewhere in the app.
    private var displayName: String {
        let parts = fav.player.name.split(separator: " ", maxSplits: 1)
        guard let first = parts.first, parts.count > 1 else {
            return fav.player.name
        }
        let last = lastNameWithSuffix(fav.player.name)
        return "\(first.prefix(1)). \(last)"
    }

    /// "RF · NYY" / "RF" / "NYY" / "—" depending on what's known.
    /// Falls back gracefully so the line never reads "nil · nil".
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

// MARK: - Shared section header

/// Used by Team Leaders and My Players. Centralized so both sections
/// stay visually identical when the spec changes (font tweak,
/// trailing-button placement, etc.).
@ViewBuilder
private func sectionHeader(
    title: String,
    trailing: AnyView?,
) -> some View {
    HStack {
        Text(title)
            .font(.title3.weight(.bold))
        Spacer()
        trailing
    }
    .padding(.horizontal, 16)
    .padding(.top, 4)
    Divider()
        .opacity(0.35)
        .padding(.horizontal, 16)
}

#Preview {
    HomeView()
}
