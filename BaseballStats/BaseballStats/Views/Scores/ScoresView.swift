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
//  Data source is BallDontLie directly via `BallDontLieClient`
//  (no Railway round trip). The view layer still consumes the
//  legacy `Game` model — BDL responses are projected via
//  `BDLGame.toGame()` in Scores.swift so the date strip / cards
//  / navigation don't need to know about the new shape. Player
//  taps in the box score (still MLB-Stats-API-backed in this
//  phase) reach through `APIClient.getPlayerByMlbId` to navigate
//  to the existing `PlayerProfileView`.
//

import Combine
import SwiftUI

/// Division rank + label payload for an expanded game card.
/// `rank` is 1-based within the (league, division) bucket; the
/// view formats as "1st AL East" via `ordinal(_:)`.
struct TeamStandingInfo: Hashable {
    let rank: Int
    /// Pre-formatted like "AL East" / "NL Central". The view
    /// prepends the rank ordinal.
    let divisionLabel: String

    /// "1st" / "2nd" / "3rd" / "4th" / "5th". 0 → "—".
    var displayString: String {
        let ord: String
        switch rank {
        case 1: ord = "1st"
        case 2: ord = "2nd"
        case 3: ord = "3rd"
        default: ord = "\(rank)th"
        }
        return "\(ord) \(divisionLabel)"
    }
}

@MainActor
final class ScoresViewModel: ObservableObject {
    /// Currently selected calendar day. Starts at today (local time).
    @Published var selectedDate: Date = Calendar.current.startOfDay(for: Date())
    @Published var games: [Game] = []
    @Published var isLoading: Bool = false
    /// In-flight `finalize` retry chain, so a second game ending replaces the
    /// first chase rather than running two slate refreshes against each other.
    private var finalizeTask: Task<Void, Never>?
    @Published var error: String?
    /// Set true once a load completes (success or empty); used so the
    /// view can distinguish "still loading" from "no games today".
    @Published var didLoad: Bool = false
    /// BDL team id → current-season W-L. Populated alongside `games`
    /// from `getStandings(season:)`. Cards look up records by the
    /// per-game `bdlAwayTeamId` / `bdlHomeTeamId`.
    @Published var teamRecords: [Int: TeamRecord] = [:]
    /// BDL team id → (division rank, label like "AL East").
    /// Derived from the same standings payload `teamRecords`
    /// uses — rank is computed within the team's (league, division)
    /// bucket sorted by wins desc then losses asc.
    @Published var teamStandings: [Int: TeamStandingInfo] = [:]

    /// BDL's first season of game coverage. Verified against Boston — a club
    /// continuous since 1901 — which returns ZERO games for 1901 / 1950 / 1975
    /// / 1986 / 1990 / 1995 through 1999, and real games with scores from 2000.
    ///
    /// The historical fallback is keyed on the SELECTED DATE, never on BDL
    /// returning an empty list. Emptiness is ambiguous: a BDL outage returns
    /// empty too, and falling back on that would quietly serve game-log-shaped
    /// cards for TODAY — a screen full of finals with no live scores, during an
    /// outage, with nothing to say anything was wrong. A date before the floor
    /// is a fact about coverage; an empty response is a guess about a cause.
    static let bdlFirstSeason = 2000

    /// Whether the slate for `date` must come from our game logs instead of BDL.
    ///
    /// A PURE FUNCTION OF THE DATE, and that is the point: it takes no
    /// response, no error, no count, so there is no input by which a BDL
    /// failure could route today's slate down the historical path. Testable
    /// without a network.
    static func usesHistoricalSource(for date: Date,
                                     calendar: Calendar = .current) -> Bool {
        calendar.component(.year, from: date) < bdlFirstSeason
    }

    /// Games this view WATCHED leave `liveList`. That transition is the only
    /// authoritative "it's over" signal we get: `/live/games` publishes
    /// in-progress games and drops one when it ends, while the BDL slate — the
    /// source of `Game.phase` — can keep saying `.live` for many minutes after
    /// the last out. `finalize` chases the slate for ~2 minutes and
    /// `periodicRefresh` re-reads every 90s, but neither can make a lagging
    /// provider agree, so until it does, `phase` is simply wrong.
    ///
    /// THIS IS WHAT THE STALE CARD WAS. A game that left `liveList` with
    /// `phase` still `.live` is not `.final`, so the old bucketing dropped it
    /// into UPCOMING, where `GameCard.statusLine` took its `.live` branch and
    /// printed "BOT 9th" off a linescore frozen at the last out. It rendered as
    /// its own final inning for as long as the provider lagged.
    ///
    /// Membership-based, not time-based, so it cannot mistake a game that is
    /// STARTING for one that has ended: a game only enters this set by having
    /// been in `liveList` and then leaving it.
    @Published private(set) var endedLocally: Set<Int> = []

    /// Membership from the PREVIOUS tick, so "left the list" means it was
    /// actually in the list. Deliberately NOT derived from `phase` the way
    /// `finalize`'s trigger below is: a game whose slate has flipped to live
    /// before the store's next poll has never been in the list, and treating
    /// its absence as an ending would render a game that is STARTING as FINAL.
    /// A false chase costs one slate read; a false ending is a wrong card.
    private var previouslyInLiveList: Set<Int> = []

    /// THE one question every render path must ask, so bucketing and status
    /// text cannot disagree. Do not re-derive "is it over" from `phase` alone
    /// at a call site — that is the bug this replaces.
    func isOver(_ game: Game) -> Bool {
        LiveStatus.isOver(phaseIsFinal: game.phase == .final,
                          gamePk:       game.gamePk,
                          endedLocally: endedLocally)
    }

    private let bdl: BallDontLieClient
    private let api: APIClient
    /// JUST the slate reads. Everything else still goes through `bdl` — this is
    /// a seam for testing the live→final transition, not an abstraction over the
    /// client. See LiveSeams.swift.
    private let slate: SlateProviding

    private let finalizeDelays: [UInt64]

    init(
        bdl: BallDontLieClient = .shared,
        api: APIClient = .shared,
        slate: SlateProviding? = nil,
        finalizeDelays: [UInt64] = LiveFinalize.defaultDelays,
    ) {
        self.bdl = bdl
        self.api = api
        self.slate = slate ?? bdl
        self.finalizeDelays = finalizeDelays
    }

    /// Fold the shared `LiveGameStore`'s live-games list into `games` — the SAME
    /// merge the old self-fetched loop did, minus the fetch: `LiveGameStore` now
    /// owns the single `/live/games` poll (Phase 2, step 2), and the view feeds
    /// its `liveList` in here on every store update. Only in-progress games
    /// matching a slate game by gamePk get their score / inning refreshed;
    /// finals and scheduled games stay as the initial `load` left them. When a
    /// game that WAS live drops out of the live set (it ended), do one full
    /// `load` to capture its final state + records.
    func applyLiveList(_ liveById: [Int: LiveGameSummary], date: Date) async {
        guard Calendar.current.isDateInToday(date) else { return }
        let wereLive = Set(games.filter { $0.phase == .live }.map(\.gamePk))

        games = games.map { g in
            liveById[g.gamePk].map { g.merging(live: $0) } ?? g
        }

        // Rendering authority: only games we actually SAW in the list and then
        // saw leave it. Recorded BEFORE the chase, because `finalize` may take
        // two minutes to make the slate agree, or never — the card must read
        // FINAL from this instant regardless.
        let leftTheList = previouslyInLiveList.subtracting(liveById.keys)
        if !leftTheList.isEmpty {
            endedLocally.formUnion(leftTheList)
        }
        previouslyInLiveList = Set(liveById.keys)

        // The chase trigger stays phase-based. It is a cheap, self-limiting
        // slate re-read, so a spurious one is harmless — and narrowing it to
        // list membership would lose the case where the app opened mid-game.
        let ended = wereLive.subtracting(liveById.keys)
        if !ended.isEmpty {
            finalize(ended, date: date)   // a game finished — chase its final state
        }
    }

    /// FAST PATH for a game the app watched end. No longer the remedy — the
    /// periodic slate refresh (see `periodicRefresh` in the view body) is what
    /// guarantees a finished game stops rendering as live, and it covers every
    /// game rather than only those observed live. This chain survives because it
    /// is much faster where it applies: it fires the instant a game leaves the
    /// live list and bypasses the 30s cache, so the last out reads FINAL in a
    /// few seconds instead of up to 90. If it is ever deleted, the refresh still
    /// converges — the user just waits longer at the one moment they are
    /// watching.
    ///
    /// Re-read the slate until the just-ended games are no longer `.live`.
    ///
    /// Replaces a single `await load(date:)`. That was wrong twice over. It was
    /// EDGE-TRIGGERED — `applyLiveList` only runs when `liveList` changes, and
    /// once a finished game drops out the list settles and never fires again, so
    /// there was exactly one attempt. And that one attempt read
    /// `getGames(date:)`, whose 30-second cache had just been filled by the
    /// live-polling that was running moments earlier, so it usually returned the
    /// pre-final slate. One shot, spent on a stale read: the game stayed `.live`
    /// until the app was relaunched.
    ///
    /// So both halves are fixed here — every attempt bypasses the cache, and
    /// there are several, spaced out, to outlast the provider taking its time to
    /// mark the game final. The loop stops as soon as no game in `ended` is still
    /// live, so the normal case is one call.
    private func finalize(_ ended: Set<Int>, date: Date) {
        finalizeTask?.cancel()
        finalizeTask = Task { @MainActor [weak self] in
            for delay in finalizeDelays {
                if delay > 0 {
                    try? await Task.sleep(nanoseconds: delay * 1_000_000_000)
                }
                guard !Task.isCancelled, let self else { return }
                await self.load(date: date, bypassCache: true)
                guard !Task.isCancelled else { return }
                let stillLive = Set(self.games.filter { $0.phase == .live }.map(\.gamePk))
                if ended.isDisjoint(with: stillLive) { return }   // all resolved
            }
        }
    }

    func load(date: Date, bypassCache: Bool = false) async {
        isLoading = true
        error = nil
        // Year-of-the-selected-date is the right key for standings —
        // a user scrolling back to October 2024 should see the 2024
        // records, not the live 2026 ones.
        let year = Calendar.current.component(.year, from: date)
        async let standingsTask: [BDLStandingsEntry]? = try? bdl.getStandings(season: year)
        if Self.usesHistoricalSource(for: date) {
            // Before BDL's floor: our own game logs are the only source.
            do {
                self.games = try await api.getHistoricalGames(date: date)
                    .sorted { ($0.teams.away.team.abbreviation ?? "")
                            < ($1.teams.away.team.abbreviation ?? "") }
                self.error = nil
            } catch {
                self.error = Self.message(for: error)
                self.games = []
            }
        } else {
            do {
                let bdlGames = try await slate.getGames(date: ScoresViewModel.iso(date),
                                                         bypassCache: bypassCache)
                self.games = bdlGames
                    .map { $0.toGame() }
                    .sorted { ($0.startDate ?? .distantFuture) < ($1.startDate ?? .distantFuture) }
            } catch {
                self.error = Self.message(for: error)
                self.games = []
            }
        }
        // Keep only ids still on the slate — switching to another date must not
        // inherit today's ended games. A pk that is now genuinely `.final` can
        // stay; `isOver` would answer true either way.
        self.endedLocally.formIntersection(Set(self.games.map(\.gamePk)))
        // Awaited AFTER the games await so a slow standings fetch
        // can't block the score cards from rendering. nil-coalesces
        // to an empty dict when standings fail or BDL ships an
        // empty payload — records just won't render that tick.
        let standings = (await standingsTask) ?? []
        self.teamRecords   = Self.recordsByBDLTeamId(standings)
        self.teamStandings = Self.standingsByBDLTeamId(standings)
        applyTodayAdjustments()
        isLoading = false
        didLoad = true
    }

    /// Pull-to-refresh variant: same fetch, but a network failure
    /// keeps the existing game list visible instead of replacing it
    /// with an error screen. The user just sees the spinner stop —
    /// the next normal `load()` will surface persistent failures.
    ///
    /// - Parameter clearingCache: drop every entry in the BDL in-process cache
    ///   first. TRUE for a deliberate pull-to-refresh, where the user is asking
    ///   for the freshest possible answer and an in-window cached value would
    ///   read as the gesture having done nothing. FALSE for the periodic
    ///   background refresh: that fires every 90s against a 30s cache, so it
    ///   misses the cache on its own merits, and clearing the WHOLE cache on a
    ///   timer would evict standings, rosters and lineups that no one asked to
    ///   re-fetch.
    ///
    /// Non-destructive by design — this is what the periodic refresh needs, and
    /// why it reuses this rather than `load`: `load` sets `isLoading` (a spinner
    /// every 90s) and empties `games` on failure (a blank screen on one dropped
    /// request).
    func refresh(clearingCache: Bool = true) async {
        if clearingCache { bdl.clearCache() }
        let year = Calendar.current.component(.year, from: selectedDate)
        async let standingsTask: [BDLStandingsEntry]? = try? bdl.getStandings(season: year)
        do {
            let bdlGames = try await bdl.getGames(date: ScoresViewModel.iso(selectedDate))
            self.games = bdlGames
                .map { $0.toGame() }
                .sorted { ($0.startDate ?? .distantFuture) < ($1.startDate ?? .distantFuture) }
            self.error = nil
        } catch {
            // Silent — keep stale games visible rather than wiping
            // the screen on a transient pull-to-refresh hiccup.
        }
        if let standings = await standingsTask {
            self.teamRecords   = Self.recordsByBDLTeamId(standings)
            self.teamStandings = Self.standingsByBDLTeamId(standings)
            applyTodayAdjustments()
        }
    }

    /// Re-fetch standings only (no games re-fetch). Called when the
    /// auto-refresh polling loop detects a Live → Final transition —
    /// the just-completed game produced a W or L delta that should
    /// show up on the score cards immediately and on the Standings
    /// tab via the broadcast notification. `bypassCache: true`
    /// guarantees we don't read whatever's still in the 5-minute
    /// standings cache window.
    func refreshStandings() async {
        let year = Calendar.current.component(.year, from: selectedDate)
        do {
            let standings = try await bdl.getStandings(season: year, bypassCache: true)
            self.teamRecords   = Self.recordsByBDLTeamId(standings)
            self.teamStandings = Self.standingsByBDLTeamId(standings)
            applyTodayAdjustments()
            NotificationCenter.default.post(name: .standingsShouldRefresh, object: nil)
        } catch {
            // Silent — the dict keeps its previous values; the next
            // `load()` tick will retry.
        }
    }

    /// Fold today's ET final games (from the already-loaded `games`)
    /// into `teamRecords` so the score cards' "(W-L)" matches the
    /// Standings tab. Silent (no "†") per the Scores/Home design.
    ///
    /// KNOWN GAP, deliberately left as it was. This is `.unanchored` with a nil
    /// cutoff, so every today-ET final in the slate is applied — including any
    /// BDL has already absorbed into the base, which double-counts exactly the
    /// way Home's did. Home could be fixed because it already fetches our
    /// backend's standings and so has a games-played count anchored to a known
    /// time; this view model fetches BDL only, so the anchor does not exist
    /// here without adding a request. Fixing it means giving this view model
    /// that fetch — its own change, not a rider on Home's.
    private func applyTodayAdjustments() {
        let deltas = TodayRecordAdjustments.deltas(
            from: games, lastUpdated: nil, absorption: .unanchored,
        )
        teamRecords = TodayRecordAdjustments.apply(deltas, to: teamRecords)
    }

    private static func recordsByBDLTeamId(
        _ standings: [BDLStandingsEntry],
    ) -> [Int: TeamRecord] {
        var dict: [Int: TeamRecord] = [:]
        for s in standings {
            dict[s.team.id] = TeamRecord(
                wins:   s.wins,
                losses: s.losses,
                pct:    nil,
            )
        }
        return dict
    }

    /// Build `{bdl_team_id: (rank, "AL East")}` from a BDL standings
    /// response. Ranks are computed within each (league, division)
    /// bucket — sorted by wins desc, losses asc — so the leading
    /// team in each division gets rank 1 ("1st AL East") and so on.
    /// Teams missing a league / division code (BDL data quirk) are
    /// skipped.
    private static func standingsByBDLTeamId(
        _ standings: [BDLStandingsEntry],
    ) -> [Int: TeamStandingInfo] {
        var buckets: [String: [BDLStandingsEntry]] = [:]
        for s in standings {
            guard let lg = leagueCode(s.team.league),
                  let div = divisionCode(s.team.division) else { continue }
            buckets["\(lg) \(div)", default: []].append(s)
        }
        var out: [Int: TeamStandingInfo] = [:]
        for (divisionLabel, entries) in buckets {
            let sorted = entries.sorted {
                if $0.wins != $1.wins { return $0.wins > $1.wins }
                return $0.losses < $1.losses
            }
            for (i, e) in sorted.enumerated() {
                out[e.team.id] = TeamStandingInfo(
                    rank:          i + 1,
                    divisionLabel: divisionLabel,
                )
            }
        }
        return out
    }

    /// "American" → "AL", "National" → "NL". nil for unknown values
    /// so the caller can skip standings for malformed standings rows.
    private static func leagueCode(_ s: String?) -> String? {
        switch s {
        case "American": return "AL"
        case "National": return "NL"
        default:         return nil
        }
    }

    /// "East" / "Central" / "West" — pass through. nil otherwise.
    private static func divisionCode(_ s: String?) -> String? {
        switch s {
        case "East":    return "East"
        case "Central": return "Central"
        case "West":    return "West"
        default:        return nil
        }
    }

    static func iso(_ date: Date) -> String {
        // `yyyy-MM-dd` in local timezone — what BDL's `dates[]`
        // filter expects, and what the date-strip pills key on.
        Self.scheduleDateFormatter.string(from: date)
    }

    static let scheduleDateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.calendar = .init(identifier: .gregorian)
        f.timeZone = .current
        f.locale   = .init(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()

    private static func message(for error: Error) -> String {
        // Prefer the typed-error's `LocalizedError` description
        // when available — `BallDontLieError` ships friendly
        // strings via `errorDescription`. The bare
        // `error.localizedDescription` returns the right text via
        // the same conformance, so we can route everything through
        // the single call here instead of pattern-matching cases.
        return error.localizedDescription
    }
}

struct ScoresView: View {
    @StateObject private var vm = ScoresViewModel()
    @EnvironmentObject private var navigation: AppNavigation
    @EnvironmentObject private var liveStore: LiveGameStore
    /// Stable token for this tab's refcounted hold on the shared list loop, so
    /// Home↔Scores switching can't cancel a loop the other tab still needs.
    @State private var listSubscriberID = LiveGameStore.SubscriberID()
    @State private var navigationPath = NavigationPath()
    @State private var showingDatePicker = false

    var body: some View {
        NavigationStack(path: $navigationPath) {
            VStack(spacing: 0) {
                dateBar
                content
            }
            .navigationTitle("Scores")
            .navigationBarTitleDisplayMode(.inline)
            .navigationDestination(for: Game.self) { game in
                BoxScoreView(
                    game:           game,
                    teamStandings:  vm.teamStandings,
                    teamRecords:    vm.teamRecords,
                    path:           $navigationPath,
                    owningTab:      .scores,
                    navigation:     navigation,
                    liveStore:      liveStore,
                )
            }
            .navigationDestination(for: PlayerSearchResult.self) { player in
                PlayerProfileView(player: player)
            }
            .sheet(isPresented: $showingDatePicker) {
                datePickerSheet
            }
        }
        .task { await vm.load(date: vm.selectedDate) }
        // Re-read the slate while the tab is visible. This — not `finalize` — is
        // what makes a finished game show as finished. `finalize` only fires for
        // games the app already had as `.live`, so a game that went live AND
        // ended while this tab sat idle was never touched by anything: it kept
        // rendering its start time hours later. Cache-respecting (`clearingCache:
        // false`) and quiet, so a tick costs one 30s-cache miss and can never
        // blank the list.
        .periodicRefresh(
            every: RefreshCadence.slate,
            isActive: navigation.shouldPoll(on: .scores),
        ) {
            await vm.refresh(clearingCache: false)
        }
        // Drive the SHARED LiveGameStore list loop from the Scores tab's
        // lifecycle, gated exactly like the old per-VM loop was (Phase 2, step 2).
        .task {
            if navigation.shouldPoll(on: .scores) { liveStore.subscribeList(owner: listSubscriberID) }
        }
        // Fold each fresh /live/games snapshot from the store into `games`
        // (score / inning) — the merge the deleted refreshLive loop used to do,
        // now sourced from the store's shared list instead of a self-fetch.
        .onChange(of: liveStore.liveList) { _, list in
            Task { await vm.applyLiveList(list, date: vm.selectedDate) }
        }
        // Detect Live → Final transitions so the Standings tab can pull in the
        // just-completed W/L delta without waiting for a tab switch or the next
        // nightly run. (The auto-refresh re-arm that used to live here is gone —
        // the store list loop is driven by shouldPoll below.)
        .onChange(of: vm.games) { oldGames, newGames in
            let wasLive = Set(oldGames.filter { $0.phase == .live }.map(\.gamePk))
            let nowFinal = newGames.filter { $0.phase == .final }.map(\.gamePk)
            if nowFinal.contains(where: { wasLive.contains($0) }) {
                // Cache-bypassing refetch updates this tab's record
                // dict immediately AND posts the broadcast so the
                // Standings tab pulls in the new W/L delta on its
                // next foreground.
                Task { await vm.refreshStandings() }
            }
        }
        // Pause the store list loop on background / switch away from the Scores
        // tab; resume with an immediate refresh on return.
        .onChange(of: navigation.shouldPoll(on: .scores)) { _, canPoll in
            if canPoll {
                liveStore.subscribeList(owner: listSubscriberID)
            } else {
                liveStore.unsubscribeList(owner: listSubscriberID)
            }
        }
        .onDisappear { liveStore.unsubscribeList(owner: listSubscriberID) }
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
        // No per-VM loop to stop anymore — the shared LiveGameStore list loop is
        // driven by shouldPoll(.scores), and applyLiveList is today-gated, so a
        // date change just reloads the slate; the next store tick folds live
        // scores back in when the date is today.
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
        // LIVE membership comes from the store's live list (the backend's
        // definition of in-progress), NOT the frozen schedule status — so a game
        // that flips to live mid-view moves into the Live section on the next
        // store update instead of staying stuck in Upcoming. Non-live games fall
        // back to their schedule status: Final if completed, else Upcoming.
        let liveIds = Set(liveStore.liveList.keys)
        // `LiveStatus` rather than a bare membership test: it is the tested
        // invariant (see LiveSeams.swift) and it holds the one case a bare test
        // gets wrong — before the store has answered at all, `listLoaded` is
        // false and the schedule's phase is the best we have. BoxScoreView has
        // used it since the live/final transition bugs; this view had its own
        // copy of the rule and drifted.
        let isLiveNow: (Game) -> Bool = { g in
            LiveStatus.isLive(inLiveList:  liveIds.contains(g.gamePk),
                              listLoaded:  liveStore.listLoaded,
                              phaseIsLive: g.phase == .live)
        }
        let live = vm.games
            .filter(isLiveNow)
            .sorted { ($0.linescore?.currentInning ?? 0) > ($1.linescore?.currentInning ?? 0) }
        let notLive = vm.games.filter { !isLiveNow($0) }
        let upcoming = notLive
            .filter { !vm.isOver($0) }
            // On-time games first (earliest start), then postponed games
            // sink to the bottom — they're not happening today, so they
            // shouldn't crowd out the games that are.
            .sorted { a, b in
                let aPpd = a.phase == .postponed
                let bPpd = b.phase == .postponed
                if aPpd != bPpd { return !aPpd }
                return (a.startDate ?? .distantFuture) < (b.startDate ?? .distantFuture)
            }
        // `isOver`, not `phase == .final` — a game that left `liveList` belongs
        // here the moment it does, not whenever the provider catches up. This
        // is also what makes the card REDRAW: moving between sections gives the
        // row a new view identity, so a FinalGameCard is constructed and its
        // body evaluated. Recomputing a value inside a card that never
        // re-renders would have left the same stale pixels on screen.
        let completed = notLive
            .filter { vm.isOver($0) }
            .sorted { ($0.startDate ?? .distantPast) > ($1.startDate ?? .distantPast) }

        return ScrollView {
            LazyVStack(spacing: 12) {
                // EVERY section builds the SAME view type. That is the fix for
                // a finished game drawing its last inning under the Completed
                // header: a row keeps one identity across a section move (so
                // the move can animate), and a stable identity whose type
                // changed leaves SwiftUI holding the old rendering. The
                // branching — including whether a NavigationLink wraps the card
                // at all — lives inside `GameRowCard`.
                if !live.isEmpty {
                    sectionHeader("Live")
                    ForEach(live) { game in
                        GameRowCard(
                            game:      game,
                            records:   vm.teamRecords,
                            standings: vm.teamStandings,
                            isOver:    vm.isOver(game),
                            path:      $navigationPath,
                        )
                    }
                }
                if !upcoming.isEmpty {
                    sectionHeader("Upcoming")
                    ForEach(upcoming) { game in
                        GameRowCard(
                            game:      game,
                            records:   vm.teamRecords,
                            standings: vm.teamStandings,
                            isOver:    vm.isOver(game),
                            path:      $navigationPath,
                        )
                    }
                }
                if !completed.isEmpty {
                    sectionHeader("Completed")
                    ForEach(completed) { game in
                        GameRowCard(
                            game:      game,
                            records:   vm.teamRecords,
                            standings: vm.teamStandings,
                            isOver:    vm.isOver(game),
                            path:      $navigationPath,
                        )
                    }
                }
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 12)
            // Animate only when live membership changes (a game enters/leaves the
            // Live section), not on every score tick — so an Upcoming→Live move
            // slides smoothly instead of snapping. `Game.id` (gamePk) identity
            // keeps rows stable across the section move.
            .animation(.default, value: liveIds)
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
    let records: [Int: TeamRecord]
    /// From `ScoresViewModel.isOver`. Belt-and-braces: with the bucketing fixed
    /// a finished game renders as `FinalGameCard` and never reaches here, but
    /// this card is the ONLY thing in the app that can print "BOT 9th", so it
    /// refuses to do so for a game known to be over no matter who calls it.
    var isOver: Bool = false

    var body: some View {
        HStack(alignment: .center, spacing: 14) {
            VStack(spacing: 8) {
                teamRow(side:       game.teams.away,
                        winner:     didWin(side: game.teams.away),
                        bdlTeamId:  game.bdlAwayTeamId)
                teamRow(side:       game.teams.home,
                        winner:     didWin(side: game.teams.home),
                        bdlTeamId:  game.bdlHomeTeamId)
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
        .shadow(color: .black.opacity(0.06), radius: 8, x: 0, y: 3)
        .contentShape(Rectangle())
    }

    private func teamRow(side: GameTeam, winner: Bool, bdlTeamId: Int?) -> some View {
        HStack(spacing: 10) {
            // Colour, not a circle of letters: the abbreviation is the very
            // next thing in the row. 18pt matches the .subheadline line it leads.
            TeamColorSwatch(code: bdlTeamId.flatMap { bdlToLahmanTeamId[$0] })

            Text(side.team.abbreviation ?? abbreviate(side.team.name))
                .font(.subheadline.weight(winner ? .bold : .semibold))
                .foregroundStyle(loserDimmed(winner) ? .secondary : .primary)
                .lineLimit(1)

            if let w = bdlTeamId.flatMap({ records[$0] })?.wins,
               let l = bdlTeamId.flatMap({ records[$0] })?.losses {
                Text("(\(w)-\(l))")
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

    /// "FINAL" / "Top 7th" / "7:05 PM" / "PPD" / "DELAYED" / detailed-
    /// state pass-through.
    private var statusLine: String {
        // Checked BEFORE the switch: `phase` is the provider's opinion and it
        // lags the last out by minutes. `isOver` already folds in the one
        // authoritative signal (the game left `liveList`), so trusting `phase`
        // first is what printed a frozen inning on a finished game.
        if isOver { return "FINAL" }
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
        case .postponed:
            return "PPD"
        case .preview:
            // A delayed game keeps its `.preview` phase (it may still be
            // played today) — surface the delay in place of the now-
            // unreliable start time.
            if game.status.detailedState == "Delayed" {
                return "DELAYED"
            }
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
        case .preview, .postponed:
            if let venue = game.venue?.name { return venue }
            return nil
        case .other:
            return nil
        }
    }

    private var statusColor: Color {
        switch game.phase {
        case .final:     return .secondary
        case .live:      return .red
        case .postponed: return .orange
        case .preview:
            // Delayed previews share the postponed accent so the
            // schedule-disrupted state reads at a glance.
            return game.status.detailedState == "Delayed" ? .orange : .primary
        case .other:     return .secondary
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
    let records: [Int: TeamRecord]
    @Binding var path: NavigationPath
    @State private var isExpanded = false
    /// Lazily-fetched box score for the expanded view. Loaded the
    /// first time the user expands the card so decision-pitcher
    /// records (W/L: …) and the HR summary line have data to
    /// render. Cached for the lifetime of the card so subsequent
    /// expand/collapse cycles don't re-hit the API.
    @State private var boxScore: BoxScoreResponse?
    @State private var isLoadingBoxScore = false
    /// Cumulative W-L-SV through this game's ET date for each
    /// decision pitcher, keyed by BDL player id. Fetched after
    /// `boxScore` lands; lets `decisionLine` render the post-game
    /// record without leaning on BDL's possibly-stale season-stats
    /// snapshot.
    @State private var pitcherRecordsByBDL: [Int: PitcherRecord] = [:]
    /// Point-in-time HR / 2B / 3B totals for each batter with a
    /// notable hit in the game. Same key scheme + lifecycle as
    /// `pitcherRecordsByBDL`. Drives the HR summary line.
    @State private var batterStatsAtDateByBDL: [Int: BatterStatsAtDate] = [:]

    var body: some View {
        VStack(spacing: 10) {
            collapsedBody
                // A historical game has nothing to expand INTO: the game-log
                // tables carry no per-inning linescore and no decisions, and
                // its negative gamePk would send the box score to BDL with an
                // id BDL has never issued. So it does not offer the tap at all
                // — a card that opens nothing is worse than one that never
                // invited the gesture. Deliberate, not an oversight.
                .contentShape(Rectangle())
                .onTapGesture {
                    guard !game.isHistorical else { return }
                    // Fire the fetch BEFORE the expand animation so
                    // the network round-trip starts while the 0.22s
                    // animation is still running. Without this, the
                    // expanded view paints with empty dicts, then the
                    // fetch lands ~0.5s later and the values pop in
                    // — visible flash. Predicate uses `!isExpanded`
                    // because we kick off the fetch only when the
                    // tap transitions collapsed → expanded.
                    if !isExpanded && boxScore == nil && !isLoadingBoxScore {
                        Task { await fetchBoxScore() }
                    }
                    withAnimation(.easeInOut(duration: 0.22)) {
                        isExpanded.toggle()
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
        .shadow(color: .black.opacity(0.06), radius: 8, x: 0, y: 3)
    }

    private func fetchBoxScore() async {
        guard !isLoadingBoxScore else { return }
        isLoadingBoxScore = true
        defer { isLoadingBoxScore = false }

        let bdl = BallDontLieClient.shared
        // Run the same shape as `BoxScoreView.loadBoxScore`: stats +
        // lineup in parallel, then season stats keyed by the lineup
        // pids so the placeholder pitchers carry season W-L-SV. The
        // synthesizer wires everything up, after which the decisions
        // section can read each side's pitching flags + the post-game
        // record adjustment.
        async let statsTask  = bdl.getGameStats(gameId: game.gamePk)
        async let lineupTask = bdl.getGameLineup(gameId: game.gamePk)
        let lineup = (try? await lineupTask) ?? []
        guard let stats = try? await statsTask else { return }

        // Cover lineup pids AND stats-side pids (relievers / pinch
        // hitters not in the lineup) so the decisions section has
        // each pitcher's season W-L-SV available. Without the
        // stats-side union, a reliever who got the W shows "(W)"
        // with no record.
        let pids = Array(Set(lineup.map(\.player.id))
                              .union(stats.map(\.player.id)))
        let season = Calendar.current.component(.year, from: game.startDate ?? Date())
        let seasonByPid: [Int: BDLSeasonStat] = await {
            guard !pids.isEmpty else { return [:] }
            if let rows = try? await bdl.getSeasonStats(playerIds: pids, season: season) {
                return Dictionary(rows.map { ($0.player.id, $0) }, uniquingKeysWith: { a, _ in a })
            }
            return [:]
        }()

        // BDL team objects: stub from the Game's TeamInfo. This
        // expanded card doesn't need accurate logos / division (the
        // synthesizer reads `id` for lineup splits and `name` for
        // stats-row bucketing), so stub coverage is enough.
        let awayBDL = Self.stubBDLTeam(
            from: game.teams.away.team,
            bdlTeamId: game.bdlAwayTeamId,
        )
        let homeBDL = Self.stubBDLTeam(
            from: game.teams.home.team,
            bdlTeamId: game.bdlHomeTeamId,
        )
        boxScore = stats.toBoxScoreResponse(
            awayTeam:         awayBDL,
            homeTeam:         homeBDL,
            awayBDLTeamId:    game.bdlAwayTeamId,
            homeBDLTeamId:    game.bdlHomeTeamId,
            lineup:           lineup,
            seasonStatsByPid: seasonByPid,
        )
        async let p: Void = loadPitcherRecords()
        async let b: Void = loadBatterStatsAtDate()
        _ = await p
        _ = await b
    }

    /// Same shape + fail-soft semantics as `loadPitcherRecords`.
    /// Picks the batters with HR / 2B / 3B > 0 in this game and
    /// fetches their point-in-time totals through `game.startDate`.
    private func loadBatterStatsAtDate() async {
        guard let bs = boxScore else { return }
        guard let gameDate = Self.etDateString(from: game.startDate) else { return }
        var notableBdlIds: Set<Int> = []
        for team in [bs.teams.away, bs.teams.home] {
            for (_, p) in team.players {
                guard let bat = p.stats?.batting else { continue }
                if (bat.homeRuns ?? 0) > 0
                    || (bat.doubles  ?? 0) > 0
                    || (bat.triples  ?? 0) > 0 {
                    notableBdlIds.insert(p.person.id)
                }
            }
        }
        guard !notableBdlIds.isEmpty else { return }
        let bdl = BallDontLieClient.shared
        let api = APIClient.shared
        let pairs = await withTaskGroup(
            of: (Int, BatterStatsAtDate)?.self,
        ) { group in
            for bdlId in notableBdlIds {
                group.addTask {
                    guard let player = try? await bdl.resolveBDLPlayerId(bdlId)
                    else { return nil }
                    let outer = try? await api.getBatterStatsAtDate(
                        playerId: player.player_id, gameDate: gameDate,
                    )
                    guard let stats = outer ?? nil else { return nil }
                    return (bdlId, stats)
                }
            }
            var out: [(Int, BatterStatsAtDate)] = []
            for await m in group {
                if let pair = m { out.append(pair) }
            }
            return out
        }
        var dict: [Int: BatterStatsAtDate] = [:]
        for (bdlId, s) in pairs { dict[bdlId] = s }
        self.batterStatsAtDateByBDL = dict
    }

    /// Same shape as `BoxScoreViewModel.loadPitcherRecords` — keeps
    /// the expand-on-tap card honest when BDL's season snapshot is
    /// post-absorption stale. Stored only by BDL id since this card
    /// renders by BDL id throughout.
    private func loadPitcherRecords() async {
        guard let bs = boxScore else { return }
        guard let gameDate = Self.etDateString(from: game.startDate) else { return }
        var decisionBdlIds: Set<Int> = []
        for team in [bs.teams.away, bs.teams.home] {
            for (_, p) in team.players {
                guard let g = p.stats?.pitching else { continue }
                if (g.wins ?? 0) > 0 || (g.losses ?? 0) > 0 || (g.saves ?? 0) > 0 {
                    decisionBdlIds.insert(p.person.id)
                }
            }
        }
        guard !decisionBdlIds.isEmpty else { return }
        let bdl = BallDontLieClient.shared
        let api = APIClient.shared
        let pairs = await withTaskGroup(
            of: (Int, PitcherRecord)?.self,
        ) { group in
            for bdlId in decisionBdlIds {
                group.addTask {
                    guard let player = try? await bdl.resolveBDLPlayerId(bdlId)
                    else { return nil }
                    let outer = try? await api.getPitcherRecordAtDate(
                        playerId: player.player_id, gameDate: gameDate,
                    )
                    guard let record = outer ?? nil else { return nil }
                    return (bdlId, record)
                }
            }
            var out: [(Int, PitcherRecord)] = []
            for await m in group {
                if let pair = m { out.append(pair) }
            }
            return out
        }
        var dict: [Int: PitcherRecord] = [:]
        for (bdlId, rec) in pairs { dict[bdlId] = rec }
        self.pitcherRecordsByBDL = dict
    }

    /// Wrap the existing `etDateFormatter` (defined alongside
    /// `isGameToday` later in this struct) into a nil-safe helper
    /// so the records loader can stay concise.
    private static func etDateString(from date: Date?) -> String? {
        guard let date else { return nil }
        return etDateFormatter.string(from: date)
    }

    private static func stubBDLTeam(from info: TeamInfo, bdlTeamId: Int?) -> BDLTeam {
        let abbr = info.abbreviation ?? String(info.name.prefix(3)).uppercased()
        return BDLTeam(
            id:                bdlTeamId ?? 0,
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

    // MARK: Collapsed header

    private var collapsedBody: some View {
        HStack(alignment: .center, spacing: 14) {
            VStack(spacing: 8) {
                teamRow(side: game.teams.away, bdlTeamId: game.bdlAwayTeamId)
                teamRow(side: game.teams.home, bdlTeamId: game.bdlHomeTeamId)
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            Divider().frame(height: 56)

            Text("FINAL")
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.secondary)
                .frame(minWidth: 56, alignment: .trailing)
        }
    }

    private func teamRow(side: GameTeam, bdlTeamId: Int?) -> some View {
        let isWinner = side.isWinner == true
        let dimmed = !isWinner
        return HStack(spacing: 10) {
            // Colour, not a circle of letters: the abbreviation is the very
            // next thing in the row. 18pt matches the .subheadline line it leads.
            TeamColorSwatch(code: bdlTeamId.flatMap { bdlToLahmanTeamId[$0] })

            Text(side.team.abbreviation ?? String(side.team.name.prefix(3)).uppercased())
                .font(.subheadline.weight(isWinner ? .bold : .semibold))
                .foregroundStyle(dimmed ? .secondary : .primary)
                .lineLimit(1)

            if let w = bdlTeamId.flatMap({ records[$0] })?.wins,
               let l = bdlTeamId.flatMap({ records[$0] })?.losses {
                Text("(\(w)-\(l))")
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
                lineRow(label: linescoreLabel(
                            for: game.teams.away,
                            bdlTeamId: game.bdlAwayTeamId,
                        ),
                        innings: (1...inningCount).map { i in
                            cell(innings.first(where: { $0.num == i })?.away?.runs)
                        },
                        totals: [
                            cell(totals?.away?.runs),
                            cell(totals?.away?.hits),
                            cell(totals?.away?.errors),
                        ],
                        isHeader: false)
                lineRow(label: linescoreLabel(
                            for: game.teams.home,
                            bdlTeamId: game.bdlHomeTeamId,
                        ),
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
                // Widened from 44pt to 88pt so "LAD (32-14)" fits
                // on one line alongside the inning columns. Header
                // row passes "" so the empty header still aligns.
                .frame(width: 88, alignment: .leading)
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

    /// "LAD (32-14)" — abbreviation + record when the BDL standings
    /// lookup hits. Falls back to bare abbreviation when the team
    /// isn't in the records dict (early-season cold start, or a
    /// season the standings endpoint doesn't cover yet).
    private func linescoreLabel(for side: GameTeam, bdlTeamId: Int?) -> String {
        let abbr = side.team.abbreviation
            ?? String(side.team.name.prefix(3)).uppercased()
        if let w = bdlTeamId.flatMap({ records[$0] })?.wins,
           let l = bdlTeamId.flatMap({ records[$0] })?.losses {
            return "\(abbr) (\(w)-\(l))"
        }
        return abbr
    }

    // MARK: Expanded — decisions

    /// `(winner, loser, saver)` derived from the per-game pitching
    /// flags on the synthesized box score. BDL ships `wins/losses/saves`
    /// as 0/1 per row; whichever pitcher has a `1` is the decision
    /// holder. `saver` is nil when no pitcher recorded a save (close
    /// games, ties, blown saves).
    private var decisionPitchers: (winner: BoxPlayer?, loser: BoxPlayer?, saver: BoxPlayer?) {
        guard let bs = boxScore else { return (nil, nil, nil) }
        var winner: BoxPlayer?
        var loser: BoxPlayer?
        var saver: BoxPlayer?
        for team in [bs.teams.away, bs.teams.home] {
            for (_, p) in team.players {
                guard let g = p.stats?.pitching else { continue }
                if (g.wins ?? 0)   > 0 { winner = p }
                if (g.losses ?? 0) > 0 { loser  = p }
                if (g.saves ?? 0)  > 0 { saver  = p }
            }
        }
        return (winner, loser, saver)
    }

    private var hasAnyDecision: Bool {
        let d = decisionPitchers
        return d.winner != nil || d.loser != nil || d.saver != nil
    }

    /// W: / L: / SV: lines, each with the decision pitcher's
    /// post-game record. Season W-L-SV come from the lineup's
    /// season-stats pre-load (carried on `seasonStats.pitching` and
    /// preserved across the merge). BDL's per-game `seasonStats`
    /// already reflects the post-game total, so today's decision is
    /// included without a manual `+1`. nil season stats → bare name.
    private var decisions: some View {
        let d = decisionPitchers
        return VStack(alignment: .leading, spacing: 4) {
            if let w = d.winner { decisionLine(tag: "W",  pitcher: w) }
            if let l = d.loser  { decisionLine(tag: "L",  pitcher: l) }
            if let s = d.saver  { decisionLine(tag: "SV", pitcher: s) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        // Animate the `(—)` → `(W X-Y)` swap when the pitcher-
        // record fetch lands. Same value-driven animation as
        // `hrSummary` so the two transitions line up visually.
        .animation(
            .easeIn(duration: 0.2),
            value: pitcherRecordsByBDL,
        )
    }

    private func decisionLine(tag: String, pitcher: BoxPlayer) -> some View {
        // While the contextual record is still loading, show "(—)"
        // rather than the BDL placeholder + bump. Same rationale
        // as `hrSegments` — the flash of a wrong intermediate
        // value is worse than a brief dash. Animated below.
        let recordText: String? = {
            guard let rec = pitcherRecordsByBDL[pitcher.person.id] else {
                return "(—)"
            }
            // `includesToday` gate matches the box-score's
            // `pitcherDecisionTag`: bump only when this is today's
            // game AND the gamelog ingest hasn't reached today yet.
            let recBump = !rec.includesToday ? 1 : 0
            switch tag {
            case "W":  return "(\(rec.wins + recBump)-\(rec.losses))"
            case "L":  return "(\(rec.wins)-\(rec.losses + recBump))"
            case "SV": return "(\(rec.saves + recBump))"
            default:   return nil
            }
        }()
        return HStack(spacing: 6) {
            Text("\(tag):")
                .font(.caption.weight(.bold))
                .foregroundStyle(.secondary)
                .frame(width: 28, alignment: .leading)
                .monospacedDigit()
            Text(pitcher.person.fullName + (recordText.map { " \($0)" } ?? ""))
                .font(.caption)
                .foregroundStyle(.primary)
                .lineLimit(1)
        }
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
                    // Animate the `(—)` → `(N)` swap when the
                    // batter-stats-at-date fetch lands. Keyed off
                    // the dict so additional players appearing
                    // mid-fetch also slide in instead of popping.
                    .animation(
                        .easeIn(duration: 0.2),
                        value: batterStatsAtDateByBDL,
                    )
            }
        }
    }

    /// Per-batter "Lastname (season HR)" pieces ordered by the
    /// batting-order index for each side. Both teams are walked so
    /// the line surfaces every HR in the game, not just the
    /// winners'.
    private func hrSegments(from bs: BoxScoreResponse) -> [String] {
        // `seasonStats.batting.homeRuns` is the PRE-game cumulative
        // loaded once from BDL's `/season_stats` at box-score open
        // — it freezes there for the life of the response. For
        // today's slate the displayed total has to fold this game's
        // HRs in so a HR that just left the yard moves the parenthetical
        // from "(20)" → "(21)" immediately. For historical games
        // we trust the placeholder as already post-game on BDL's
        // side and don't double-count.
        //
        // The per-game count also serves as a multi-occurrence prefix
        // ("Alvarez 2 (21)") so a 2-HR night stays visible at a glance.
        let teams = [bs.teams.away, bs.teams.home]
        let isToday = isGameToday
        var out: [String] = []
        for team in teams {
            for id in team.batters {
                guard let p = team.players["ID\(id)"] else { continue }
                guard let hr = p.stats?.batting?.homeRuns, hr > 0 else { continue }
                let last = lastNameWithSuffix(p.person.fullName)
                let prefix = hr > 1 ? "\(last) \(hr)" : last
                // While the point-in-time fetch is still in flight,
                // render `(—)` rather than the stale placeholder
                // total. Eliminates the flash of e.g. "(20)" → "(21)"
                // on a 21st-HR-of-the-year night. Animated below.
                if let stats = batterStatsAtDateByBDL[p.person.id] {
                    let bump = !stats.includesToday ? hr : 0
                    out.append("\(prefix) (\(stats.homeRuns + bump))")
                } else {
                    out.append("\(prefix) (—)")
                }
            }
        }
        return out
    }

    /// True iff `game.startDate` falls on today's ET-local
    /// calendar day. Anchored to ET because MLB schedules its slate
    /// there — a 10pm PT first-pitch crosses midnight UTC but
    /// belongs to "tonight's slate" in ET.
    private var isGameToday: Bool {
        guard let start = game.startDate else { return false }
        let f = Self.etDateFormatter
        return f.string(from: start) == f.string(from: Date())
    }

    private static let etDateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.calendar = .init(identifier: .gregorian)
        f.timeZone = TimeZone(identifier: "America/New_York") ?? .current
        f.locale   = .init(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()

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

/// Unified card for the Upcoming + Live buckets. Renders the plain `GameCard`
/// (start time) or the live `LiveGameCard` (score + bases/count/LIVE) internally,
/// keyed off REACTIVE `liveList` membership — the SAME signal that buckets the
/// row into the Live section, so layout and subscription can't diverge.
///
/// Being ONE type across both buckets is what fixes the transition bugs: the row
/// keeps a stable `gamePk` identity across the section slide (so the animation is
/// unchanged), and this card — not a per-phase card that has to be swapped in —
/// OWNS the refcounted detail subscription and re-evaluates it via
/// `.onChange(of: shouldSubscribeLive)`. So a game flipping Upcoming→Live (or
/// Live→ended) re-subscribes / re-renders in place without depending on a SwiftUI
/// view-type swap re-running the appearance lifecycle. `FinalGameCard` stays
/// separate for the Completed bucket.
private struct GameRowCard: View {
    let game: Game
    let records: [Int: TeamRecord]
    let standings: [Int: TeamStandingInfo]
    /// Answered once by `ScoresViewModel.isOver` and threaded down, rather than
    /// re-derived here. Two independent copies of "is it over" is precisely how
    /// the stale card happened.
    let isOver: Bool
    /// Needed because a FINISHED game renders `FinalGameCard`, which owns its
    /// own tap-to-expand and pushes the box score from a button inside the
    /// expanded body — so it must NOT sit inside a NavigationLink. Threaded
    /// here rather than left at the call site, because unifying the row type
    /// is the entire point (see `body`).
    @Binding var path: NavigationPath
    @EnvironmentObject private var navigation: AppNavigation
    @EnvironmentObject private var liveStore: LiveGameStore
    /// Stable per-row token for the store's refcounted detail loop (moved here
    /// from `LiveGameCard` so it survives the Upcoming↔Live re-render — one loop
    /// shared across N cards + a pushed box score).
    @State private var subscriberID = LiveGameStore.SubscriberID()

    /// The backend's live definition (present in `liveList`) — reactive, so this
    /// recomputes on every store publish. Drives BOTH the layout choice and the
    /// subscription.
    private var isLive: Bool {
        LiveStatus.isLive(inLiveList:  liveStore.liveList[game.gamePk] != nil,
                          listLoaded:  liveStore.listLoaded,
                          phaseIsLive: game.phase == .live)
    }

    /// Subscribe only while live AND the Scores tab is visible / app active.
    private var shouldSubscribeLive: Bool {
        isLive && navigation.shouldPoll(on: .scores)
    }

    var body: some View {
        Group {
            if isLive {
                // Live block renders ONLY while in `liveList`, so a stale
                // `detail` snapshot can't show live content after a game ends.
                // Checked FIRST: membership in the live list outranks anything
                // else, so a game the store still calls live can never draw as
                // finished.
                NavigationLink(value: game) {
                    LiveGameCard(game: game, records: records, standings: standings)
                }
                .buttonStyle(.plain)
            } else if isOver {
                // NO NavigationLink, deliberately. `FinalGameCard` is
                // expand-on-tap and pushes the box score from a button inside
                // its expanded body; wrapping it in a link would make the whole
                // card a push target and swallow the expand gesture.
                FinalGameCard(game: game, records: records, path: $path)
            } else {
                NavigationLink(value: game) {
                    GameCard(game: game, records: records, isOver: isOver)
                }
                .buttonStyle(.plain)
            }
        }
        // Reactive subscribe: fires on the Upcoming→Live transition because this
        // outer card persists across the section move (stable identity, one type),
        // unlike the old GameCard→LiveGameCard type swap. Composes shouldPoll:
        // subscribe iff live AND tab-visible/active.
        //
        // THE SINGLE TYPE IS LOAD-BEARING, and not only for the subscription.
        // Every section builds THIS view, and the branching happens inside it.
        // A game keeps one identity (`Game.id` == gamePk) as it moves between
        // sections — deliberately, so the move animates — and SwiftUI resolves
        // a stable identity whose VIEW TYPE changed by keeping the old
        // rendering: a finished game went on drawing "BOT 9th · 3 outs" under
        // the Completed header while the data underneath was correct and the
        // new card was being constructed. Reproduced in a simulator harness and
        // fixed by exactly this unification. Do NOT hoist a branch back out to
        // the call site.
        //
        // The subscription gate needs no extra guard for the finished case:
        // `isLive` is `LiveStatus.isLive`, which returns false for a game
        // absent from a loaded list, so `shouldSubscribeLive` is already false
        // for anything rendering `FinalGameCard`.
        .task {
            if shouldSubscribeLive { liveStore.subscribeDetail(game.gamePk, owner: subscriberID) }
        }
        .onChange(of: shouldSubscribeLive) { _, on in
            if on {
                liveStore.subscribeDetail(game.gamePk, owner: subscriberID, immediate: true)
            } else {
                liveStore.unsubscribeDetail(game.gamePk, owner: subscriberID)
            }
        }
        .onDisappear { liveStore.unsubscribeDetail(game.gamePk, owner: subscriberID) }
    }
}

/// Card variant for games currently in progress — pure rendering. It reads this
/// game's live snapshot from the shared store; the refcounted detail
/// subscription is owned by the enclosing `GameRowCard` (so it re-subscribes on
/// the Upcoming→Live transition, which this card alone could not).
private struct LiveGameCard: View {
    let game: Game
    let records: [Int: TeamRecord]
    let standings: [Int: TeamStandingInfo]
    @EnvironmentObject private var liveStore: LiveGameStore

    /// This game's live snapshot from the shared store, adapted to the existing
    /// `LiveFeedResponse` shape the card renders — so the display is unchanged.
    private var liveFeed: LiveFeedResponse? {
        liveStore.detail[game.gamePk]?.toLiveFeedResponse()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            scoreboardRow
            if let live = liveFeed?.liveData {
                Divider()
                inGameDetail(live)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .frame(maxWidth: .infinity)
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 16))
        .shadow(color: .black.opacity(0.06), radius: 8, x: 0, y: 3)
        .contentShape(Rectangle())
    }

    // MARK: Top — team rows + inning + LIVE badge

    private var scoreboardRow: some View {
        // The score comes from the SAME store entry that decides this card
        // exists at all, not from the merged `Game`.
        //
        // The card used to read `game.teams.*.score`, which is only refreshed
        // when `applyLiveList` merges — while the panel below it read the store
        // directly. So the inning, outs and bases advanced every poll and the
        // score did not, and a card sat at 0-0 while its own box score showed
        // 1-0. Two halves of one card, two sources, one of them live.
        //
        // `liveList[gamePk]` is non-nil by construction here: `GameRowCard`
        // renders this view only when `liveStore.liveList[game.gamePk] != nil`.
        // The merged game stays as the fallback so nothing renders blank if the
        // entry is ever missing.
        //
        // NOT the detail snapshot, which is what `BoxScoreView` uses: its
        // `linescore.teams` came back null on a sampled live game, so copying
        // that expression here would trade a stale number for no number.
        let summary = liveStore.liveList[game.gamePk]
        return HStack(alignment: .center, spacing: 14) {
            VStack(spacing: 8) {
                teamRow(side: game.teams.away, bdlTeamId: game.bdlAwayTeamId,
                        liveScore: summary?.away.runs)
                teamRow(side: game.teams.home, bdlTeamId: game.bdlHomeTeamId,
                        liveScore: summary?.home.runs)
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

    private func teamRow(side: GameTeam, bdlTeamId: Int?, liveScore: Int?) -> some View {
        let standingText: String? = bdlTeamId
            .flatMap { standings[$0] }
            .map { $0.displayString }
        return HStack(spacing: 10) {
            // Colour, not a circle of letters: the abbreviation is the very
            // next thing in the row. 18pt matches the .subheadline line it leads.
            TeamColorSwatch(code: bdlTeamId.flatMap { bdlToLahmanTeamId[$0] })

            VStack(alignment: .leading, spacing: 0) {
                HStack(spacing: 6) {
                    Text(side.team.abbreviation ?? String(side.team.name.prefix(3)).uppercased())
                        .font(.subheadline.weight(.semibold))
                        .lineLimit(1)
                    if let w = bdlTeamId.flatMap({ records[$0] })?.wins,
                       let l = bdlTeamId.flatMap({ records[$0] })?.losses {
                        Text("(\(w)-\(l))")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .monospacedDigit()
                    }
                }
                if let standingText {
                    Text(standingText)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }

            Spacer()

            Text((liveScore ?? side.score).map(String.init) ?? "")
                .font(.title3.weight(.semibold))
                .monospacedDigit()
        }
    }

    /// "▲ 7th" / "▼ 9th" — top vs bottom of inning, derived from the
    /// linescore's `isTopInning`. Falls back to the inning ordinal
    /// alone when the half isn't reported (mid-inning / end-inning).
    private var inningArrow: some View {
        let lf = liveFeed
        let ls = game.linescore ?? lf?.liveData.linescore.map(toGameLinescore)
        let ordinal = ls?.currentInningOrdinal
            ?? lf?.liveData.linescore?.currentInningOrdinal
            ?? "?"
        let isTop = lf?.liveData.linescore?.isTopInning
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
