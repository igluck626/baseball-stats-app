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
    /// is cancelled and replaced, so repeat calls can't leave two loops alive.
    /// `immediate` does one leading fetch before the sleep loop — the
    /// foreground/visible RESUME path, matching the committed lifecycle pattern.
    func startListLoop(immediate: Bool = false) {
        stopListLoop()
        listTask = Task { @MainActor [weak self] in
            if immediate {
                guard let self else { return }
                await self.fetchList()
            }
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
    /// subscribing when `(owner, gameId)` is already active is a no-op. The
    /// FIRST distinct owner (set 0→1) starts that id's loop; later owners share
    /// it. `immediate` gives a new subscriber an instant fetch instead of
    /// waiting a full interval.
    func subscribeDetail(_ gameId: Int, owner: SubscriberID, immediate: Bool = false) {
        var owners = detailOwners[gameId] ?? []
        let (inserted, _) = owners.insert(owner)
        detailOwners[gameId] = owners
        guard inserted else { return }          // already subscribed → no-op
        if owners.count == 1 {
            startDetailLoop(gameId, immediate: immediate)   // 0→1: start the loop
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

    private func startDetailLoop(_ gameId: Int, immediate: Bool) {
        detailTasks[gameId]?.cancel()
        detailTasks[gameId] = Task { @MainActor [weak self] in
            if immediate {
                guard let self else { return }
                await self.fetchDetail(gameId)
            }
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
