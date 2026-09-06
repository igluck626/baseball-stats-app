//
//  ScheduleSheet.swift
//  BaseballStats
//
//  Full-season schedule sheet for the favorite team. Triggered
//  by the hero card's "Schedule ›" button. Renders one row per
//  game, grouped by month with sticky headers tinted in the
//  team's primary color. Tapping a row pushes a BoxScoreView
//  inside the sheet's own NavigationStack.
//
//  Auto-scrolls to today's game on first paint (or the next
//  upcoming game if today isn't a game day).
//

import SwiftUI

struct ScheduleSheet: View {
    @StateObject private var vm = ScheduleViewModel()
    @Environment(\.dismiss) private var dismiss
    @State private var path = NavigationPath()

    let favorite: MLBTeamCatalog.Entry
    /// Threaded through from HomeView so the sheet's BoxScoreView
    /// destination renders the same division-rank + W-L header
    /// the Scores tab uses.
    let teamStandings: [Int: TeamStandingInfo]
    let teamRecords:   [Int: TeamRecord]
    /// Passed in explicitly (not via `@EnvironmentObject`) because environment
    /// objects don't reliably cross the `.sheet` boundary. Forwarded to the
    /// pushed BoxScoreView so its live polling still gates on lifecycle/tab.
    @ObservedObject var navigation: AppNavigation
    /// Same sheet-boundary reason — forwarded to the pushed BoxScoreView so a
    /// live box score opened from the schedule subscribes to the shared store.
    @ObservedObject var liveStore: LiveGameStore

    private var teamColor: Color {
        TeamColors.color(for: favorite.lahmanCode) ?? .accentColor
    }

    private var seasonYear: Int {
        Calendar.current.component(.year, from: Date())
    }

    var body: some View {
        NavigationStack(path: $path) {
            content
            .navigationBarTitleDisplayMode(.inline)
            // Without this the bar is fully transparent and rows scroll visibly
            // behind it — and because the month header pins BELOW the bar, an
            // August row would render above its own "AUGUST" header. Same
            // modifier the other five toolbar-bearing screens use.
            .toolbarBackground(.ultraThinMaterial, for: .navigationBar)
            .toolbar {
                ToolbarItem(placement: .principal) {
                    HStack(spacing: 8) {
                        TeamLogoView(team: favorite.teamInfo, size: 24)
                        Text("\(String(seasonYear)) Schedule")
                            .font(.headline.weight(.semibold))
                    }
                }
                ToolbarItem(placement: .cancellationAction) {
                    Button("Close") { dismiss() }
                }
            }
            .stackDestinations(BoxScoreContext(
                path: $path,
                owningTab: .home,
                navigation: navigation,
                liveStore: liveStore,
                teamStandings: teamStandings,
                teamRecords: teamRecords,
            ))
        }
        .task { await vm.load(bdlTeamId: favorite.bdlTeamId) }
        .presentationDetents([.large])
        // Glass sheet — matches the app-wide sheet treatment.
        .presentationBackground(.ultraThinMaterial)
    }

    @ViewBuilder
    private var content: some View {
        if vm.isLoading && !vm.didLoad {
            ProgressView()
                .controlSize(.large)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if let err = vm.error, vm.gamesByMonth.isEmpty {
            ContentUnavailableView {
                Label("Schedule unavailable", systemImage: "calendar.badge.exclamationmark")
            } description: {
                Text(err)
            }
        } else if vm.gamesByMonth.isEmpty {
            ContentUnavailableView {
                Label("No games scheduled", systemImage: "calendar")
            } description: {
                Text("No regular-season games on the schedule yet.")
            }
        } else {
            scheduleList
        }
    }

    private var scheduleList: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(spacing: 0, pinnedViews: [.sectionHeaders]) {
                    ForEach(Array(vm.gamesByMonth.enumerated()), id: \.offset) { _, bucket in
                        Section(header: monthHeader(bucket.month)) {
                            ForEach(bucket.games) { game in
                                ScheduleRow(
                                    recordAfter:   vm.recordAfter[game.gamePk],
                                    game:          game,
                                    favoriteBDLId: favorite.bdlTeamId,
                                    teamColor:     teamColor,
                                    onTap:         { path.append(game) },
                                )
                                .id(game.id)
                                Divider()
                                    .opacity(0.3)
                                    .padding(.leading, 16)
                            }
                        }
                    }
                }
            }
            .onAppear {
                // One render tick of slack before scrolling so the
                // LazyVStack has had a chance to lay out the section
                // we're targeting. Without this, scrollTo lands on
                // an unmeasured ID and silently no-ops.
                Task {
                    try? await Task.sleep(nanoseconds: 80_000_000)
                    if let target = anchorGame() {
                        withAnimation(.easeOut(duration: 0.25)) {
                            proxy.scrollTo(target.id, anchor: .center)
                        }
                    }
                }
            }
        }
    }

    /// Today if it's a game day, else the next upcoming game, else
    /// the season's last game (so the sheet still lands somewhere
    /// useful after a season ends). nil only when the schedule is
    /// genuinely empty.
    private func anchorGame() -> Game? {
        let cal = Calendar.current
        let today = cal.startOfDay(for: Date())
        for bucket in vm.gamesByMonth {
            for game in bucket.games {
                if let start = game.startDate, cal.isDate(start, inSameDayAs: today) {
                    return game
                }
            }
        }
        for bucket in vm.gamesByMonth {
            for game in bucket.games {
                if let start = game.startDate, start >= today {
                    return game
                }
            }
        }
        return vm.gamesByMonth.last?.games.last
    }

    private func monthHeader(_ name: String) -> some View {
        HStack {
            Text(name.uppercased())
                .font(.subheadline.weight(.bold))
                .foregroundStyle(teamColor)
                .tracking(0.8)
            Spacer()
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity)
        .background(.thinMaterial)
    }
}

// MARK: - Schedule Row

private struct ScheduleRow: View {
    /// "W-L after this game", for completed games only — nil for anything
    /// unplayed, which is what keeps upcoming rows unchanged.
    var recordAfter: String? = nil
    let game: Game
    let favoriteBDLId: Int
    let teamColor: Color
    let onTap: () -> Void

    private var opponent: GameTeam {
        game.bdlHomeTeamId == favoriteBDLId
            ? game.teams.away
            : game.teams.home
    }

    private var isHome: Bool {
        game.bdlHomeTeamId == favoriteBDLId
    }

    private var didWin: Bool {
        HomeGameUtils.favoriteWon(
            game: game, favoriteBDLId: favoriteBDLId,
        )
    }

    var body: some View {
        Button(action: onTap) {
            HStack(spacing: 12) {
                dateColumn
                indicatorColumn
                opponentColumn
                Spacer(minLength: 6)
                trailingColumn
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    /// True iff this game falls on today's ET calendar date. MLB
    /// schedules everything off America/New_York, so an ET date
    /// comparison is what callers expect even on a PT device — a
    /// 10pm PT game (1am ET next day) reads as "tomorrow" the
    /// moment ET rolls over, the same way the existing
    /// `BallDontLieClient.easternDateString(for:)` filter buckets
    /// games for the rest of the app.
    private var isTodayET: Bool {
        guard let start = game.startDate else { return false }
        return Self.etDateFormatter.string(from: start)
            == Self.etDateFormatter.string(from: Date())
    }

    @ViewBuilder
    private var dateColumn: some View {
        if isTodayET {
            // Single-line "Today" pill in team color — same column
            // width as the weekday + date layout below, so rows align.
            Text("Today")
                .font(.subheadline.weight(.bold))
                .foregroundStyle(teamColor)
                .frame(width: 64, alignment: .leading)
        } else {
            VStack(alignment: .leading, spacing: 2) {
                Text(Self.weekdayFormatter.string(from: game.startDate ?? Date()))
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                Text(Self.dateFormatter.string(from: game.startDate ?? Date()))
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.primary)
            }
            .frame(width: 64, alignment: .leading)
        }
    }

    private var indicatorColumn: some View {
        Text(isHome ? "vs" : "@")
            .font(.caption.weight(.semibold))
            .foregroundStyle(.secondary)
            .frame(width: 22, alignment: .center)
    }

    /// No team mark: the abbreviation is right beside it, so the badge was
    /// saying the same thing twice. The mark in this sheet's toolbar stays —
    /// that one names the club whose schedule this is, which the title
    /// ("2026 Schedule") does not.
    private var opponentColumn: some View {
        HStack(spacing: 8) {
            Text(opponent.team.abbreviation
                 ?? String(opponent.team.name.prefix(3)).uppercased())
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.primary)
                .lineLimit(1)
        }
    }

    @ViewBuilder
    private var trailingColumn: some View {
        switch game.phase {
        case .final:
            let (f, o) = HomeGameUtils.scores(
                game: game, favoriteBDLId: favoriteBDLId,
            )
            HStack(spacing: 6) {
                Text(didWin ? "W" : "L")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(.white)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(Capsule().fill(didWin ? Color.green : Color.red))
                Text("\(f ?? 0)-\(o ?? 0)")
                    .font(.subheadline.weight(didWin ? .bold : .semibold))
                    .foregroundStyle(didWin ? .primary : .secondary)
                    .monospacedDigit()
                // Inside the trailing stack rather than a fifth column, so it
                // stays grouped with the result it belongs to and the date /
                // indicator / opponent columns keep their widths. Fixed width
                // so the scores above it stay aligned down the list.
                if let recordAfter {
                    Text(recordAfter)
                        .font(.caption.weight(.medium))
                        .foregroundStyle(.tertiary)
                        .monospacedDigit()
                        .frame(width: 46, alignment: .trailing)
                }
            }
        case .live:
            let (f, o) = HomeGameUtils.scores(
                game: game, favoriteBDLId: favoriteBDLId,
            )
            HStack(spacing: 6) {
                Circle().fill(.red).frame(width: 6, height: 6)
                Text("LIVE")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(.red)
                Text("\(f ?? 0)-\(o ?? 0)")
                    .font(.subheadline.weight(.bold))
                    .foregroundStyle(.primary)
                    .monospacedDigit()
            }
        case .postponed:
            Text("PPD")
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.orange)
        case .preview, .other:
            Text(HomeGameUtils.localTime(game: game))
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.secondary)
                .monospacedDigit()
        }
    }

    private static let weekdayFormatter: DateFormatter = {
        let f = DateFormatter()
        f.timeZone = .current
        f.dateFormat = "EEE"
        return f
    }()

    private static let dateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.timeZone = .current
        f.dateFormat = "MMM d"
        return f
    }()

    /// ET-anchored `yyyy-MM-dd`. Used only for the "is today?"
    /// check — comparing the game's start to `Date()` via the
    /// same calendar pair keeps the verdict consistent with the
    /// rest of the app's ET-based date filters.
    private static let etDateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.calendar = .init(identifier: .gregorian)
        f.timeZone = TimeZone(identifier: "America/New_York") ?? .current
        f.locale   = .init(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()
}
