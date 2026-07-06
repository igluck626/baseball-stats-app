//
//  LiveGameStore.swift
//  BaseballStats
//
//  Phase 2 of the live-games redesign (client single-source-of-truth). ONE
//  root-injected store that owns all live-data fetching, so the Home favorite
//  card, the Scores list, each live card, and the box score read the SAME data
//  and can't disagree — the client mirror of the backend's single snapshot.
//
//  It holds two tiers, both `@Published` so SwiftUI re-renders on update:
//    • `liveList`  — every live game from ONE `/live/games` loop (compact
//                    summaries), shared by the Home card + Scores list.
//    • `detail`    — the full `/live/games/{id}` snapshot per SUBSCRIBED game,
//                    for the live cards + box score.
//
//  Detail loops are refcounted by DISTINCT subscriber TOKEN per game id
//  (`detailOwners: [Int: Set<SubscriberID>]`). Because that's a set, subscribe/
//  unsubscribe are idempotent by construction: subscribe-while-subscribed and
//  unsubscribe-while-unsubscribed are no-ops, the count can't go negative, and
//  a loop exists for an id IFF its owner set is non-empty. So a surface can
//  subscribe from `.task`/`.onChange(true)` and unsubscribe from
//  `.onChange(false)` AND `.onDisappear` in any interleaving without leaking a
//  loop or stopping one another surface still needs. See docs/live_redesign_phase2.md.
//
//  The store owns FETCHING only — NOT tab/lifecycle logic. Surfaces drive
//  start/stop/subscribe from the existing `AppNavigation.shouldPoll(on:)`
//  wiring, so the committed lifecycle work is reused, not duplicated.
//
//  NOTE (Phase 2, step 1): this type is injected but nothing reads it yet — all
//  existing per-surface loops keep running unchanged. Surfaces migrate onto it
//  in later steps.
//

import Combine
import SwiftUI

@MainActor
final class LiveGameStore: ObservableObject {

    /// Stable per-surface identity. Each subscribing view holds ONE in `@State`
    /// (`@State private var liveToken = LiveGameStore.SubscriberID()`), so the
    /// same view instance always presents the same token — which is what makes
    /// subscribe/unsubscribe idempotent.
    typealias SubscriberID = UUID

    // MARK: - Published state (views read these)

    /// All currently-live games, keyed by gamePk, from the shared `/live/games`
    /// loop. Empty when nothing is live or the list loop isn't running.
    @Published private(set) var liveList: [Int: LiveGameSummary] = [:]

    /// Full snapshot per SUBSCRIBED game id, from `/live/games/{id}`. Present
    /// only while at least one surface is subscribed to that id.
    @Published private(set) var detail: [Int: LiveGameDetail] = [:]

    /// Last error from the list loop, for a surface that wants a retry state.
    /// Transient failures keep the last `liveList` rather than blanking it.
    @Published private(set) var listError: String?

    // MARK: - Internals

    private let api: APIClient
    private var listTask: Task<Void, Never>?
    private var detailTasks: [Int: Task<Void, Never>] = [:]
    /// Distinct subscriber tokens per game id. Effective refcount = set.count.
    private var detailOwners: [Int: Set<SubscriberID>] = [:]

    /// Matches every other live loop's cadence (Scores/Home/box score = 15s).
    private static let refreshIntervalNanos: UInt64 = 15 * 1_000_000_000

    init(api: APIClient = .shared) {
        self.api = api
    }

    // MARK: - List loop (shared by Home card + Scores list)

    /// Start the single `/live/games` poll loop. Self-cancelling: a running loop
    /// is cancelled and replaced, so repeat calls can't leave two loops alive —
    /// and, since it always stop-and-restarts, every call is a fresh loop START.
    ///
    /// ALWAYS leads with a fetch before the first sleep (mirrors
    /// `startDetailLoop`), so `liveList` populates ~1s after the tab appears
    /// instead of after a full interval — otherwise a live game is miscategorized
    /// as Upcoming until the first fetch lands. There is no separate `immediate`
    /// flag: the list loop has no already-running-loop case to re-arm (it stops
    /// first), so a leading fetch on start is the whole story; a RESUME just
    /// calls this again, which restarts + leads with a fetch.
    func startListLoop() {
        stopListLoop()
        listTask = Task { @MainActor [weak self] in
            await self?.fetchList()             // leading fetch — always, on every loop start
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: Self.refreshIntervalNanos)
                guard !Task.isCancelled, let self else { return }
                await self.fetchList()
            }
        }
    }

    func stopListLoop() {
        listTask?.cancel()
        listTask = nil
    }

    private func fetchList() async {
        do {
            let resp = try await api.getLiveGames()
            var map: [Int: LiveGameSummary] = [:]
            for g in resp.games { map[g.gameId] = g }
            liveList = map
            listError = nil
        } catch {
            // Transient — keep the last snapshot (like every existing live loop)
            // and expose the message for an optional retry surface.
            listError = error.localizedDescription
        }
    }

    // MARK: - Detail loops (refcounted by distinct subscriber token)

    /// Register `owner`'s interest in game `gameId`'s full snapshot. Idempotent:
    /// subscribing when `(owner, gameId)` is already active is a no-op.
    ///
    /// `immediate` semantics — READ before relying on it (e.g. Step 5 box score):
    ///   • Loop START (set 0→1): `startDetailLoop` ALWAYS leads with a fetch, so
    ///     the first subscriber never waits an interval. `immediate` is
    ///     irrelevant on this transition — a brand-new loop always fetches first.
    ///   • Already-running loop (set 1→2, 2→3, …): the loop is already ticking
    ///     and `detail[gameId]` is (usually) already populated, so a newcomer
    ///     sees data at once. Pass `immediate: true` to ALSO trigger a one-shot
    ///     fetch now (freshest possible for a resume / a box score joining a
    ///     card's loop); `immediate: false` just joins without an extra fetch.
    func subscribeDetail(_ gameId: Int, owner: SubscriberID, immediate: Bool = false) {
        var owners = detailOwners[gameId] ?? []
        let (inserted, _) = owners.insert(owner)
        detailOwners[gameId] = owners
        guard inserted else { return }          // already subscribed → no-op
        if owners.count == 1 {
            startDetailLoop(gameId)             // 0→1: start the loop (always leads with a fetch)
        } else if immediate {
            // Loop already running for another owner — hand the newcomer a fresh
            // value now rather than making it wait for the next tick.
            Task { @MainActor [weak self] in await self?.fetchDetail(gameId) }
        }
    }

    /// Release `owner`'s interest. Idempotent: unsubscribing when
    /// `(owner, gameId)` is not active is a no-op (can't go negative or stop a
    /// loop someone else needs). The LAST owner leaving (set 1→0) stops the loop
    /// and evicts `detail[gameId]`.
    func unsubscribeDetail(_ gameId: Int, owner: SubscriberID) {
        guard var owners = detailOwners[gameId],
              owners.remove(owner) != nil else { return }   // not subscribed → no-op
        if owners.isEmpty {
            detailOwners[gameId] = nil
            detailTasks[gameId]?.cancel()
            detailTasks[gameId] = nil
            detail[gameId] = nil
        } else {
            detailOwners[gameId] = owners
        }
    }

    /// Start (or replace) the detail poll loop for `gameId`. ALWAYS leads with a
    /// fetch before the first sleep, so the first subscriber's situation/inning
    /// populates on the first cycle (~1s) instead of after a full interval — no
    /// caller can accidentally get a delayed first load. `fetchDetail` is a
    /// single `/live/games/{id}` call (the backend owns any schedule/lineup
    /// warm-up), so nothing gates the first detail snapshot reaching `detail`.
    private func startDetailLoop(_ gameId: Int) {
        detailTasks[gameId]?.cancel()
        detailTasks[gameId] = Task { @MainActor [weak self] in
            await self?.fetchDetail(gameId)         // leading fetch — always, on every loop start
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: Self.refreshIntervalNanos)
                guard !Task.isCancelled, let self else { return }
                await self.fetchDetail(gameId)
            }
        }
    }

    private func fetchDetail(_ gameId: Int) async {
        // nil = 404 (game not live/over); throw = transient. Either way keep the
        // last snapshot so a hiccup doesn't blank the UI. The loop runs until the
        // last subscriber leaves (invariant), so we don't self-terminate here —
        // surfaces unsubscribe via their lifecycle wiring.
        if let d = try? await api.getLiveGame(id: gameId) {
            detail[gameId] = d
        }
    }
}
