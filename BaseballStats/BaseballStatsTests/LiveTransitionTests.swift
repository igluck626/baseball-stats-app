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
