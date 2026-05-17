//
//  ScoresView.swift
//  BaseballStats
//
//  Main view for the Scores tab. Top: a horizontally-scrolling date
//  strip spanning ±7 days around today, with today pre-selected.
//  Below: one card per game on the chosen date, surfacing score,
//  status, and decisions. Tapping a card pushes into a box score.
//
//  Auto-refresh: while any game on the selected date is live, a
//  30-second timer fires another /schedule fetch so the score and
//  inning state stay current. The timer is cancelled when the
//  selected date changes, when the view disappears, or when no
//  games remain live.
//
//  Data source is MLB Stats API directly via `MLBStatsAPIClient`
//  (no Railway round trip). Player taps in the box score reach
//  through `APIClient.getPlayerByMlbId` to navigate to the
//  existing `PlayerProfileView`.
//

import Combine
import SwiftUI

@MainActor
final class ScoresViewModel: ObservableObject {
    /// Currently selected calendar day. Starts at today (local time).
    @Published var selectedDate: Date = Calendar.current.startOfDay(for: Date())
    @Published var games: [Game] = []
    @Published var isLoading: Bool = false
    @Published var error: String?
    /// Set true once a load completes (success or empty); used so the
    /// view can distinguish "still loading" from "no games today".
    @Published var didLoad: Bool = false

    private let api: MLBStatsAPIClient
    private var refreshTask: Task<Void, Never>?

    init(api: MLBStatsAPIClient = .shared) {
        self.api = api
    }

    func load(date: Date) async {
        isLoading = true
        error = nil
        do {
            let response = try await api.getSchedule(date: date)
            let games = response.dates.first(where: { $0.date == ScoresViewModel.iso(date) })?.games
                ?? response.dates.flatMap(\.games)
            self.games = games
        } catch {
            self.error = (error as? MLBStatsAPIError).map(Self.message(for:))
                ?? error.localizedDescription
            self.games = []
        }
        isLoading = false
        didLoad = true
    }

    /// Pull-to-refresh variant: same fetch, but a network failure
    /// keeps the existing game list visible instead of replacing it
    /// with an error screen. The user just sees the spinner stop —
    /// the next normal `load()` will surface persistent failures.
    func refresh() async {
        do {
            let response = try await api.getSchedule(date: selectedDate)
            let g = response.dates.first(where: { $0.date == ScoresViewModel.iso(selectedDate) })?.games
                ?? response.dates.flatMap(\.games)
            self.games = g
            self.error = nil
        } catch {
            // Silent — keep stale games visible rather than wiping
            // the screen on a transient pull-to-refresh hiccup.
        }
    }

    /// Spin up a polling task that re-runs `load(date:)` every 30s
    /// while any game in `games` is live AND the selected date is
    /// today. We don't poll past dates (scores frozen) or future
    /// dates (no live state to refresh into). Cancels itself
    /// naturally once the last live game on today's slate ends.
    func startAutoRefresh(for date: Date) {
        stopAutoRefresh()
        guard Calendar.current.isDateInToday(date) else { return }
        guard games.contains(where: { $0.phase == .live }) else { return }
        refreshTask = Task { @MainActor [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 30 * 1_000_000_000)
                guard !Task.isCancelled, let self else { return }
                await self.load(date: date)
                if !self.games.contains(where: { $0.phase == .live }) { return }
            }
        }
    }

    func stopAutoRefresh() {
        refreshTask?.cancel()
        refreshTask = nil
    }

    static func iso(_ date: Date) -> String {
        MLBStatsAPIClient.scheduleDateFormatter.string(from: date)
    }

    private static func message(for error: MLBStatsAPIError) -> String {
        switch error {
        case .badStatus(let code):  return "MLB Stats API returned \(code)."
        case .decoding:             return "Couldn't read the schedule response."
        }
    }
}

struct ScoresView: View {
    @StateObject private var vm = ScoresViewModel()
    @State private var navigationPath = NavigationPath()
    @State private var showingDatePicker = false

    var body: some View {
        NavigationStack(path: $navigationPath) {
            VStack(spacing: 0) {
                dateBar
                content
            }
            .navigationTitle("Scores")
            .navigationBarTitleDisplayMode(.large)
            .navigationDestination(for: Game.self) { game in
                BoxScoreView(game: game, path: $navigationPath)
            }
            .navigationDestination(for: PlayerSearchResult.self) { player in
                PlayerProfileView(player: player)
            }
            .sheet(isPresented: $showingDatePicker) {
                datePickerSheet
            }
        }
        .task { await vm.load(date: vm.selectedDate) }
        // Restart the auto-refresh whenever a load completes — gives
        // it a fresh look at whether any game is live now. Also
        // detect Live → Final transitions so the Standings tab can
        // pull in the just-completed W/L delta without waiting for
        // a tab switch or the next nightly run.
        .onChange(of: vm.games) { oldGames, newGames in
            vm.startAutoRefresh(for: vm.selectedDate)
            let wasLive = Set(oldGames.filter { $0.phase == .live }.map(\.gamePk))
            let nowFinal = newGames.filter { $0.phase == .final }.map(\.gamePk)
            if nowFinal.contains(where: { wasLive.contains($0) }) {
                NotificationCenter.default.post(name: .standingsShouldRefresh, object: nil)
            }
        }
        .onDisappear { vm.stopAutoRefresh() }
    }

    // MARK: - Date bar

    /// Symmetrical nav row: ◀ pill ▶. The center pill carries the
    /// relative-day label ("Today" / "Yesterday" / "Mon, May 12")
    /// and is itself the tap target that opens the calendar sheet,
    /// so there's no separate calendar icon.
    private var dateBar: some View {
        HStack(spacing: 12) {
            stepButton(systemImage: "chevron.left", days: -1)
            Spacer(minLength: 0)
            datePill
            Spacer(minLength: 0)
            stepButton(systemImage: "chevron.right", days: 1)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
    }

    private func stepButton(systemImage: String, days: Int) -> some View {
        Button {
            let cal = Calendar.current
            guard let next = cal.date(byAdding: .day, value: days, to: vm.selectedDate) else { return }
            jumpTo(date: cal.startOfDay(for: next))
        } label: {
            Image(systemName: systemImage)
                .font(.subheadline.weight(.semibold))
                .frame(width: 36, height: 32)
                // Glass capsule to match the Leaders-tab stat picker
                // — `Color(.secondarySystemFill)` read as a solid
                // grey patch that fought the page background.
                .glassEffect(.regular, in: Capsule())
        }
        .buttonStyle(.plain)
    }

    private var datePill: some View {
        Button { showingDatePicker = true } label: {
            Text(relativeDateLabel(vm.selectedDate))
                .font(.subheadline.weight(.bold))
                .monospacedDigit()
                .foregroundStyle(.primary)
                .padding(.horizontal, 14)
                .padding(.vertical, 7)
                .glassEffect(.regular, in: Capsule())
        }
        .buttonStyle(.plain)
    }

    /// Sheet content: graphical date picker. Tap a day → load it +
    /// dismiss. No min/max bounds — the MLB Stats API serves any
    /// date and the user might want deep history or schedule peeks.
    private var datePickerSheet: some View {
        NavigationStack {
            DatePicker(
                "Date",
                selection: Binding(
                    get: { vm.selectedDate },
                    set: { newDate in
                        let day = Calendar.current.startOfDay(for: newDate)
                        jumpTo(date: day)
                        showingDatePicker = false
                    }
                ),
                displayedComponents: [.date]
            )
            .datePickerStyle(.graphical)
            .padding()
            .navigationTitle("Pick a date")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { showingDatePicker = false }
                }
            }
        }
        .presentationDetents([.medium, .large])
    }

    private func jumpTo(date: Date) {
        vm.stopAutoRefresh()
        vm.selectedDate = date
        Task { await vm.load(date: date) }
    }

    /// "Today" / "Yesterday" / "Tomorrow" / "Mon, May 12". Anchored on
    /// `Date()` (the actual clock) rather than `vm.selectedDate` so the
    /// label correctly identifies the relative position of the picked
    /// date vs. now.
    private func relativeDateLabel(_ date: Date) -> String {
        let cal = Calendar.current
        if cal.isDateInToday(date)     { return "Today" }
        if cal.isDateInYesterday(date) { return "Yesterday" }
        if cal.isDateInTomorrow(date)  { return "Tomorrow" }
        return Self.absoluteDateFormatter.string(from: date)
    }

    @ViewBuilder
    private var content: some View {
        if vm.isLoading && vm.games.isEmpty {
            ProgressView()
                .controlSize(.large)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if let error = vm.error, vm.games.isEmpty {
            ContentUnavailableView {
                Label("Couldn't load scores", systemImage: "exclamationmark.triangle")
            } description: {
                Text(error)
            } actions: {
                Button("Try Again") { Task { await vm.load(date: vm.selectedDate) } }
                    .buttonStyle(.borderedProminent)
            }
        } else if vm.games.isEmpty && vm.didLoad {
            ContentUnavailableView {
                Label("No games", systemImage: "calendar")
            } description: {
                Text("No MLB games scheduled for \(ScoresView.cardDateFormatter.string(from: vm.selectedDate)).")
            }
        } else {
            gameList
        }
    }

    private var gameList: some View {
        // Bucket games by phase so the list reads top-down by what
        // the user most likely wants to see — live action first,
        // then today's still-to-come games, then finals at the
        // bottom. Sort within each bucket so the most "interesting
        // right now" items rise: latest innings for live, earliest
        // start time for upcoming, most-recently-completed for
        // finals.
        let live = vm.games
            .filter { $0.phase == .live }
            .sorted { ($0.linescore?.currentInning ?? 0) > ($1.linescore?.currentInning ?? 0) }
        let upcoming = vm.games
            .filter { $0.phase == .preview || $0.phase == .other }
            .sorted { ($0.startDate ?? .distantFuture) < ($1.startDate ?? .distantFuture) }
        let completed = vm.games
            .filter { $0.phase == .final }
            .sorted { ($0.startDate ?? .distantPast) > ($1.startDate ?? .distantPast) }

        return ScrollView {
            LazyVStack(spacing: 12) {
                if !live.isEmpty {
                    sectionHeader("Live")
                    ForEach(live) { game in
                        NavigationLink(value: game) {
                            LiveGameCard(game: game)
                        }
                        .buttonStyle(.plain)
                    }
                }
                if !upcoming.isEmpty {
                    sectionHeader("Upcoming")
                    ForEach(upcoming) { game in
                        NavigationLink(value: game) {
                            GameCard(game: game)
                        }
                        .buttonStyle(.plain)
                    }
                }
                if !completed.isEmpty {
                    sectionHeader("Completed")
                    ForEach(completed) { game in
                        // Final games are expand-on-tap; box-score
                        // nav happens via the embedded "Box Score →"
                        // button inside the expanded view, so the
                        // outer cell doesn't wrap a NavigationLink.
                        FinalGameCard(game: game, path: $navigationPath)
                    }
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 12)
        }
        .refreshable {
            await vm.refresh()
        }
    }

    /// Lightweight bucket header matching the muted division headers
    /// on the Standings view — small, uppercase, secondary fill.
    private func sectionHeader(_ title: String) -> some View {
        Text(title.uppercased())
            .font(.caption.weight(.semibold))
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.top, 4)
            .padding(.horizontal, 4)
    }

    // MARK: - Formatters

    /// "Mon, May 12" — used when the selected date isn't ±1 from
    /// today. Year is omitted to keep the pill compact; the calendar
    /// sheet lets the user verify the year if they care.
    private static let absoluteDateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "EEE, MMM d"
        return f
    }()

    static let cardDateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateStyle = .full
        f.timeStyle = .none
        return f
    }()
}

// MARK: - Game card

private struct GameCard: View {
    let game: Game

    var body: some View {
        HStack(alignment: .center, spacing: 14) {
            VStack(spacing: 8) {
                teamRow(side: game.teams.away,
                        winner: didWin(side: game.teams.away))
                teamRow(side: game.teams.home,
                        winner: didWin(side: game.teams.home))
            }
            // Score section expands to fill remaining width; the
            // venue section to the right is fixed at 110pt so the
            // divider sits at a stable position regardless of
            // venue name length or wrap state.
            .frame(minWidth: 120, maxWidth: .infinity, alignment: .leading)

            Divider().frame(height: 56)

            VStack(alignment: .trailing, spacing: 4) {
                Text(statusLine)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(statusColor)
                    .lineLimit(1)
                    .minimumScaleFactor(0.85)
                if let detail = statusDetail {
                    Text(detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        // No lineLimit — long venue names like
                        // "Globe Life Field at Arlington" wrap to a
                        // second line inside the fixed-width column
                        // instead of pushing the divider leftward.
                        .multilineTextAlignment(.trailing)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .frame(width: 110, alignment: .trailing)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .frame(maxWidth: .infinity)
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 16))
        .shadow(color: .black.opacity(0.06), radius: 6, x: 0, y: 2)
        .contentShape(Rectangle())
    }

    private func teamRow(side: GameTeam, winner: Bool) -> some View {
        HStack(spacing: 10) {
            TeamLogoView(team: side.team, size: 28)

            Text(side.team.abbreviation ?? abbreviate(side.team.name))
                .font(.subheadline.weight(winner ? .bold : .semibold))
                .foregroundStyle(loserDimmed(winner) ? .secondary : .primary)
                .lineLimit(1)

            if let record = side.leagueRecord, let w = record.wins, let l = record.losses {
                Text("(\(w)–\(l))")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }

            Spacer()

            // Only render the score for games that have actually
            // started. The MLB API ships score=0 for both teams
            // on previews / scheduled games, which read as "0–0"
            // here even before first pitch; suppress entirely so
            // the time + venue carry the row instead.
            if game.phase == .live || game.phase == .final {
                Text(side.score.map(String.init) ?? "")
                    .font(.title3.weight(winner ? .bold : .semibold))
                    .foregroundStyle(loserDimmed(winner) ? .secondary : .primary)
                    .monospacedDigit()
            }
        }
    }

    /// Dim the loser's row in completed games — keeps the winner the
    /// visual anchor. For in-progress or scheduled games no team is
    /// dimmed.
    private func loserDimmed(_ winner: Bool) -> Bool {
        game.phase == .final && !winner
    }

    /// True iff this side won the game. For non-final games returns
    /// false on both sides so neither row is highlighted.
    private func didWin(side: GameTeam) -> Bool {
        game.phase == .final && side.isWinner == true
    }

    /// "FINAL" / "Top 7th" / "7:05 PM" / detailed-state pass-through.
    private var statusLine: String {
        switch game.phase {
        case .final:
            return "FINAL"
        case .live:
            if let ordinal = game.linescore?.currentInningOrdinal,
               let state = game.linescore?.inningState {
                let stateShort = state.uppercased().hasPrefix("MID") ? "MID"
                                : state.uppercased().hasPrefix("END") ? "END"
                                : state.uppercased().hasPrefix("TOP") ? "TOP"
                                : "BOT"
                return "\(stateShort) \(ordinal)"
            }
            return "LIVE"
        case .preview:
            if let date = game.startDate {
                return Self.timeFormatter.string(from: date)
            }
            return game.status.detailedState.uppercased()
        case .other:
            return game.status.detailedState.uppercased()
        }
    }

    /// Secondary line under the headline status — game type or venue
    /// hint for previews, current outs for live games. Decisions for
    /// finals live on `FinalGameCard`'s expanded body, not the
    /// collapsed row.
    private var statusDetail: String? {
        switch game.phase {
        case .final:
            return nil
        case .live:
            let outs = game.linescore?.outs ?? 0
            return "\(outs) out\(outs == 1 ? "" : "s")"
        case .preview:
            if let venue = game.venue?.name { return venue }
            return nil
        case .other:
            return nil
        }
    }

    private var statusColor: Color {
        switch game.phase {
        case .final:   return .secondary
        case .live:    return .red
        case .preview: return .primary
        case .other:   return .secondary
        }
    }

    /// First three letters of the team name — fallback for the rare
    /// schedule row that doesn't ship `abbreviation`.
    private func abbreviate(_ name: String) -> String {
        String(name.prefix(3)).uppercased()
    }

    private static let timeFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "h:mm a"
        return f
    }()
}

// MARK: - Final game card (expandable)

/// Final-game variant of `GameCard`. The collapsed shape matches the
/// non-final card visually but the tap target is the body itself —
/// tapping toggles an expansion that reveals the linescore + W/L/SV
/// decisions + a "Box Score →" button that pushes the full box
/// score on the parent's NavigationStack.
private struct FinalGameCard: View {
    let game: Game
    @Binding var path: NavigationPath
    @State private var isExpanded = false
    /// Lazily-fetched box score for the expanded view. Loaded the
    /// first time the user expands the card so decision-pitcher
    /// records (W/L: …) and the HR summary line have data to
    /// render. Cached for the lifetime of the card so subsequent
    /// expand/collapse cycles don't re-hit the API.
    @State private var boxScore: BoxScoreResponse?
    @State private var isLoadingBoxScore = false

    var body: some View {
        VStack(spacing: 10) {
            collapsedBody
                .contentShape(Rectangle())
                .onTapGesture {
                    withAnimation(.easeInOut(duration: 0.22)) {
                        isExpanded.toggle()
                    }
                    if isExpanded && boxScore == nil && !isLoadingBoxScore {
                        Task { await fetchBoxScore() }
                    }
                }
            if isExpanded {
                Divider()
                linescore
                if hasAnyDecision {
                    Divider()
                    decisions
                }
                hrSummary
                boxScoreButton
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .frame(maxWidth: .infinity)
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 16))
        .shadow(color: .black.opacity(0.06), radius: 6, x: 0, y: 2)
    }

    private func fetchBoxScore() async {
        isLoadingBoxScore = true
        defer { isLoadingBoxScore = false }
        boxScore = try? await MLBStatsAPIClient.shared.getBoxScore(gamePk: game.gamePk)
    }

    // MARK: Collapsed header

    private var collapsedBody: some View {
        HStack(alignment: .center, spacing: 14) {
            VStack(spacing: 8) {
                teamRow(side: game.teams.away)
                teamRow(side: game.teams.home)
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            Divider().frame(height: 56)

            Text("FINAL")
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.secondary)
                .frame(minWidth: 56, alignment: .trailing)
        }
    }

    private func teamRow(side: GameTeam) -> some View {
        let isWinner = side.isWinner == true
        let dimmed = !isWinner
        return HStack(spacing: 10) {
            TeamLogoView(team: side.team, size: 28)

            Text(side.team.abbreviation ?? String(side.team.name.prefix(3)).uppercased())
                .font(.subheadline.weight(isWinner ? .bold : .semibold))
                .foregroundStyle(dimmed ? .secondary : .primary)
                .lineLimit(1)

            if let record = side.leagueRecord, let w = record.wins, let l = record.losses {
                Text("(\(w)–\(l))")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }

            Spacer()

            Text(side.score.map(String.init) ?? "")
                .font(.title3.weight(isWinner ? .bold : .semibold))
                .foregroundStyle(dimmed ? .secondary : .primary)
                .monospacedDigit()
        }
    }

    // MARK: Expanded — linescore

    /// One inning column per inning that has data, plus R/H/E
    /// totals. Horizontally scrollable so extra-innings games don't
    /// blow up the card width.
    private var linescore: some View {
        let innings = game.linescore?.innings ?? []
        let totals = game.linescore?.teams
        let inningCount = max(innings.count, game.linescore?.scheduledInnings ?? 9)
        return ScrollView(.horizontal, showsIndicators: false) {
            VStack(alignment: .leading, spacing: 2) {
                lineRow(label: "",
                        innings: (1...inningCount).map { String($0) },
                        totals: ["R", "H", "E"],
                        isHeader: true)
                lineRow(label: game.teams.away.team.abbreviation
                            ?? String(game.teams.away.team.name.prefix(3)).uppercased(),
                        innings: (1...inningCount).map { i in
                            cell(innings.first(where: { $0.num == i })?.away?.runs)
                        },
                        totals: [
                            cell(totals?.away?.runs),
                            cell(totals?.away?.hits),
                            cell(totals?.away?.errors),
                        ],
                        isHeader: false)
                lineRow(label: game.teams.home.team.abbreviation
                            ?? String(game.teams.home.team.name.prefix(3)).uppercased(),
                        innings: (1...inningCount).map { i in
                            cell(innings.first(where: { $0.num == i })?.home?.runs)
                        },
                        totals: [
                            cell(totals?.home?.runs),
                            cell(totals?.home?.hits),
                            cell(totals?.home?.errors),
                        ],
                        isHeader: false)
            }
        }
    }

    private func lineRow(label: String, innings: [String], totals: [String], isHeader: Bool) -> some View {
        HStack(spacing: 0) {
            Text(label)
                .font(.caption.weight(.semibold))
                .frame(width: 44, alignment: .leading)
            ForEach(innings.indices, id: \.self) { i in
                Text(innings[i])
                    .font(.caption.weight(isHeader ? .bold : .regular))
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
        .foregroundStyle(isHeader ? Color.secondary : Color.primary)
    }

    private func cell(_ v: Int?) -> String {
        v.map(String.init) ?? "-"
    }

    // MARK: Expanded — decisions

    private var hasAnyDecision: Bool {
        let d = game.decisions
        return d?.winner != nil || d?.loser != nil || d?.save != nil
    }

    /// W: / L: / SV: lines. Pitcher records ("W: Cole (8-2)") come
    /// from the lazily-fetched box score's `seasonStats.pitching`;
    /// until that arrives we render the name alone so the section
    /// isn't blank during the brief fetch window.
    private var decisions: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let w = game.decisions?.winner {
                decisionLine(tag: "W",  player: w)
            }
            if let l = game.decisions?.loser {
                decisionLine(tag: "L",  player: l)
            }
            if let s = game.decisions?.save {
                decisionLine(tag: "SV", player: s)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func decisionLine(tag: String, player: PlayerInfo) -> some View {
        let record = recordForPitcher(id: player.id, tag: tag)
        return HStack(spacing: 6) {
            Text("\(tag):")
                .font(.caption.weight(.bold))
                .foregroundStyle(.secondary)
                .frame(width: 28, alignment: .leading)
                .monospacedDigit()
            Text(player.fullName + (record.map { " (\($0))" } ?? ""))
                .font(.caption)
                .foregroundStyle(.primary)
                .lineLimit(1)
        }
    }

    /// Look up the decision pitcher's updated W-L (or saves total
    /// for SV decisions) from the cached box score. nil before the
    /// fetch completes — the line falls back to a name-only render.
    private func recordForPitcher(id: Int, tag: String) -> String? {
        guard let bs = boxScore else { return nil }
        let key = "ID\(id)"
        let bp = bs.teams.away.players[key] ?? bs.teams.home.players[key]
        guard let pit = bp?.seasonStats?.pitching else { return nil }
        if tag == "SV" {
            return pit.saves.map { "\($0) SV" }
        }
        guard let w = pit.wins, let l = pit.losses else { return nil }
        return "\(w)-\(l)"
    }

    /// "HR: Judge (18), Stanton (11)" line shown below the decisions
    /// section when at least one home run was hit in the game.
    /// Pulls per-batter HR counts from the lazily-fetched box score;
    /// renders nothing until the fetch lands (no blank label, no
    /// loading state — the line just appears when ready).
    @ViewBuilder
    private var hrSummary: some View {
        if let bs = boxScore {
            let homerSegments = hrSegments(from: bs)
            if !homerSegments.isEmpty {
                (Text("HR: ").font(.caption2.weight(.bold))
                    + Text(homerSegments.joined(separator: ", ")).font(.caption2))
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.top, 4)
            }
        }
    }

    /// Per-batter "Lastname (season HR)" pieces ordered by the
    /// batting-order index for each side. Both teams are walked so
    /// the line surfaces every HR in the game, not just the
    /// winners'.
    private func hrSegments(from bs: BoxScoreResponse) -> [String] {
        let teams = [bs.teams.away, bs.teams.home]
        var out: [String] = []
        for team in teams {
            for id in team.batters {
                guard let p = team.players["ID\(id)"] else { continue }
                guard let hr = p.stats?.batting?.homeRuns, hr > 0 else { continue }
                let season = p.seasonStats?.batting?.homeRuns ?? 0
                let last = p.person.fullName.split(separator: " ").last.map(String.init)
                    ?? p.person.fullName
                out.append("\(last) (\(season))")
            }
        }
        return out
    }

    // MARK: Expanded — box score nav

    private var boxScoreButton: some View {
        Button {
            path.append(game)
        } label: {
            HStack(spacing: 4) {
                Text("Box Score")
                Image(systemName: "arrow.right")
            }
            .font(.subheadline.weight(.semibold))
            .foregroundStyle(.white)
            .padding(.horizontal, 14)
            .padding(.vertical, 8)
            .background(Capsule().fill(Color.accentColor))
        }
        .buttonStyle(.plain)
        .frame(maxWidth: .infinity, alignment: .trailing)
    }
}

// MARK: - Live game card

/// Card variant for games currently in progress. Holds its own
/// `LiveFeedViewModel` so each card polls `/feed/live` independently;
/// the parent `ScoresViewModel`'s 30-second schedule poll only
/// covers list-level state (a game flipping from preview → live or
/// live → final). Tapping the card pushes the live BoxScoreView.
private struct LiveGameCard: View {
    let game: Game
    @StateObject private var feed = LiveFeedViewModel()

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            scoreboardRow
            if let live = feed.live?.liveData {
                Divider()
                inGameDetail(live)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .frame(maxWidth: .infinity)
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 16))
        .shadow(color: .black.opacity(0.06), radius: 6, x: 0, y: 2)
        .contentShape(Rectangle())
        .task { await feed.start(gamePk: game.gamePk) }
        .onDisappear { feed.stop() }
    }

    // MARK: Top — team rows + inning + LIVE badge

    private var scoreboardRow: some View {
        HStack(alignment: .center, spacing: 14) {
            VStack(spacing: 8) {
                teamRow(side: game.teams.away)
                teamRow(side: game.teams.home)
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            Divider().frame(height: 56)

            VStack(alignment: .trailing, spacing: 6) {
                LiveBadge()
                inningArrow
            }
            .frame(minWidth: 60, alignment: .trailing)
        }
    }

    private func teamRow(side: GameTeam) -> some View {
        HStack(spacing: 10) {
            TeamLogoView(team: side.team, size: 28)

            Text(side.team.abbreviation ?? String(side.team.name.prefix(3)).uppercased())
                .font(.subheadline.weight(.semibold))
                .lineLimit(1)

            Spacer()

            Text(side.score.map(String.init) ?? "")
                .font(.title3.weight(.semibold))
                .monospacedDigit()
        }
    }

    /// "▲ 7th" / "▼ 9th" — top vs bottom of inning, derived from the
    /// linescore's `isTopInning`. Falls back to the inning ordinal
    /// alone when the half isn't reported (mid-inning / end-inning).
    private var inningArrow: some View {
        let ls = game.linescore ?? feed.live?.liveData.linescore.map(toGameLinescore)
        let ordinal = ls?.currentInningOrdinal
            ?? feed.live?.liveData.linescore?.currentInningOrdinal
            ?? "?"
        let isTop = feed.live?.liveData.linescore?.isTopInning
            ?? game.linescore?.isTopInning
        let arrow: String? = isTop.map { $0 ? "▲" : "▼" }
        return HStack(spacing: 4) {
            if let arrow {
                Text(arrow).font(.caption.weight(.bold))
            }
            Text(ordinal)
                .font(.caption.weight(.semibold))
                .monospacedDigit()
        }
        .foregroundStyle(.red)
    }

    /// Bridge from `LiveLinescore` → `Linescore` shape so the
    /// inning ordinal can be read off either source. The fields
    /// we care about (`currentInningOrdinal`) line up by name.
    private func toGameLinescore(_ live: LiveLinescore) -> Linescore {
        Linescore(
            currentInning: live.currentInning,
            currentInningOrdinal: live.currentInningOrdinal,
            inningState: live.inningState,
            innings: live.innings,
            teams: live.teams,
            scheduledInnings: live.scheduledInnings,
            isTopInning: live.isTopInning,
            balls: live.balls,
            strikes: live.strikes,
            outs: live.outs
        )
    }

    // MARK: Bottom — current matchup, bases + count, last play

    private func inGameDetail(_ live: LiveData) -> some View {
        let ls = live.linescore
        let play = live.plays?.currentPlay
        let batter = play?.matchup?.batter ?? ls?.offense?.batter
        let pitcher = play?.matchup?.pitcher ?? ls?.defense?.pitcher
        let balls = play?.count?.balls ?? ls?.balls ?? 0
        let strikes = play?.count?.strikes ?? ls?.strikes ?? 0
        let outs = play?.count?.outs ?? ls?.outs ?? 0
        return VStack(alignment: .leading, spacing: 8) {
            if let batter, let pitcher {
                Text("\(batter.fullName) vs. \(pitcher.fullName)")
                    .font(.caption.weight(.semibold))
                    .lineLimit(1)
            }
            HStack(spacing: 12) {
                BaseRunnerView(
                    first:  ls?.offense?.first  != nil,
                    second: ls?.offense?.second != nil,
                    third:  ls?.offense?.third  != nil,
                    size: 26
                )
                Text("\(balls)-\(strikes), \(outs) out\(outs == 1 ? "" : "s")")
                    .font(.caption.weight(.semibold))
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
            }
            if let desc = lastPlayDescription(play) {
                Text(desc)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
    }

    /// "Strike swinging" / "Single to left field" — prefer the PA
    /// result description when the AB has resolved; otherwise the
    /// last pitch event's description (mid-PA states).
    private func lastPlayDescription(_ play: LivePlay?) -> String? {
        if let desc = play?.result?.description, !desc.isEmpty { return desc }
        return play?.playEvents?.compactMap(\.details?.description).last
    }
}

#Preview {
    ScoresView()
}
