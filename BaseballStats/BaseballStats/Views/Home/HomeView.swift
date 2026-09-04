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
    @EnvironmentObject private var navigation: AppNavigation
    @EnvironmentObject private var liveStore: LiveGameStore
    /// Stable token for this tab's refcounted hold on the shared list loop, so
    /// Home↔Scores switching can't cancel a loop the other tab still needs.
    @State private var listSubscriberID = LiveGameStore.SubscriberID()
    @State private var navigationPath = NavigationPath()
    @State private var showingSettings = false
    @State private var showingAddPlayer = false
    @State private var showingSchedule = false
    @State private var showingLeadersSheet = false
    @State private var showingRosterSheet = false
    @State private var showingHistorySheet = false
    @State private var showingInjurySheet = false
    @State private var isEditingFavorites = false
    /// Tapped news article — drives the in-app Safari reader sheet.
    @State private var selectedArticle: NewsArticle?

    var body: some View { 
        NavigationStack(path: $navigationPath) {
            ZStack {
                backgroundGradient
                content
            }
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { toolbarContent }
            .stackDestinations(
                path: $navigationPath,
                owningTab: .home,
                navigation: navigation,
                liveStore: liveStore,
                teamStandings: vm.teamStandings,
                teamRecords: vm.teamRecords,
            )
            .navigationDestination(for: TeamNewsDestination.self) { dest in
                TeamNewsListView(
                    scope:      dest.scope,
                    lahmanCode: dest.lahmanCode,
                    teamName:   dest.teamName,
                    tint:       teamColor,
                )
            }
            .sheet(isPresented: $showingSettings) {
                SettingsView()
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
                        navigation:    navigation,
                        liveStore:     liveStore,
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
            .sheet(isPresented: $showingHistorySheet) { historySheet }
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
            // In-app Safari reader for a tapped news article. `item:` keys
            // the sheet to the exact tapped article so its url opens.
            .sheet(item: $selectedArticle) { article in
                if let url = URL(string: article.url) {
                    SafariView(url: url)
                        .ignoresSafeArea()
                        .appearanceOverride()
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
            await vm.loadNews(bdlTeamId: bdlId)
            await vm.loadLeagueNews()
        }
        .task(id: favoritesStore.playerIds) {
            await vm.loadFavoritePlayers(ids: favoritesStore.playerIds)
        }
        // Hold the SHARED LiveGameStore list loop while the Home tab is visible,
        // gated by shouldPoll(.home). Refcounted by `listSubscriberID` (like the
        // Scores tab), so a Home↔Scores switch can't cancel a loop the other tab
        // still holds regardless of .onChange ordering.
        .task {
            if navigation.shouldPoll(on: .home) { liveStore.subscribeList(owner: listSubscriberID) }
        }
        // Re-read the favourite's games while Home is visible. NOT behind
        // `hasLiveGame` — `applyLiveList` is, and that is exactly why the hero
        // card could freeze: once the view model stopped believing a game was
        // live, the only path that updated it stopped running. This one keeps
        // going regardless, which is the point.
        .periodicRefresh(
            every: RefreshCadence.slate,
            isActive: navigation.shouldPoll(on: .home),
        ) {
            if let bdlId = store.bdlTeamId {
                await vm.load(bdlTeamId: bdlId, quiet: true)
            }
        }
        // Fold each fresh /live/games snapshot from the store into the hero strip
        // (score / inning) — the merge the deleted refreshLive loop used to do,
        // now sourced from the store's shared list instead of a self-fetch.
        .onChange(of: liveStore.liveList) { _, list in
            guard let bdlId = store.bdlTeamId else { return }
            Task { await vm.applyLiveList(list, bdlTeamId: bdlId) }
        }
        // Pause the store list loop on background / switch away from Home;
        // resume with an immediate refresh on return.
        .onChange(of: navigation.shouldPoll(on: .home)) { _, canPoll in
            if canPoll {
                liveStore.subscribeList(owner: listSubscriberID)
            } else {
                liveStore.unsubscribeList(owner: listSubscriberID)
            }
        }
        .onDisappear { liveStore.unsubscribeList(owner: listSubscriberID) }
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

    /// Push the full news list for the given scope. Team scope uses the exact
    /// same Lahman code the carousel/news fetch already uses
    /// (`bdlToLahmanTeamId`) plus the team's display name for the nav title;
    /// league scope needs neither (per-article badges + a fixed title).
    private func pushNewsList(scope: NewsScope) {
        switch scope {
        case .team:
            guard let bdlId = store.bdlTeamId,
                  let lahmanCode = bdlToLahmanTeamId[bdlId] else { return }
            let teamName = MLBTeamCatalog.entry(forBDLId: bdlId)?.fullName
            navigationPath.append(
                TeamNewsDestination(scope: .team, lahmanCode: lahmanCode, teamName: teamName)
            )
        case .league:
            navigationPath.append(
                TeamNewsDestination(scope: .league, lahmanCode: nil, teamName: nil)
            )
        }
    }

    /// Subtle team-color wash that only paints the top ~40% of the
    /// screen and fades to clear. The `Color(.systemBackground)` base
    /// shows through below the fade — so the lower half of the tab
    /// reads as plain system surface (and the glass cards on top look
    /// like genuine glass against it).
    /// Extracted from the `.sheet` chain: adding a sixth argument to the
    /// destinations modifier pushed `body` past the compiler's type-checking
    /// budget ("unable to type-check this expression in reasonable time"). A
    /// SwiftUI view builder is one expression, so every modifier added to it
    /// costs inference time for the whole chain — pulling a branch into its own
    /// property is the standard remedy and changes nothing at runtime.
    @ViewBuilder
    private var historySheet: some View {
        if let bdlId = store.bdlTeamId,
           let entry = MLBTeamCatalog.entry(forBDLId: bdlId) {
            TeamHistorySheet(
                entry:            entry,
                vm:               vm,
                history:          vm.teamHistory,
                postseasonByYear: vm.postseasonByYear,
                isLoading:        vm.isLoadingHistory,
                navigation:       navigation,
                liveStore:        liveStore,
            )
            .presentationDetents([.large])
        }
    }

    private var backgroundGradient: some View {
        ZStack {
            // Grouped background so the solid white/dark cards float with
            // clear separation (matches the player-profile surface).
            Color(.systemGroupedBackground)
                .ignoresSafeArea()
            LinearGradient(
                colors: [
                    // ~50% stronger than the previous 0.25 / 0.08 so the
                    // team color reads clearly (was too pale). Kept global —
                    // every team uses the same values; content sits on opaque
                    // cards so dark-team primaries stay legible.
                    teamColor.opacity(0.375),
                    teamColor.opacity(0.12),
                    Color.clear,
                ],
                startPoint: .top,
                endPoint: .init(x: 0.5, y: 0.4),
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
                    showingSettings = true
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
                    liveGame:     vm.liveGame,
                    stripGames:   vm.recentAndUpcoming,
                    endedGames:   vm.endedLocally,
                    onSchedule:   { showingSchedule = true },
                    onTapStripGame: { game in navigationPath.append(game) },
                )

                // Team News — nice-to-have, so it only appears once
                // articles have loaded. A failed/empty fetch keeps
                // `vm.news` empty and the section never renders.
                if !vm.news.isEmpty {
                    TeamNewsSection(
                        teamArticles:   vm.news,
                        leagueArticles: vm.leagueNews,
                        tint:           tint,
                        onTapArticle:   { article in selectedArticle = article },
                        onSeeAll:       { scope in pushNewsList(scope: scope) },
                    )
                }

                TeamLeadersSection(
                    leaders:     vm.teamLeaders,
                    isLoading:   vm.isLoadingLeaders,
                    tint:        tint,
                    onSeeAll:    { showingLeadersSheet = true },
                    onTapPlayer: { player in
                        navigationPath.append(player)
                    },
                )

                // Roster / Injury / History — three nav entries grouped into
                // one card. The injury row is omitted when there are none.
                TeamToolsCard(
                    tint:        tint,
                    injuryCount: vm.injuredPlayers.count,
                    onRoster:    { showingRosterSheet = true },
                    onInjuries:  { showingInjurySheet = true },
                    onHistory:   { showingHistorySheet = true },
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

// MARK: - Shared card surface

/// Solid card surface with a team-color wash over a `systemBackground` base.
/// Big cards (default) carry the tint across the WHOLE card — stronger at top,
/// still gently tinted at the bottom (never clear) — so the team color reads
/// as the card's surface. Small tiles (`faint: true`) keep a top-tint that
/// fades to clear for a crisp white/dark look. Adaptive light/dark; opacities
/// stay low so inner `.primary`/`.secondary` text stays legible across all 30
/// team colors. In DARK mode the wash color is brightened (`adaptiveBrightened`)
/// so dark team colors actually show — text/borders/accents elsewhere keep the
/// TRUE team color. Pair with the caller's existing border + shadow.
private func teamWashBackground(
    tint: Color,
    cornerRadius: CGFloat,
    isDark: Bool,
    faint: Bool = false,
) -> some View {
    let shape = RoundedRectangle(cornerRadius: cornerRadius, style: .continuous)
    // Light mode over a white base already shows the raw color fine; dark mode
    // boosts every team color so even the dark ones register against the dark
    // surface (ceiling-capped so the bright ones don't blow out).
    let washTint = isDark ? tint.brightenedForDark() : tint
    let stops: [Gradient.Stop] = faint
        ? [
            .init(color: washTint.opacity(isDark ? 0.10 : 0.07), location: 0.0),
            .init(color: .clear,                                 location: 1.0),
          ]
        : isDark
            ? [
                .init(color: washTint.opacity(0.20), location: 0.0),   // stronger at top
                .init(color: washTint.opacity(0.15), location: 0.5),   // mid
                .init(color: washTint.opacity(0.11), location: 1.0),   // still tinted at bottom
              ]
            : [
                .init(color: washTint.opacity(0.16), location: 0.0),
                .init(color: washTint.opacity(0.11), location: 0.5),
                .init(color: washTint.opacity(0.08), location: 1.0),
              ]
    return ZStack {
        shape.fill(Color(.systemBackground))
        shape.fill(LinearGradient(stops: stops, startPoint: .top, endPoint: .bottom))
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
    /// Favorite team's currently-live game, if any. When non-nil, the
    /// card replaces the last/next game rows with a live-score panel
    /// (away logo + score · LIVE + inning · home logo + score).
    let liveGame: Game?
    let stripGames: [Game]
    /// Games this session watched leave the live list — see
    /// `HomeViewModel.endedLocally`. Passed as a value rather than a closure so
    /// SwiftUI can diff it and actually redraw when a game ends.
    let endedGames: Set<Int>
    let onSchedule: () -> Void
    let onTapStripGame: (Game) -> Void

    /// Drives the opacity pulse on the LIVE badge. Toggled true on
    /// first appearance so the repeat-forever animation starts; SwiftUI
    /// keeps animating between 1.0 and 0.4 from there.
    @State private var livePulse = false

    /// Point size of the record in `header`. 25pt is deliberately NOT a stock
    /// text style, so it must be scaled by hand — and it must be `@ScaledMetric`
    /// rather than a bare `.system(size: 25)`.
    ///
    /// DO NOT "simplify" this to `.font(.system(size: 25, weight: .bold))`.
    /// A literal system size does not respond to Dynamic Type at all: the
    /// record would look correct at the default size and then stay frozen at
    /// 25pt while every other line grew, which at AX5 leaves the card's anchor
    /// smaller than the division beneath it. This header's whole reason for
    /// being a single left-aligned column is that it survives AX1–AX5 without
    /// a fallback layout; a frozen record forfeits exactly that.
    ///
    /// `relativeTo: .title` is also load-bearing, not decorative. The live
    /// score columns use `.title.bold()` (see `liveTeamColumn`), and the
    /// record is deliberately sized BELOW them so that during a live game the
    /// score — not the season record — is the largest number in the card.
    /// Scaling relative to `.title` keeps that 25:28 ratio fixed at every
    /// Dynamic Type size, so the inversion holds all the way up rather than
    /// only at the default size.
    ///
    /// KNOWN AND ACCEPTED: the record's lead over the division line narrows as
    /// the text size grows, and at AX5 the two are the same size. Measured off
    /// the simulator, the record's ink height runs 18.3pt against the
    /// division's 11.3pt at the default size (1.62x) but 36.3pt against
    /// 35.7pt at AX5 (1.02x). This is a property of the type scale, not a bug:
    /// `.subheadline` grows roughly 3.2x from default to AX5 while `.title`
    /// grows roughly 2.0x, so the smaller style closes the gap on its own.
    ///
    /// Do not "fix" it by moving this to `relativeTo: .subheadline`. That
    /// would hold the record-to-division ratio constant, but the record would
    /// then outgrow the live score — which is the one thing this 25pt exists
    /// to prevent. The alternatives are equally lossy: enlarging the record
    /// re-creates the imbalance it was reduced to solve, and slowing the
    /// division returns it to the faint grey line the treatment promoted it
    /// out of. A reader at AX5 has three lines in front of them, in order,
    /// distinguished by weight and colour; the size difference is what gets
    /// spent, deliberately.
    @ScaledMetric(relativeTo: .title) private var recordPointSize: CGFloat = 25

    /// Home tab tree, so the root-injected coordinators are reliably present.
    @EnvironmentObject private var navigation: AppNavigation
    @EnvironmentObject private var liveStore: LiveGameStore
    /// Stable per-card identity for the store's refcounted detail subscription
    /// (Phase 2, step 4) — makes subscribe/unsubscribe idempotent.
    @State private var subscriberID = LiveGameStore.SubscriberID()

    /// The favorite's live snapshot from the shared store, adapted to the
    /// existing `LiveFeedResponse` shape the situation panel renders — so the
    /// runner / out / matchup display is unchanged.
    private var liveFeed: LiveFeedResponse? {
        guard let pk = liveGame?.gamePk else { return nil }
        return liveStore.detail[pk]?.toLiveFeedResponse()
    }

    @Environment(\.colorScheme) private var colorScheme

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
            if let live = liveGame {
                liveScoreRow(game: live)
            } else {
                lastGameRow
                nextGameRow
            }
            if !stripGames.isEmpty {
                Divider().opacity(0.4)
                gameStrip
            }
            scheduleLink
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        // Solid card + faint team wash (matches the player profile / heat
        // cards) so it reads as a crisp white surface over the grouped
        // background rather than translucent grey.
        .background(
            teamWashBackground(
                tint: teamColor, cornerRadius: 18, isDark: colorScheme == .dark,
            )
        )
        .overlay(
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .strokeBorder(teamColor.opacity(0.25), lineWidth: 1)
        )
        .shadow(color: .black.opacity(0.06), radius: 8, x: 0, y: 3)
        .padding(.horizontal, 16)
        // Subscribe to the favorite's shared detail loop while there's a live
        // game AND the Home tab is visible/active. Refcounted by subscriberID.
        .task {
            if let pk = liveGame?.gamePk, navigation.shouldPoll(on: .home) {
                liveStore.subscribeDetail(pk, owner: subscriberID)
            }
        }
        // Favorite's live game changed (or cleared): release the old id and
        // subscribe the new one. onChange hands us both, so the old game's
        // detail loop refcount drops correctly.
        .onChange(of: liveGame?.gamePk) { oldPk, newPk in
            if let oldPk { liveStore.unsubscribeDetail(oldPk, owner: subscriberID) }
            if let newPk, navigation.shouldPoll(on: .home) {
                liveStore.subscribeDetail(newPk, owner: subscriberID, immediate: true)
            }
        }
        // Pause on background / switch away from Home; resume with an immediate
        // refresh on return.
        .onChange(of: navigation.shouldPoll(on: .home)) { _, canPoll in
            guard let pk = liveGame?.gamePk else { return }
            if canPoll {
                liveStore.subscribeDetail(pk, owner: subscriberID, immediate: true)
            } else {
                liveStore.unsubscribeDetail(pk, owner: subscriberID)
            }
        }
        .onDisappear {
            if let pk = liveGame?.gamePk {
                liveStore.unsubscribeDetail(pk, owner: subscriberID)
            }
        }
    }

    /// Team header. Carried an 88pt club mark on the left until the imagery
    /// removal; the toolbar above still shows a 22pt mark beside the same
    /// name, so a small mark in the chrome with none in the content is the
    /// hierarchy we keep.
    ///
    /// The mark's removal left a left-aligned column with 48–73% of its width
    /// empty — the record used 105pt of 329, the division 88pt — which read as
    /// residue rather than composition. Rather than spend that width on a
    /// second column (which cannot survive Dynamic Type: at AX1 a two-column
    /// header overflows for every club), the column is kept and the type
    /// re-set, so the emptiness is deliberate and the block still grows
    /// downward at accessibility sizes without a fallback layout.
    ///
    /// Three moves: the club name drops to an uppercase eyebrow — the toolbar
    /// states it twice already, so at .title3 here it was the third statement
    /// of the least load-bearing fact; the division comes out of .secondary to
    /// 15pt semibold .primary, since a small grey string under a 34pt numeral
    /// was the element that most read as unfinished; and the spacing closes to
    /// 0/2 so the three lines bind into one unit instead of floating apart.
    ///
    /// The record then came DOWN, from .largeTitle (34pt) to 25pt. The 34pt was
    /// set when the division was faint 15pt secondary and the record had to
    /// carry the block alone; once the division became 15pt semibold primary
    /// the record was oversized for a composition that had gained a second real
    /// element. At 25pt it still leads (1.7x the division) without dominating,
    /// and it sits below the live score so a game in progress reads first.
    /// Net: the card is ~27pt shorter than it was.
    private var header: some View {
        VStack(alignment: .leading, spacing: 0) {
            // `minimumScaleFactor` is LOAD-BEARING here, not a guard. The
            // longest club name fits comfortably through AX3, but at AX5
            // "ARIZONA DIAMONDBACKS" exceeds the 329pt content width even
            // after scaling and truncates to "ARIZONA DIAMON…". That is
            // accepted rather than fixed: the toolbar directly above still
            // renders the full name at AX5, so the eyebrow is the third
            // statement of a fact already twice on screen, and shrinking it
            // further would cost legibility for the users who chose AX5.
            // Do not drop this modifier — without it the name truncates
            // earlier and harder.
            Text(entry.fullName.uppercased())
                .font(.caption2.weight(.bold))
                .tracking(0.6)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .minimumScaleFactor(0.85)
            Text(recordText)
                .font(.system(size: recordPointSize, weight: .bold))
                .foregroundStyle(.primary)
                .monospacedDigit()
            Text(divisionText)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.primary)
                .padding(.top, 2)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var hasL10OrStreak: Bool {
        streakCode != nil || (lastTenW != nil && lastTenL != nil)
    }

    /// Form pills, side by side while they fit and stacked when they don't.
    ///
    /// Side by side they overflow at the largest accessibility sizes, and a
    /// `Text` given less width than it wants WRAPS. "STREAK" is one word wider
    /// than the space it was being offered, and a single word with no break
    /// opportunity wraps mid-word — hence "STRE / AK" and "4- / 6", with the
    /// capsule growing around the two lines into a blob. Nothing here is
    /// clipped or truncated; it is ordinary wrapping in a space too narrow for
    /// the word.
    ///
    /// `ViewThatFits` takes the row when the row fits and the column when it
    /// does not, so nothing changes below AX5 — verified in the simulator at
    /// real Dynamic Type, where AX3 is byte-identical to today. Stacking is
    /// preferred over the alternatives because it is the only one that keeps
    /// BOTH labels and BOTH values: dropping the labels to fit one line loses
    /// what the numbers mean, and dropping the capsule fixes nothing at all —
    /// the constraint is the row's width, not the capsule's shape, so plain
    /// text wraps mid-word exactly the same way.
    private var streakLine: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: 12) {
                streakPills
                Spacer(minLength: 0)
            }
            VStack(alignment: .leading, spacing: 8) {
                streakPills
            }
        }
    }

    /// The pills themselves, shared by both arms of `ViewThatFits` so the two
    /// layouts cannot drift apart.
    @ViewBuilder
    private var streakPills: some View {
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

    /// Live-score panel — away column / center status / home column.
    /// Favorite-team side reads in `.primary`; the opponent's score
    /// drops to `.secondary` so the user's eyes land on their team
    /// first. The center column carries a pulsing LIVE badge plus
    /// the inning label (BDL's per-game payload only ships the
    /// inning *number* — no top/bottom half — so we say "Inning Nth"
    /// rather than mislabeling it "Top N").
    @ViewBuilder
    private func liveScoreRow(game: Game) -> some View {
        let away = game.teams.away
        let home = game.teams.home
        let isFavoriteHome = (game.bdlHomeTeamId == entry.bdlTeamId)
        let isFavoriteAway = (game.bdlAwayTeamId == entry.bdlTeamId)

        VStack(spacing: 8) {
            HStack(alignment: .center, spacing: 0) {
                liveTeamColumn(
                    team: away.team, score: away.score, isFavorite: isFavoriteAway,
                )
                Spacer(minLength: 8)
                liveStatusColumn(game: game)
                Spacer(minLength: 8)
                liveTeamColumn(
                    team: home.team, score: home.score, isFavorite: isFavoriteHome,
                )
            }
            liveSituationPanel
        }
        .padding(.vertical, 8)
    }

    /// Bases + outs + current matchup, driven by `liveFeed` (the shared
    /// `LiveGameStore`'s detail snapshot for the favorite's game). Rendered
    /// unconditionally beneath the score row so the layout is stable
    /// — runners default to empty, outs to zero, and the
    /// batter-vs-pitcher line is hidden when names aren't ready
    /// (early-game cold start, or BDL hasn't shipped a "Start
    /// Batter/Pitcher" event yet).
    private var liveSituationPanel: some View {
        let linescore = liveFeed?.liveData.linescore
        let outs = linescore?.outs ?? 0
        let first  = linescore?.offense?.first  != nil
        let second = linescore?.offense?.second != nil
        let third  = linescore?.offense?.third  != nil
        let batterFull  = linescore?.offense?.batter?.fullName  ?? ""
        let pitcherFull = linescore?.defense?.pitcher?.fullName ?? ""

        return VStack(spacing: 8) {
            HStack(alignment: .center, spacing: 16) {
                BaseRunnerView(
                    first: first, second: second, third: third, size: 28,
                )
                Spacer()
                liveOutsDots(outs: outs)
            }
            if Self.isReadableName(batterFull),
               Self.isReadableName(pitcherFull) {
                HStack(spacing: 6) {
                    Text(Self.lastNameOnly(batterFull))
                        .font(.caption.weight(.semibold))
                        .lineLimit(1)
                    Text("vs")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                    Text(Self.lastNameOnly(pitcherFull))
                        .font(.caption.weight(.semibold))
                        .lineLimit(1)
                }
            }
        }
    }

    /// 3-dot out indicator. Yellow when out is recorded; muted gray
    /// otherwise. 10pt circles match the compact card density.
    private func liveOutsDots(outs: Int) -> some View {
        HStack(spacing: 6) {
            ForEach(0..<3, id: \.self) { i in
                Circle()
                    .fill(i < outs ? Color.yellow : Color(.systemGray4))
                    .frame(width: 10, height: 10)
            }
        }
    }

    /// Trailing space-separated token — surnames come back from BDL's
    /// "X pitches to Y" parser already in last-name form, so this is
    /// usually a passthrough; for full names it strips the first
    /// name to keep the hero-card matchup line compact.
    private static func lastNameOnly(_ full: String) -> String {
        full.components(separatedBy: " ").last ?? full
    }

    /// Guard against the synthesizer's "—" / empty fallback when BDL
    /// hasn't shipped a matchup-intro play yet. Keeps the
    /// batter-vs-pitcher row from rendering "— vs —".
    private static func isReadableName(_ s: String) -> Bool {
        !s.isEmpty && s != "—"
    }

    private func liveTeamColumn(
        team: TeamInfo, score: Int?, isFavorite: Bool,
    ) -> some View {
        VStack(spacing: 4) {
            // Same move as the box-score column: the swatch leads the
            // abbreviation rather than standing alone above it. 13pt here —
            // this label is .caption, and an 18pt bar beside 12pt letters
            // reads as a rule rather than a mark.
            HStack(spacing: 4) {
                TeamColorSwatch(team: team, height: 13)
                Text(team.abbreviation ?? String(team.name.prefix(3)).uppercased())
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
            }
            Text("\(score ?? 0)")
                .font(.title.bold())
                .monospacedDigit()
                .foregroundStyle(isFavorite ? .primary : .secondary)
        }
    }

    private func liveStatusColumn(game: Game) -> some View {
        VStack(spacing: 6) {
            Text("LIVE")
                .font(.caption2.weight(.bold))
                .tracking(0.5)
                .foregroundStyle(.white)
                .padding(.horizontal, 8)
                .padding(.vertical, 3)
                .background(Color.red, in: Capsule())
                .opacity(livePulse ? 1.0 : 0.4)
                .animation(
                    .easeInOut(duration: 1.8).repeatForever(autoreverses: true),
                    value: livePulse,
                )
                .onAppear { livePulse = true }
            Text(liveInningText(game: game))
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    /// Inning label for the center column. Prefers
    /// `linescore.currentInningOrdinal` when MLB Stats API supplied
    /// it; otherwise builds an ordinal from `currentInning`. Falls
    /// back to a bare "Live" string only when no inning is in the
    /// payload at all (early-game / mid-transition snapshots).
    private func liveInningText(game: Game) -> String {
        if let ord = game.linescore?.currentInningOrdinal, !ord.isEmpty {
            return "Inning \(ord)"
        }
        if let inning = game.linescore?.currentInning {
            return "Inning \(Self.inningOrdinal(inning))"
        }
        return "Live"
    }

    private static func inningOrdinal(_ n: Int) -> String {
        let last2 = n % 100
        let suffix: String
        if (11...13).contains(last2) {
            suffix = "th"
        } else {
            switch n % 10 {
            case 1: suffix = "st"
            case 2: suffix = "nd"
            case 3: suffix = "rd"
            default: suffix = "th"
            }
        }
        return "\(n)\(suffix)"
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
                    // A different failure from the pills above, needing a
                    // different fix. This trailing date is the flexible end of
                    // the row, so at AX5 it is squeezed until "Yesterday"
                    // hyphenates to "Yester- / day". Given its natural width it
                    // stays whole and the matchup line — which has real word
                    // breaks — wraps instead, which is legible.
                    .fixedSize(horizontal: true, vertical: false)
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
                // `phase == .live` alone kept the red live treatment — and the
                // frozen inning beside it — on a game that had already ended.
                let nextIsLive = next.phase == .live
                    && !LiveStatus.isOver(phaseIsFinal: false,
                                          gamePk: next.gamePk,
                                          endedLocally: endedGames)
                Image(systemName: nextIsLive ? "dot.radiowaves.left.and.right" : "calendar")
                    .font(.body.weight(.semibold))
                    .foregroundStyle(nextIsLive ? .red : teamColor)
                    .frame(width: 26, height: 26)
                    .background(
                        Circle().fill(
                            (nextIsLive ? Color.red : teamColor).opacity(0.12)
                        )
                    )
                Text(HomeGameUtils.nextGameLine(game: next, favoriteBDLId: entry.bdlTeamId,
                                                isOver: !nextIsLive && next.phase == .live))
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
                                isOver:        LiveStatus.isOver(
                                    phaseIsFinal: game.phase == .final,
                                    gamePk:       game.gamePk,
                                    endedLocally: endedGames),
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
    /// From `LiveStatus.isOver`. A finished game must stop drawing live content
    /// and lose the red ring even while the slate still calls it live.
    var isOver: Bool = false

    @Environment(\.colorScheme) private var colorScheme

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
            // No team mark here: the abbreviation sits immediately beside it,
            // so the badge was saying the same thing twice in the narrowest
            // card in the app (110pt). The letters alone read "vs NYY".
            HStack(spacing: 4) {
                Text(isHomeGame ? "vs" : "@")
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.secondary)
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
        // Solid tile + a VERY faint team wash (half the big-card
        // intensity) so it reads as crisp white with just a hint of color.
        .background(
            teamWashBackground(
                tint: tint, cornerRadius: 10, isDark: colorScheme == .dark, faint: true,
            )
        )
        .overlay {
            // Live games keep a red accent ring; everything else
            // relies on the material itself for separation.
            if game.phase == .live && !isOver {
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .strokeBorder(Color.red.opacity(0.55), lineWidth: 1.2)
            }
        }
    }

    @ViewBuilder
    private var bodyByPhase: some View {
        // `isOver` first: a game that left the live list draws its final score,
        // not the inning it stopped in, however long the slate takes to agree.
        if isOver {
            finalContent
        } else {
            switch game.phase {
            case .final:    finalContent
            case .live:     liveContent
            case .preview, .other, .postponed: upcomingContent
            }
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

    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        HStack(spacing: 10) {
            Capsule()
                // Brighten the team-color accent in dark mode so dark teams
                // still read against the dark surface.
                .fill(colorScheme == .dark ? tint.brightenedForDarkText() : tint)
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

    /// `isOver` is the caller's answer from `LiveStatus.isOver`. Without it this
    /// printed `inningLine` — the inning off a linescore frozen at the last out
    /// — for as long as the provider went on calling the game live.
    static func nextGameLine(game: Game, favoriteBDLId: Int, isOver: Bool = false) -> String {
        let venue  = (game.bdlHomeTeamId == favoriteBDLId) ? "vs" : "@"
        let opp    = opponentAbbr(game: game, favoriteBDLId: favoriteBDLId)
        if isOver { return "Recent · \(venue) \(opp)" }
        switch game.phase {
        case .live:
            return "\(venue) \(opp) · \(inningLine(game: game))"
        case .postponed:
            return "PPD \(venue) \(opp)"
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

    @Environment(\.colorScheme) private var colorScheme
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
                    .foregroundStyle(colorScheme == .dark ? tint.brightenedForDarkText() : tint)
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
                        VStack(alignment: .leading, spacing: 8) {
                            ForEach(Array(groups.enumerated()), id: \.element.id) { idx, group in
                                statGroupSection(group)
                                if idx < groups.count - 1 {
                                    Divider().opacity(0.3)
                                }
                            }
                        }
                    }
                }
            }
            .padding(16)
            // Solid card + faint team wash — exact same recipe as the
            // hero card so the two large cards match in both light and dark.
            .background(
                teamWashBackground(
                    tint: tint, cornerRadius: 18, isDark: colorScheme == .dark,
                )
            )
            .overlay(
                RoundedRectangle(cornerRadius: 18, style: .continuous)
                    .strokeBorder(tint.opacity(0.25), lineWidth: 1)
            )
            .shadow(color: .black.opacity(0.06), radius: 8, x: 0, y: 3)
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
                .font(.caption2.weight(.semibold))
                .tracking(0.8)
                .foregroundStyle(.tertiary)
            VStack(spacing: 2) {
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

// MARK: - Team Tools Card (Roster / Injury / History nav rows)

/// Groups the three team-detail entry points (Roster, Injury Report, History)
/// into one card of tappable nav rows — replacing the old bare section headers.
/// Each row opens its existing sheet. The Injury Report row is omitted entirely
/// when there are no injuries (so no dangling divider), while the card itself
/// always shows (Roster + History are always present).
private struct TeamToolsCard: View {
    let tint: Color
    /// 0 → the Injury Report row is omitted.
    let injuryCount: Int
    let onRoster: () -> Void
    let onInjuries: () -> Void
    let onHistory: () -> Void

    @Environment(\.colorScheme) private var colorScheme

    /// Team accent, brightened in dark mode — same source as the old header
    /// capsule bar.
    private var iconTint: Color {
        colorScheme == .dark ? tint.brightenedForDarkText() : tint
    }

    /// Divider starts after the icon (leading inset) like a grouped list.
    private static let dividerInset: CGFloat = 58

    private var injuryCaption: String {
        injuryCount == 1 ? "1 player on the IL" : "\(injuryCount) players on the IL"
    }

    var body: some View {
        VStack(spacing: 0) {
            row(icon: "person.3.fill", title: "Roster",
                caption: "Active roster", action: onRoster)

            if injuryCount > 0 {
                divider
                row(icon: "cross.case.fill", title: "Injury Report",
                    caption: injuryCaption, badge: injuryCount, action: onInjuries)
            }

            divider
            row(icon: "clock.arrow.circlepath", title: "History",
                caption: "Season-by-season record", action: onHistory)
        }
        // Team-tint wash — identical treatment to the Team Leaders card so the
        // two cards stay in sync (same helper, border, shadow, corner radius).
        .background(
            teamWashBackground(tint: tint, cornerRadius: 18, isDark: colorScheme == .dark)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .strokeBorder(tint.opacity(0.25), lineWidth: 1)
        )
        .shadow(color: .black.opacity(0.06), radius: 8, x: 0, y: 3)
        .padding(.horizontal, 16)
    }

    private var divider: some View {
        Divider().padding(.leading, Self.dividerInset)
    }

    private func row(
        icon: String,
        title: String,
        caption: String,
        badge: Int? = nil,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            HStack(spacing: 12) {
                Image(systemName: icon)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(iconTint)
                    .frame(width: 30, height: 30)
                    .background(
                        RoundedRectangle(cornerRadius: 8, style: .continuous)
                            .fill(iconTint.opacity(0.15))
                    )

                VStack(alignment: .leading, spacing: 1) {
                    Text(title)
                        .font(.headline)
                        .foregroundStyle(.primary)
                    Text(caption)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                Spacer(minLength: 8)

                if let badge {
                    Text("\(badge)")
                        .font(.caption.weight(.bold))
                        .monospacedDigit()
                        .foregroundStyle(.white)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 2)
                        .background(Capsule().fill(iconTint))
                }

                Image(systemName: "chevron.right")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.tertiary)
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 11)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
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
                withAnimation(.spring(response: 0.3)) {
                    isEditing.toggle()
                }
            }
            .font(.subheadline.weight(.semibold))
        }
    }

    @ViewBuilder
    private func favoriteCard(_ fav: FavoritePlayerDisplay) -> some View {
        if isEditing {
            FavoritePlayerTile(fav: fav, showRemoveBadge: true, tint: tint, onRemove: {
                onRemove(fav.player.player_id)
            })
        } else {
            Button { onTapPlayer(fav.player) } label: {
                FavoritePlayerTile(fav: fav, showRemoveBadge: false, tint: tint, onRemove: {})
            }
            .buttonStyle(.plain)
        }
    }
}

private struct FavoritePlayerTile: View {
    let fav: FavoritePlayerDisplay
    let showRemoveBadge: Bool
    let tint: Color
    let onRemove: () -> Void

    @Environment(\.colorScheme) private var colorScheme

    // RECLAIMED — but this tile is the one place where deleting the frame is
    // NOT enough on its own. Unlike the list rows and the shelf card, the tile
    // is pinned to a fixed 132×156, so dropping the 50pt disc would leave the
    // space behind as a hole rather than closing it. The frame comes down to
    // match (see below), which keeps the carousel tight instead of airy.
    var body: some View {
        ZStack(alignment: .topLeading) {
            VStack(spacing: 6) {
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
            .frame(width: 132, height: 104)
            // Solid tile + a VERY faint team wash, matching the game-strip
            // tiles — crisp white/dark with just a hint of team color.
            .background(
                teamWashBackground(
                    tint: tint, cornerRadius: 14, isDark: colorScheme == .dark, faint: true,
                )
            )
            .shadow(color: .black.opacity(0.06), radius: 8, y: 3)

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
        // Mirrors the live tile: the disc went with the portrait, and the
        // height tracks it, so the skeleton-to-real swap stays invisible.
        VStack(spacing: 6) {
            RoundedRectangle(cornerRadius: 4).fill(Color(.systemGray5)).frame(width: 80, height: 12)
            RoundedRectangle(cornerRadius: 4).fill(Color(.systemGray5)).frame(width: 56, height: 10)
            RoundedRectangle(cornerRadius: 4).fill(Color(.systemGray5)).frame(width: 100, height: 10)
        }
        .padding(.vertical, 10)
        .padding(.horizontal, 8)
        .frame(width: 132, height: 104)
        // Solid tile — matches the live tile so the skeleton shape lines
        // up exactly while data is loading.
        .background(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .fill(Color(.systemBackground))
        )
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
            .frame(width: 132, height: 104)
            // Solid tile — matches the player tiles. The dashed accent
            // stroke on top is the unique signal.
            .background(
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .fill(Color(.systemBackground))
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

// MARK: - Team News Section

/// Horizontal carousel of the favorite team's latest news, placed between the
/// team hero card and the Team Leaders section. The caller only renders this
/// when `articles` is non-empty, so loading / empty / error all resolve to
/// "section absent".
private struct TeamNewsSection: View {
    let teamArticles: [NewsArticle]
    let leagueArticles: [NewsArticle]
    let tint: Color
    let onTapArticle: (NewsArticle) -> Void
    let onSeeAll: (NewsScope) -> Void

    /// Persisted across launches so the carousel reopens on the last-used feed.
    @AppStorage("newsScope") private var scope: NewsScope = .team
    @Environment(\.colorScheme) private var colorScheme

    /// Articles for the active scope. Falls back to team if league was chosen
    /// but isn't loaded (e.g. the league fetch failed), so the carousel is
    /// never empty while team news exists.
    private var articles: [NewsArticle] {
        scope == .league && !leagueArticles.isEmpty ? leagueArticles : teamArticles
    }

    /// Team-mode is visually identical to today; badges only in league mode.
    private var showsTeamBadge: Bool { scope == .league }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            // Same header chrome as Team Leaders (capsule accent + title).
            // "See all" pushes the full news list for the current scope; styled
            // like the active "See All Stats" link (team tint, dark-brightened).
            HomeSectionHeader(title: "Team News", tint: tint) {
                Button { onSeeAll(scope) } label: {
                    HStack(spacing: 3) {
                        Text("See all")
                        Image(systemName: "chevron.right")
                            .font(.caption2.weight(.bold))
                    }
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(colorScheme == .dark ? tint.brightenedForDarkText() : tint)
                }
            }

            // Team / League toggle — only once the league feed is available.
            // Constrained width so the two-option control doesn't span the
            // full screen; leading-aligned under the header.
            if !leagueArticles.isEmpty {
                Picker("News scope", selection: $scope) {
                    Text("Team").tag(NewsScope.team)
                    Text("League").tag(NewsScope.league)
                }
                .pickerStyle(.segmented)
                .frame(width: 200)
                .padding(.horizontal, 16)
            }

            ScrollView(.horizontal, showsIndicators: false) {
                LazyHStack(spacing: 12) {
                    ForEach(articles) { article in
                        Button { onTapArticle(article) } label: {
                            NewsCard(article: article, tint: tint, showsTeamBadge: showsTeamBadge)
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, 16)
            }
        }
    }
}

/// One news tile — image (16:9) with a source badge, headline (2 lines), and
/// a relative timestamp. Full-bleed image clipped to the card's rounded
/// corners; solid card surface + the app's standard shadow.
private struct NewsCard: View {
    let article: NewsArticle
    let tint: Color
    /// League mode adds a team badge (bottom-right) and derives the image
    /// fallback tint from the article's own team instead of the favorite.
    let showsTeamBadge: Bool

    private static let cardWidth: CGFloat = 250
    private static let imageHeight: CGFloat = cardWidth * 9 / 16   // 16:9

    private static let relativeFormatter: RelativeDateTimeFormatter = {
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .abbreviated   // "2h ago", "3d ago"
        return f
    }()

    /// Per-article team tint in league mode; favorite-team tint otherwise.
    private var effectiveTint: Color {
        showsTeamBadge ? (TeamColors.color(for: article.teamCode) ?? .accentColor) : tint
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            image
                .frame(width: Self.cardWidth, height: Self.imageHeight)
                .clipped()
                .overlay(alignment: .bottomLeading) { sourceBadge }
                .overlay(alignment: .bottomTrailing) {
                    if showsTeamBadge {
                        NewsTeamBadge(teamCode: article.teamCode)
                            .padding(8)
                    }
                }

            VStack(alignment: .leading, spacing: 6) {
                Text(article.title)
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(.primary)
                    .lineLimit(2, reservesSpace: true)   // uniform card height
                    .multilineTextAlignment(.leading)
                Text(Self.relativeFormatter.localizedString(
                    for: article.publishedAt, relativeTo: Date()))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .padding(12)
            .frame(width: Self.cardWidth, alignment: .leading)
        }
        .frame(width: Self.cardWidth)
        .background(Color(.systemBackground))
        .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
        .shadow(color: .black.opacity(0.06), radius: 8, x: 0, y: 3)
        .contentShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
    }

    // MARK: Image + fallbacks

    @ViewBuilder
    private var image: some View {
        if let urlStr = article.imageUrl, let url = URL(string: urlStr) {
            AsyncImage(url: url) { phase in
                switch phase {
                case .success(let img):
                    img.resizable().scaledToFill()
                case .empty:
                    placeholder                 // loading: neutral gray
                case .failure:
                    fallback                    // broken/unreachable: team block
                @unknown default:
                    fallback
                }
            }
        } else {
            fallback                            // no image url
        }
    }

    /// Neutral loading fill.
    private var placeholder: some View {
        Rectangle().fill(Color(.secondarySystemFill))
    }

    /// Graceful no-image block — a faint team-tint wash with a newspaper
    /// glyph, never a broken-image icon.
    private var fallback: some View {
        ZStack {
            Rectangle().fill(effectiveTint.opacity(0.18))
            Image(systemName: "newspaper.fill")
                .font(.title)
                .foregroundStyle(effectiveTint.opacity(0.55))
        }
    }

    /// "MLB.com" badge — semi-opaque light pill + dark text, legible over
    /// any photo in both light and dark mode.
    private var sourceBadge: some View {
        Text(article.sourceName)
            .font(.caption2.weight(.semibold))
            .foregroundStyle(.black)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(Color.white.opacity(0.82), in: Capsule())
            .padding(8)
    }
}

#Preview {
    HomeView()
}
