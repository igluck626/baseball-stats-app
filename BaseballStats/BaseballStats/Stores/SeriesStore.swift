//
//  SeriesStore.swift
//  BaseballStats
//
//  The current postseason's series lines ("AL Wild Card · Game 2 · BOS leads
//  1-0", "BOS wins 2-1"), keyed by balldontlie game id, for the Scores cards,
//  the box-score header and the Home strip.
//
//  ⚠️ HIDDEN, NEVER WRONG. A failed fetch leaves the store empty and every
//  series line simply absent. A regular-season game never gets one, whatever
//  the store holds (`line(for:)` checks `isRegularSeason` first).
//
//  Refetches when any game the store knows about goes final — it watches the
//  live store's list for that transition — so Game 2's pre-game line picks up
//  Game 1's result without waiting out the backend's 60s cache by much.
//

import Combine
import Foundation

@MainActor
final class SeriesStore: ObservableObject {
    static let shared = SeriesStore()

    /// balldontlie game id -> that game's series state.
    @Published private(set) var byGameId: [Int: PostseasonSeriesGame] = [:]

    private let fetch: (Int) async throws -> PostseasonSeriesResponse
    private var loadedSeason: Int?
    private var lastLoad: Date?
    private var inFlight = false
    private var liveWatch: AnyCancellable?
    private var lastStatus: [Int: String] = [:]

    init(fetch: @escaping (Int) async throws -> PostseasonSeriesResponse = { season in
        try await APIClient.shared.getPostseasonSeries(season: season)
    }) {
        self.fetch = fetch
    }

    /// The line to show under `game`, or nil (regular season, unknown game,
    /// or the endpoint failed).
    func line(for game: Game) -> String? {
        guard !game.isRegularSeason else { return nil }
        return byGameId[game.bdlGameId ?? game.gamePk]?.line
    }

    /// The short form for tight spaces (the Home strip): the series state —
    /// "BOS leads 1-0" — or, before a series has a result, "Game 1".
    func compactLine(for game: Game) -> String? {
        guard !game.isRegularSeason, let g = byGameId[game.bdlGameId ?? game.gamePk] else { return nil }
        return g.seriesStatus ?? "Game \(g.gameNumber)"
    }

    /// Load the current season if it is postseason time of year (September
    /// through December) — the app-start and foreground trigger. Nothing is
    /// fetched the rest of the year.
    func loadCurrentIfPostseason(now: Date = Date()) async {
        let cal = Calendar(identifier: .gregorian)
        let month = cal.component(.month, from: now)
        guard month >= 9 else { return }
        await load(season: cal.component(.year, from: now))
    }

    /// Load `season` unless it was loaded in the last `maxAge` seconds.
    func load(season: Int, force: Bool = false, maxAge: TimeInterval = 60) async {
        if !force, loadedSeason == season, let lastLoad, Date().timeIntervalSince(lastLoad) < maxAge {
            return
        }
        guard !inFlight else { return }
        inFlight = true
        defer { inFlight = false }
        do {
            let resp = try await fetch(season)
            var map: [Int: PostseasonSeriesGame] = [:]
            for s in resp.series {
                for g in s.games { map[g.gameId] = g }
            }
            byGameId = map
            loadedSeason = season
            lastLoad = Date()
        } catch {
            // Keep whatever we had — a stale line from 60s ago is still the
            // right series, and an empty store hides every line.
            lastLoad = nil
        }
    }

    /// Refetch when a game this store knows about goes final. Idempotent.
    func watch(_ live: LiveGameStore) {
        guard liveWatch == nil else { return }
        liveWatch = live.$liveList
            .receive(on: RunLoop.main)
            .sink { [weak self] list in
                Task { @MainActor in self?.noteLiveList(list.mapValues(\.status)) }
            }
    }

    /// Exposed for tests: feed the live list's statuses, and reload if any
    /// known postseason game has just turned final.
    func noteLiveList(_ statuses: [Int: String]) {
        let turnedFinal = statuses.contains { id, status in
            status.lowercased() == "final" && lastStatus[id].map { $0.lowercased() != "final" } == true
                && byGameId[id] != nil
        }
        lastStatus.merge(statuses) { _, new in new }
        if turnedFinal, let season = loadedSeason {
            Task { await self.load(season: season, force: true) }
        }
    }
}
