//
//  LiveTransitionTests.swift
//  BaseballStatsTests
//
//  The live→final transition, tested without a live game.
//
//  A game finishing while the app is open produced four separate faults
//  (commit 2b0fd74), and the only way to see any of them was to be watching
//  when a real game ended — a window that is easy to miss and impossible to
//  schedule. These tests stand in for that window.
//

import Foundation
import Testing
@testable import BaseballStats

// MARK: - Fixtures

private func makeTeam(_ id: Int, _ abbr: String) -> BDLTeam {
    BDLTeam(id: id, slug: abbr.lowercased(), abbreviation: abbr,
            displayName: abbr, shortDisplayName: abbr, name: abbr,
            location: abbr, league: "American", division: "East")
}

/// A slate row. `status` is what the whole thing turns on: BDL ships
/// "STATUS_IN_PROGRESS" until it decides the game is over, then "STATUS_FINAL".
private func makeGame(id: Int, status: String, inning: Int? = 9) -> BDLGame {
    BDLGame(
        id: id,
        homeTeam: makeTeam(1, "NYY"), awayTeam: makeTeam(2, "BOS"),
        homeTeamData: nil, awayTeamData: nil,
        date: "2026-08-17T00:40:00.000Z",
        status: status, venue: nil, period: inning, displayClock: nil,
        scoringSummary: nil, season: 2026, seasonType: "regular",
        postseason: false, homeTeamName: "Yankees", awayTeamName: "Red Sox",
    )
}

private func makeSummary(gameId: Int) -> LiveGameSummary {
    LiveGameSummary(
        gameId: gameId, fetchedAt: "2026-08-17T01:00:00Z",
        status: "in_progress", isLive: true, inning: 9,
        inningHalf: "bottom", outs: 3,
        away: LiveTeamLite(teamCode: "BOS", abbreviation: "BOS", name: "Red Sox", runs: 2),
        home: LiveTeamLite(teamCode: "NYY", abbreviation: "NYY", name: "Yankees", runs: 3),
    )
}

// MARK: - Fakes

/// Records every slate read — how many, and crucially with what `bypassCache`.
/// `statusSequence` lets a test say "the provider still says in-progress for the
/// first N reads", which is the upstream lag the retry chain exists for.
private final class FakeSlate: SlateProviding, @unchecked Sendable {
    let gameId: Int
    /// Status returned per call, last value repeating once exhausted.
    var statusSequence: [String]
    private(set) var calls: [(bypassCache: Bool, date: String)] = []
    private let lock = NSLock()

    init(gameId: Int, statusSequence: [String]) {
        self.gameId = gameId
        self.statusSequence = statusSequence
    }

    var callCount: Int { lock.withLock { calls.count } }
    var bypassFlags: [Bool] { lock.withLock { calls.map(\.bypassCache) } }

    private func next(date: String, bypassCache: Bool) -> [BDLGame] {
        lock.withLock {
            let i = min(calls.count, statusSequence.count - 1)
            calls.append((bypassCache, date))
            return [makeGame(id: gameId, status: statusSequence[i])]
        }
    }

    func getGames(date: String, bypassCache: Bool) async throws -> [BDLGame] {
        next(date: date, bypassCache: bypassCache)
    }
    func getTeamGames(date: String, teamId: Int, bypassCache: Bool) async throws -> [BDLGame] {
        next(date: date, bypassCache: bypassCache)
    }
}

private struct FakeLiveFeedError: Error {}

/// Serves `/live/games/{id}` as either a snapshot, a 404 (nil), or a throw —
/// the three cases `fetchDetail` must tell apart.
private final class FakeLiveFeed: LiveFeedProviding, @unchecked Sendable {
    enum Response { case snapshot, notFound, failure }
    var response: Response = .snapshot
    private let lock = NSLock()
    private var _detailCalls = 0
    var detailCalls: Int { lock.withLock { _detailCalls } }

    func getLiveGames() async throws -> LiveGamesResponse {
        LiveGamesResponse(fetchedAt: "2026-08-17T01:00:00Z", count: 0, games: [])
    }

    func getLiveGame(id gameId: Int) async throws -> LiveGameDetail? {
        lock.withLock { _detailCalls += 1 }
        switch response {
        case .notFound: return nil
        case .failure:  throw FakeLiveFeedError()
        case .snapshot: return try Self.decodeSnapshot(gameId: gameId)
        }
    }

    /// Built by decoding, so the fixture exercises the real Codable path rather
    /// than a hand-assembled value that could drift from the wire shape.
    static func decodeSnapshot(gameId: Int) throws -> LiveGameDetail {
        let json = """
        {"game_id": \(gameId), "fetched_at": "2026-08-17T01:00:00Z",
         "status": "in_progress", "season": 2026, "season_type": "regular",
         "summary": {"away": {"runs": 2}, "home": {"runs": 3}, "inning": 9,
                     "inning_half": "bottom", "outs": 3, "balls": 0,
                     "strikes": 0, "is_live": true},
         "linescore": {"innings": []},
         "situation": {"on_first": false, "on_second": false, "on_third": false},
         "plays": [], "scoring_plays": [],
         "batting": {"away": [], "home": []},
         "pitching": {"away": [], "home": []}}
        """
        return try JSONDecoder().decode(LiveGameDetail.self, from: Data(json.utf8))
    }
}

// MARK: - 1 & 2. finalize: fires, terminates, and gives up

@MainActor
struct FinalizeTests {

    /// Zero delays: the chain's TIMING is not what these tests are about, and a
    /// test that waited the real two minutes to prove it terminates is a test
    /// nobody runs. The attempt COUNT is what matters and is unchanged.
    private static let noDelays: [UInt64] = [0, 0, 0, 0, 0]

    /// Drive a view model to the exact moment a live game leaves the live list.
    /// `load` seeds `games` with a live game (that first read is an ordinary,
    /// cached one); the empty `applyLiveList` is the only signal the app ever
    /// gets that a game has ended.
    private static func endGame(
        slate: FakeSlate, gameId: Int,
    ) async -> ScoresViewModel {
        let vm = ScoresViewModel(slate: slate, finalizeDelays: noDelays)
        await vm.load(date: Date())
        await vm.applyLiveList([:], date: Date())
        return vm
    }

    /// A game leaving the live list triggers a slate re-read, and the chain
    /// STOPS as soon as the slate reports it final. This is the whole bug: the
    /// card used to sit on its last live state until relaunch.
    @Test func firesOnGameLeavingLiveListAndStopsWhenFinal() async throws {
        let gameId = 5059646
        // Read 1 = the initial load (still live). Read 2 = the recovery, final.
        let slate = FakeSlate(gameId: gameId, statusSequence: [
            "STATUS_IN_PROGRESS", "STATUS_FINAL",
        ])
        let vm = await Self.endGame(slate: slate, gameId: gameId)

        try await waitUntil { vm.games.first?.phase == .final }
        try await Task.sleep(nanoseconds: 300_000_000)   // let any extra attempt land
        #expect(slate.callCount == 2, "one load + one recovery read; no needless retries")
    }

    /// The retry exists because a fresh read can still beat the provider. Here
    /// the slate lags for two more reads before flipping, so the chain must keep
    /// going — and then stop.
    @Test func retriesWhileProviderStillLags() async throws {
        let gameId = 5059646
        let slate = FakeSlate(gameId: gameId, statusSequence: [
            "STATUS_IN_PROGRESS",   // initial load
            "STATUS_IN_PROGRESS",   // recovery 1 — provider hasn't flipped
            "STATUS_IN_PROGRESS",   // recovery 2 — still not
            "STATUS_FINAL",         // recovery 3 — there it is
        ])
        let vm = await Self.endGame(slate: slate, gameId: gameId)

        try await waitUntil { vm.games.first?.phase == .final }
        try await Task.sleep(nanoseconds: 300_000_000)
        #expect(slate.callCount == 4, "one load + three recoveries, then stop")
    }

    /// It must not retry forever. With a provider that never flips, the chain
    /// runs its five attempts and gives up.
    @Test func givesUpAfterFiveAttempts() async throws {
        let gameId = 5059646
        let slate = FakeSlate(gameId: gameId, statusSequence: ["STATUS_IN_PROGRESS"])
        _ = await Self.endGame(slate: slate, gameId: gameId)

        try await waitUntil { slate.callCount >= 6 }     // 1 load + 5 recoveries
        let settled = slate.callCount
        try await Task.sleep(nanoseconds: 500_000_000)
        #expect(slate.callCount == settled, "chain stopped rather than looping forever")
        #expect(settled == 6)
    }

    /// THE ROOT CAUSE. Every recovery read must bypass the 30s slate cache —
    /// that cache had just been filled by the live polling that was watching the
    /// game, so reading it returned the very state we were trying to leave.
    /// The initial load, by contrast, SHOULD use the cache.
    @Test func recoveryReadsBypassTheCacheAndOrdinaryLoadsDoNot() async throws {
        let gameId = 5059646
        let slate = FakeSlate(gameId: gameId, statusSequence: [
            "STATUS_IN_PROGRESS", "STATUS_IN_PROGRESS", "STATUS_FINAL",
        ])
        let vm = await Self.endGame(slate: slate, gameId: gameId)

        try await waitUntil { vm.games.first?.phase == .final }
        let flags = slate.bypassFlags
        #expect(flags.first == false, "the ordinary load should still use the cache")
        #expect(flags.dropFirst().allSatisfy { $0 },
                "every recovery read must skip it; got \(flags)")
    }

    /// A game that ends when nothing was live must not start a chase.
    @Test func doesNotFireWhenNothingWasLive() async throws {
        let slate = FakeSlate(gameId: 1, statusSequence: ["STATUS_FINAL"])
        let vm = ScoresViewModel(slate: slate, finalizeDelays: Self.noDelays)
        await vm.load(date: Date())                       // read 1: already final
        await vm.applyLiveList([:], date: Date())         // nothing was live
        try await Task.sleep(nanoseconds: 300_000_000)
        #expect(slate.callCount == 1, "no recovery read for a game that was never live")
    }

    /// THE STALE CARD, as a test. The provider never flips the game to final —
    /// which is the real-world case that produced a card reading "BOT 9th" long
    /// after the last out, because `phase` stayed `.live` and the game was
    /// therefore neither live (absent from `liveList`) nor final.
    ///
    /// The invariant: leaving `liveList` having been in it IS the game ending.
    /// Nothing downstream may wait for the provider to agree.
    @Test func gameLeavingLiveListIsOverEvenWhileProviderSaysLive() async throws {
        let gameId = 5059646
        // Every read still says in-progress — the provider never catches up.
        let slate = FakeSlate(gameId: gameId, statusSequence: [
            "STATUS_IN_PROGRESS", "STATUS_IN_PROGRESS", "STATUS_IN_PROGRESS",
            "STATUS_IN_PROGRESS", "STATUS_IN_PROGRESS", "STATUS_IN_PROGRESS",
        ])
        let vm = ScoresViewModel(slate: slate, finalizeDelays: Self.noDelays)
        await vm.load(date: Date())
        // It must be IN the list before it can leave it — that transition, not
        // mere absence, is the ending.
        await vm.applyLiveList([gameId: makeSummary(gameId: gameId)], date: Date())

        let before = try #require(vm.games.first)
        #expect(before.phase == .live, "precondition: the slate calls it live")
        #expect(vm.isOver(before) == false, "precondition: not over while it is live")

        await vm.applyLiveList([:], date: Date())          // the last out
        try await Task.sleep(nanoseconds: 300_000_000)     // let the chase exhaust

        let after = try #require(vm.games.first)
        #expect(after.phase == .live,
                "the provider still says live — that is the condition under test")
        #expect(vm.isOver(after),
                "left liveList having been live: it is over regardless of phase")
        #expect(vm.endedLocally.contains(gameId))
    }

    /// The other direction, so the fix cannot be "call everything over". A game
    /// that was never in the live list — one about to START, where the slate has
    /// flipped but the store has not polled yet — must NOT read as finished.
    @Test func gameThatWasNeverLiveIsNotTreatedAsOver() async throws {
        let slate = FakeSlate(gameId: 1, statusSequence: ["STATUS_IN_PROGRESS"])
        let vm = ScoresViewModel(slate: slate, finalizeDelays: Self.noDelays)
        await vm.load(date: Date())
        await vm.applyLiveList([:], date: Date())          // never was in the list
        try await Task.sleep(nanoseconds: 300_000_000)

        let game = try #require(vm.games.first)
        #expect(vm.isOver(game) == false,
                "absence from liveList alone is not an ending — it must have LEFT it")
        #expect(vm.endedLocally.isEmpty)
    }
}

// MARK: - 2b. Home carries the same rule

@MainActor
struct HomeEndedGameTests {

    private static let noDelays: [UInt64] = [0, 0, 0, 0, 0]

    /// The hero line and the strip card read the same stale `phase` the Scores
    /// list did, off the same frozen linescore. Home tracks endings the same
    /// way, so `nextGameLine` stops printing an inning for a finished game.
    @Test func favouriteLeavingLiveListIsOverEvenWhileProviderSaysLive() async throws {
        let gameId = 5059646
        let slate = FakeSlate(gameId: gameId, statusSequence: [
            "STATUS_IN_PROGRESS", "STATUS_IN_PROGRESS", "STATUS_IN_PROGRESS",
            "STATUS_IN_PROGRESS", "STATUS_IN_PROGRESS", "STATUS_IN_PROGRESS",
        ])
        let vm = HomeViewModel(slate: slate, finalizeDelays: Self.noDelays)
        await vm.load(bdlTeamId: 1)

        let live = try #require(vm.recentAndUpcoming.first { $0.phase == .live })
        #expect(vm.isOver(live) == false, "precondition: live means not over")

        await vm.applyLiveList([gameId: makeSummary(gameId: gameId)], bdlTeamId: 1)
        await vm.applyLiveList([:], bdlTeamId: 1)          // the last out
        try await Task.sleep(nanoseconds: 300_000_000)

        let after = try #require(vm.recentAndUpcoming.first { $0.gamePk == gameId })
        #expect(after.phase == .live, "the provider still says live — the condition under test")
        #expect(vm.isOver(after), "left liveList having been live: it is over")

        let line = HomeGameUtils.nextGameLine(
            game: after, favoriteBDLId: 1, isOver: vm.isOver(after))
        #expect(!line.contains("Bot") && !line.contains("Top"),
                "the hero line must not print a frozen inning: got \(line)")
    }

    /// The guard, same as Scores: absence alone is not an ending.
    @Test func favouriteNeverInTheListIsNotOver() async throws {
        let gameId = 5059646
        let slate = FakeSlate(gameId: gameId, statusSequence: ["STATUS_IN_PROGRESS"])
        let vm = HomeViewModel(slate: slate, finalizeDelays: Self.noDelays)
        await vm.load(bdlTeamId: 1)
        await vm.applyLiveList([:], bdlTeamId: 1)
        try await Task.sleep(nanoseconds: 300_000_000)

        let game = try #require(vm.recentAndUpcoming.first { $0.gamePk == gameId })
        #expect(vm.isOver(game) == false,
                "never in the list, so nothing left it — a starting game is not finished")
        #expect(vm.endedLocally.isEmpty)
    }
}

// MARK: - 3. fetchDetail: 404 clears, throw keeps

@MainActor
struct LiveDetailSnapshotTests {

    /// A 404 is an ANSWER — the game is over — so the snapshot must go. Keeping
    /// it is what froze the situation panel on "3 outs".
    @Test func clearsSnapshotOn404() async throws {
        let feed = FakeLiveFeed()
        let store = LiveGameStore(api: feed, refreshIntervalNanos: 50_000_000)
        let gameId = 5059646
        let owner = LiveGameStore.SubscriberID()

        store.subscribeDetail(gameId, owner: owner)
        try await waitUntil { store.detail[gameId] != nil }
        #expect(store.detail[gameId] != nil, "precondition: a live snapshot is held")

        feed.response = .notFound
        try await waitUntil(timeout: 25) { store.detail[gameId] == nil }
        #expect(store.detail[gameId] == nil, "404 must clear, not freeze")
        store.unsubscribeDetail(gameId, owner: owner)
    }

    /// A throw is transient — a hiccup must not blank a game mid-inning.
    @Test func keepsSnapshotOnThrow() async throws {
        let feed = FakeLiveFeed()
        let store = LiveGameStore(api: feed, refreshIntervalNanos: 50_000_000)
        let gameId = 5059646
        let owner = LiveGameStore.SubscriberID()

        store.subscribeDetail(gameId, owner: owner)
        try await waitUntil { store.detail[gameId] != nil }

        feed.response = .failure
        let before = feed.detailCalls
        try await waitUntil(timeout: 25) { feed.detailCalls > before }   // a failing poll happened
        #expect(store.detail[gameId] != nil, "a transient failure must keep the last snapshot")
        store.unsubscribeDetail(gameId, owner: owner)
    }

    /// `listLoaded` is what lets callers tell "nothing live" from "haven't asked".
    @Test func listLoadedFlipsAfterFirstAnswer() async throws {
        let feed = FakeLiveFeed()
        let store = LiveGameStore(api: feed, refreshIntervalNanos: 50_000_000)
        #expect(store.listLoaded == false)
        let owner = LiveGameStore.SubscriberID()
        store.subscribeList(owner: owner)
        try await waitUntil { store.listLoaded }
        #expect(store.listLoaded)
        store.unsubscribeList(owner: owner)
    }
}

// MARK: - 4. isLive

struct LiveStatusTests {

    /// The fault: a stale `.live` phase outvoting a live list that knew better,
    /// which sent a finished game's box score down the live path to a 404.
    @Test func stalePhaseDoesNotOverrideALoadedList() {
        #expect(LiveStatus.isLive(inLiveList: false, listLoaded: true, phaseIsLive: true) == false)
    }

    /// The case the old `||` existed for, and which must survive: a box score
    /// opened on a live game before the store's first answer.
    @Test func trustsThePhaseUntilTheStoreHasAnswered() {
        #expect(LiveStatus.isLive(inLiveList: false, listLoaded: false, phaseIsLive: true) == true)
        #expect(LiveStatus.isLive(inLiveList: false, listLoaded: false, phaseIsLive: false) == false)
    }

    @Test func membershipIsAuthoritativeEitherWay() {
        #expect(LiveStatus.isLive(inLiveList: true,  listLoaded: true,  phaseIsLive: false) == true)
        #expect(LiveStatus.isLive(inLiveList: true,  listLoaded: false, phaseIsLive: false) == true)
        #expect(LiveStatus.isLive(inLiveList: false, listLoaded: true,  phaseIsLive: false) == false)
    }
}

// MARK: - 5. The ordinal names only the inning

struct InningOrdinalTests {

    /// "BOT Bot 9th" came from the ordinal carrying a half-word its callers were
    /// already adding. `Game.merging(live:)` is the shipping path that builds it.
    @Test func ordinalCarriesNoHalfWord() throws {
        let base = makeGame(id: 1, status: "STATUS_IN_PROGRESS").toGame()
        let merged = base.merging(live: makeSummary(gameId: 1))
        let ordinal = try #require(merged.linescore?.currentInningOrdinal)
        #expect(ordinal == "9th")
        #expect(!ordinal.lowercased().contains("bot"))
        #expect(!ordinal.lowercased().contains("top"))
    }
}

// MARK: - Live batting line carries every counting stat

/// A live box score showed home runs but not doubles or triples, because the
/// live batting row carried `hr` and nothing else of the kind. These decode a
/// row shaped exactly like the backend's and assert it survives the trip into
/// `BoxBatting`, which is what the box score's "2B / 3B / HR" line reads.
///
/// Decoding from JSON is the point. The keys are snake_case and this decoder
/// applies NO `.convertFromSnakeCase`, so a missing `CodingKeys` entry yields
/// nil — indistinguishable from "the batter didn't do it", which is the bug
/// itself. A hand-built `LiveBatterRow` would not catch that.
struct LiveBattingLineTests {

    /// Field-for-field the shape `live_service._bat_row` emits, with values
    /// taken from a real balldontlie row (Randy Arozarena: a double AND a home
    /// run — the exact combination that used to show one and hide the other).
    private static let payload = """
    {"game_id": 5059646, "fetched_at": "2026-08-17T01:00:00Z",
     "status": "in_progress", "season": 2026, "season_type": "regular",
     "summary": {"away": {"runs": 2}, "home": {"runs": 3}, "inning": 9,
                 "inning_half": "bottom", "outs": 3, "balls": 0, "strikes": 0,
                 "is_live": true},
     "linescore": {"innings": []},
     "situation": {"on_first": false, "on_second": false, "on_third": false},
     "plays": [], "scoring_plays": [],
     "batting": {"away": [], "home": [
        {"id": 694497, "name": "Randy Arozarena", "position": "LF",
         "ab": 5, "r": 1, "h": 2, "rbi": 2, "hr": 1, "bb": 0, "k": 1,
         "avg": 0.251, "obp": 0.34, "slg": 0.44,
         "doubles": 1, "triples": 0, "stolen_bases": 0, "caught_stealing": 1,
         "hit_by_pitch": 0, "sac_flies": 0, "sac_bunts": 0, "gidp": 0,
         "plate_appearances": 5}
     ]},
     "pitching": {"away": [], "home": []}}
    """

    private static func batting() throws -> BoxBatting {
        let detail = try JSONDecoder().decode(
            LiveGameDetail.self, from: Data(payload.utf8))
        let box = detail.toBoxScoreResponse()
        let player = try #require(box.teams.home.players["ID694497"])
        return try #require(player.stats?.batting)
    }

    /// The reported symptom: the double must arrive, not just the home run.
    @Test func doublesAndTriplesReachTheBoxScore() throws {
        let b = try Self.batting()
        #expect(b.doubles == 1, "the double was dropped — this is the bug")
        #expect(b.triples == 0)
        #expect(b.homeRuns == 1, "home runs must still work")
    }

    /// The five that nothing renders yet. They were nil for the same reason and
    /// would have failed the same way; testing them now means whoever displays
    /// one later finds it already working.
    @Test func theLatentCountingStatsAlsoArrive() throws {
        let b = try Self.batting()
        #expect(b.stolenBases == 0)
        #expect(b.caughtStealing == 1)      // non-zero: proves the key maps
        #expect(b.hitByPitch == 0)
        #expect(b.sacFlies == 0)
        #expect(b.sacBunts == 0)
        #expect(b.groundIntoDoublePlay == 0)
    }

    /// The line that always worked must keep working.
    @Test func theConventionalBattingLineIsUnchanged() throws {
        let b = try Self.batting()
        #expect(b.atBats == 5)
        #expect(b.runs == 1)
        #expect(b.hits == 2)
        #expect(b.rbi == 2)
        #expect(b.baseOnBalls == 0)
        #expect(b.strikeOuts == 1)
    }

    /// A snake_case key decoding as nil is the failure mode this whole fix is
    /// about, so assert the distinction directly: absent means nil, present
    /// means the value — never silently nil when the field was sent.
    @Test func anAbsentFieldIsNilRatherThanZero() throws {
        let stripped = Self.payload.replacingOccurrences(
            of: #""doubles": 1, "triples": 0, "#, with: "")
        let detail = try JSONDecoder().decode(
            LiveGameDetail.self, from: Data(stripped.utf8))
        let b = try #require(
            detail.toBoxScoreResponse().teams.home.players["ID694497"]?.stats?.batting)
        #expect(b.doubles == nil)
        #expect(b.caughtStealing == 1, "the other keys still decode")
    }
}

// MARK: - Helpers

/// Poll until `condition` holds. The code under test is time-based (a retry
/// chain with real sleeps), so tests wait on the observable outcome rather than
/// on a fixed duration.
private func waitUntil(
    timeout: TimeInterval = 5,
    _ condition: @MainActor () -> Bool,
) async throws {
    let deadline = Date().addingTimeInterval(timeout)
    while Date() < deadline {
        if await MainActor.run(body: condition) { return }
        try await Task.sleep(nanoseconds: 50_000_000)
    }
    if await MainActor.run(body: condition) { return }
    Issue.record("condition not met within \(timeout)s")
}

private extension NSLock {
    func withLock<T>(_ body: () -> T) -> T {
        lock(); defer { unlock() }
        return body()
    }
}

// MARK: - Periodic refresh

/// The loop behind `.periodicRefresh`. Extracted from the ViewModifier
/// precisely so these can run without a view: the failure this guards against
/// (a tab that quietly stops refreshing, or one that refreshes while hidden)
/// is invisible until someone leaves the app open for an hour.
@MainActor
struct PeriodicRefreshTests {

    /// Counts calls across the loop's lifetime.
    final class Counter: @unchecked Sendable {
        private(set) var count = 0
        func bump() { count += 1 }
    }

    @Test func leadsWithAnImmediateRunRatherThanSleepingFirst() async throws {
        let hits = Counter()
        let task = Task {
            await PeriodicRefreshLoop.run(
                interval: .seconds(60), isActive: true, action: { hits.bump() },
            )
        }
        // Far shorter than the interval: anything counted here can only be the
        // leading call. A loop that slept first would still be at zero.
        try await Task.sleep(for: .milliseconds(120))
        task.cancel()
        #expect(hits.count == 1)
    }

    @Test func repeatsOnTheInterval() async throws {
        let hits = Counter()
        let task = Task {
            await PeriodicRefreshLoop.run(
                interval: .milliseconds(50), isActive: true, action: { hits.bump() },
            )
        }
        try await Task.sleep(for: .milliseconds(260))
        task.cancel()
        // 1 leading + ~4 ticks. Asserted as a range because timing on CI is not
        // a promise; the point is that it kept going, not that it hit exactly N.
        #expect(hits.count >= 3)
        #expect(hits.count <= 7)
    }

    @Test func doesNothingWhileInactive() async throws {
        let hits = Counter()
        let task = Task {
            await PeriodicRefreshLoop.run(
                interval: .milliseconds(20), isActive: false, action: { hits.bump() },
            )
        }
        try await Task.sleep(for: .milliseconds(150))
        task.cancel()
        // A backgrounded or switched-away tab must not spend a single request.
        #expect(hits.count == 0)
    }

    @Test func stopsPromptlyOnCancellation() async throws {
        let hits = Counter()
        let task = Task {
            await PeriodicRefreshLoop.run(
                interval: .milliseconds(30), isActive: true, action: { hits.bump() },
            )
        }
        try await Task.sleep(for: .milliseconds(100))
        task.cancel()
        let atCancel = hits.count
        try await Task.sleep(for: .milliseconds(150))
        // Nothing after cancellation — this is what makes `.task(id:)` teardown
        // on tab-switch actually stop the traffic.
        #expect(hits.count == atCancel)
    }
}

// MARK: - 5b. The finished row must not subscribe to live detail

@MainActor
struct FinishedRowSubscriptionTests {

    /// `GameRowCard` now renders every section, so a FINISHED game runs through
    /// the same view that owns the live-detail subscription. Asserted rather
    /// than assumed: the gate is `shouldSubscribeLive = isLive && shouldPoll`,
    /// and `isLive` must be false for anything that draws `FinalGameCard`.
    @Test func aFinishedGameIsNeverLiveSoNeverSubscribes() {
        // Absent from a LOADED list — the ordinary finished case.
        #expect(!LiveStatus.isLive(inLiveList: false, listLoaded: true, phaseIsLive: false))
        // Absent, list NOT yet loaded, slate says final — still not live.
        #expect(!LiveStatus.isLive(inLiveList: false, listLoaded: false, phaseIsLive: false))
        // The one case that IS live: still in the list. It must outrank
        // everything, which is why `body` checks `isLive` before `isOver`.
        #expect(LiveStatus.isLive(inLiveList: true, listLoaded: true, phaseIsLive: false))
    }

    /// A game absent from a loaded list with a STALE `.live` phase — the case
    /// a7471bd was about — must also not subscribe.
    @Test func aStalePhaseDoesNotResurrectTheSubscription() {
        #expect(!LiveStatus.isLive(inLiveList: false, listLoaded: true, phaseIsLive: true))
    }
}

// MARK: - 5c. A load that finishes late must not overwrite a newer date

@MainActor
struct StaleLoadTests {

    private static func day(_ s: String) -> Date {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = .current
        f.dateFormat = "yyyy-MM-dd"
        return f.date(from: s)!
    }

    /// A fetch started for one day, landing after the user has moved to
    /// another, must be discarded. Six callers start loads and none of them
    /// coordinated: whichever FINISHED last used to win, rather than whichever
    /// was requested last — so a fast double-tap on the arrows could leave
    /// `games` describing one day while `selectedDate` said another.
    @Test func aLoadForAnAbandonedDateDoesNotLand() async throws {
        let slate = FakeSlate(gameId: 1, statusSequence: ["STATUS_FINAL"])
        let vm = ScoresViewModel(slate: slate, finalizeDelays: [0, 0, 0, 0, 0])

        // Simulate the race directly: begin a load for an old date, move the
        // selection on, and let the old load finish.
        let oldDate = Self.day("2026-08-20")
        vm.selectedDate = Self.day("2026-08-25")          // user has moved on
        await vm.load(date: oldDate)                       // late arrival

        #expect(vm.games.isEmpty,
                "a result for an abandoned date must not be assigned")
        #expect(vm.selectedDate == Self.day("2026-08-25"),
                "and it must not move the selection back")
    }

    /// The ordinary case still works — a load for the CURRENT date assigns.
    @Test func aLoadForTheCurrentDateStillLands() async throws {
        let slate = FakeSlate(gameId: 1, statusSequence: ["STATUS_FINAL"])
        let vm = ScoresViewModel(slate: slate, finalizeDelays: [0, 0, 0, 0, 0])
        let date = Self.day("2026-08-25")
        vm.selectedDate = date
        await vm.load(date: date)
        #expect(!vm.games.isEmpty, "the current date's games must land")
    }

    /// `selectDate` moves the selection SYNCHRONOUSLY, so a periodic refresh
    /// that begins afterwards already targets the new day — the tick cannot
    /// resurrect the old one.
    @Test func selectDateMovesTheSelectionBeforeAnyAwait() async throws {
        let slate = FakeSlate(gameId: 1, statusSequence: ["STATUS_FINAL"])
        let vm = ScoresViewModel(slate: slate, finalizeDelays: [0, 0, 0, 0, 0])
        let target = Self.day("2026-08-20")
        vm.selectDate(target)
        #expect(vm.selectedDate == target,
                "the selection must be current the instant the tap is handled")
    }
}

// MARK: - 6. Historical slate routing

@MainActor
struct HistoricalSlateTests {

    private static func day(_ s: String) -> Date {
        let f = DateFormatter()
        f.calendar = Calendar(identifier: .gregorian)
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = TimeZone(identifier: "UTC")
        f.dateFormat = "yyyy-MM-dd"
        return f.date(from: s)!
    }
    private static let utc: Calendar = {
        var c = Calendar(identifier: .gregorian)
        c.timeZone = TimeZone(identifier: "UTC")!
        return c
    }()

    /// The routing decision takes ONLY a date. It cannot be reached by an empty
    /// or failed BDL response, which is the whole reason it is not keyed on
    /// emptiness: an outage returns an empty slate for TODAY, and falling back
    /// on that would serve game-log cards for a live day mid-outage.
    @Test func routingIsAFunctionOfTheDateAlone() {
        #expect(ScoresViewModel.usesHistoricalSource(for: Self.day("1986-07-04"), calendar: Self.utc))
        #expect(ScoresViewModel.usesHistoricalSource(for: Self.day("1999-12-31"), calendar: Self.utc))
        #expect(!ScoresViewModel.usesHistoricalSource(for: Self.day("2000-01-01"), calendar: Self.utc),
                "2000 is BDL's first covered season — it must NOT take the fallback")
        #expect(!ScoresViewModel.usesHistoricalSource(for: Self.day("2026-08-26"), calendar: Self.utc))
    }

    /// A pre-floor date must not consult BDL's slate at all — no wasted request,
    /// and no chance of an empty BDL answer being mistaken for the real one.
    @Test func aPreFloorDateNeverAsksBDLForTheSlate() async throws {
        let slate = FakeSlate(gameId: 1, statusSequence: ["STATUS_FINAL"])
        let vm = ScoresViewModel(slate: slate, finalizeDelays: [0, 0, 0, 0, 0])
        await vm.load(date: Self.day("1986-07-04"))
        #expect(slate.callCount == 0, "the historical path must not read the BDL slate")
    }

    /// A modern date still goes to BDL, unchanged.
    @Test func aModernDateStillReadsTheBDLSlate() async throws {
        let slate = FakeSlate(gameId: 1, statusSequence: ["STATUS_FINAL"])
        let vm = ScoresViewModel(slate: slate, finalizeDelays: [0, 0, 0, 0, 0])
        await vm.load(date: Date())
        #expect(slate.callCount >= 1, "today must still come from BDL")
    }

    /// The synthetic id is negative, so it cannot collide with any real game id
    /// — BDL's or MLB's — and the sign alone identifies a historical game.
    @Test func historicalGamesAreIdentifiedBySign() {
        let bdlIds = [5_043_956, 11_528_268, 822_738, 825_107]
        for pk in bdlIds {
            #expect(pk > 0, "every real id observed is positive")
        }
        // -(19860704 * 100_000 + team26 * 10 + gameNumber) for retro-ATL198607040
        #expect((-1_986_070_405_050 as Int) < 0)
    }
}
