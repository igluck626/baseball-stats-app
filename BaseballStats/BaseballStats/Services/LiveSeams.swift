//
//  LiveSeams.swift
//  BaseballStats
//
//  Two narrow protocols that exist so the live→final transition can be tested
//  without waiting for a real game to end.
//
//  That transition produced a bug (commit 2b0fd74) whose only verification was
//  "watch a game finish while the app is open" — a window that is missed more
//  often than it is caught, and one that will come round again every time this
//  code is touched. These seams make the transition a unit test instead.
//
//  They are DELIBERATELY the smallest thing that does that job. Neither is an
//  abstraction over its client: the view models still hold a concrete
//  `BallDontLieClient` for standings, rosters, lineups and everything else, and
//  only the two slate reads go through `SlateProviding`. Resist widening these
//  to "all of BDL" or "all of our API" — a protocol that mirrors a whole client
//  is a maintenance cost with no test to show for it.
//

import Foundation

/// The day's schedule, which is the ONLY thing that reports a game as final —
/// `/live/games` publishes in-progress games and simply drops one when it ends,
/// so the slate is where "is it over?" is actually answered.
///
/// `bypassCache` is part of the protocol because it is the whole point: the
/// original bug was the recovery path reading a 30-second cache that had just
/// been filled with the pre-final slate, so a test has to be able to see which
/// value each caller passed.
protocol SlateProviding: Sendable {
    func getGames(date: String, bypassCache: Bool) async throws -> [BDLGame]
    func getTeamGames(date: String, teamId: Int, bypassCache: Bool) async throws -> [BDLGame]
}

extension BallDontLieClient: SlateProviding {}

/// Our backend's live proxy, as `LiveGameStore` consumes it.
///
/// `getLiveGame` returning `LiveGameDetail?` is the meaningful part: nil means
/// 404, which means the game is over, and the store must clear its snapshot
/// rather than keep it. A throw means transient and the snapshot must survive.
/// Those two were once collapsed into a `try?`; the seam lets a test hold them
/// apart.
protocol LiveFeedProviding: Sendable {
    func getLiveGames() async throws -> LiveGamesResponse
    func getLiveGame(id gameId: Int) async throws -> LiveGameDetail?
}

extension APIClient: LiveFeedProviding {}

/// Timing for the "chase a just-ended game's final state" retry chain, shared
/// by both view models so the two can't drift apart.
enum LiveFinalize {
    /// Delays between attempts, in seconds. Front-loaded because the slate
    /// usually flips within a few seconds; the tail exists because if it hasn't
    /// flipped within a minute it is the provider lagging, not us. Five attempts
    /// over roughly two minutes.
    ///
    /// Injectable on both view models purely so tests can collapse it — a test
    /// that actually waited two minutes to prove the chain terminates would be
    /// one nobody runs.
    static let defaultDelays: [UInt64] = [0, 3, 10, 30, 75]
}

/// Whether a game should be treated as live, given what the store knows and
/// what the caller was handed.
///
/// Extracted from `BoxScoreView` so it can be tested directly — it was the
/// third of the four faults, and it is pure logic that does not need a view.
/// The rule: membership in the live list is authoritative in BOTH directions,
/// but only once the store has actually answered; before that, fall back to the
/// status the caller was given.
enum LiveStatus {
    static func isLive(inLiveList: Bool, listLoaded: Bool, phaseIsLive: Bool) -> Bool {
        if inLiveList { return true }     // authoritative yes
        if listLoaded { return false }    // authoritative no — a stale phase must NOT override
        return phaseIsLive                // store has no opinion yet
    }
}
