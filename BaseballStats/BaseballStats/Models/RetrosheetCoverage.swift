//
//  RetrosheetCoverage.swift
//  BaseballStats
//
//  How far our own record runs — read from the service, not hardcoded.
//

import Foundation

/// The newest season served from our Retrosheet tables rather than the
/// provider.
///
/// ⚠️ WHY THIS IS NOT A CONSTANT. The boundary is not "last year"; it is
/// "the newest season Retrosheet has published and we have finished
/// ingesting". A constant encodes the first as a proxy for the second, and the
/// proxy fails at the first rollover: once Retrosheet publishes a season and
/// we load it, a client pinned to the old number keeps reading that season
/// from the provider — silently, on the thinner source — and every season
/// after inherits the same fault, one a year.
///
/// FETCHED ONCE PER SESSION. The value moves at most annually, so re-reading
/// it would be traffic spent on a number that almost never changes. If the
/// backend's answer changes WHILE the app is running — the ingest finishing
/// mid-session is the only realistic way — this keeps the value it started
/// with until the next launch. That is a deliberate no-op rather than an
/// oversight: the alternative is a slate that reroutes under the reader
/// between one date and the next, and being one launch stale costs nothing.
///
/// THE FALLBACK IS THE OLD CONSTANT. A failed or slow request leaves
/// `lastSeason` at 2025, which is exactly today's behaviour — so a backend
/// outage degrades to what shipped before this existed rather than to a broken
/// boundary.
enum RetrosheetCoverage {

    /// Used until the service answers, and kept if it never does.
    static let fallbackLastSeason = 2025

    private static let lock = NSLock()
    // Read from view bodies and view models on whatever actor they run on, and
    // written exactly once per session behind the lock. `nonisolated(unsafe)`
    // states that plainly rather than forcing every synchronous reader —
    // `Game.usesRetrosheetBoxScore` among them — onto the main actor.
    nonisolated(unsafe) private static var cached = fallbackLastSeason
    nonisolated(unsafe) private static var didLoad = false

    static var lastSeason: Int {
        lock.lock(); defer { lock.unlock() }
        return cached
    }

    /// Read the boundary once. Subsequent calls return immediately, including
    /// after a failure — one outage should not mean a request per slate load
    /// for the rest of the session.
    static func ensureLoaded(api: APIClient = .shared) async {
        lock.lock()
        let already = didLoad
        lock.unlock()
        if already { return }

        let fetched = try? await api.getRetrosheetLastSeason()
        lock.lock()
        didLoad = true
        // Sanity-gate the answer rather than trusting it: a boundary in the
        // future would route the live season to tables that cannot hold it,
        // and one before 1898 would route everything to the provider.
        if let f = fetched, f >= 1898,
           f <= Calendar(identifier: .gregorian).component(.year, from: Date()) {
            cached = f
        }
        lock.unlock()
    }

    /// Test seam — resets to the unloaded state.
    static func resetForTesting(to season: Int? = nil) {
        lock.lock()
        cached = season ?? fallbackLastSeason
        didLoad = (season != nil)
        lock.unlock()
    }
}
