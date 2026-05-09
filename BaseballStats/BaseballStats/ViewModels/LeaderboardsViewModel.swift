//
//  LeaderboardsViewModel.swift
//  BaseballStats
//
//  Drives the Leaderboards tab. One fetch per (player type, stat, year)
//  selection. Selection state lives here so the view can re-bind the
//  picker without losing scroll/focus context.
//

import Combine
import Foundation

@MainActor
final class LeaderboardsViewModel: ObservableObject {

    // MARK: - Domain types

    enum PlayerKind: String, CaseIterable, Identifiable {
        case batter, pitcher
        var id: String { rawValue }
        var label: String {
            switch self {
            case .batter:  return "Batting"
            case .pitcher: return "Pitching"
            }
        }
    }

    /// League filter — "All" combines both leagues; AL/NL pass through to
    /// the backend's `league` query param. Standalone enum so the view's
    /// segmented control can iterate it directly.
    enum LeagueFilter: String, CaseIterable, Identifiable {
        case all = "All"
        case al  = "AL"
        case nl  = "NL"
        var id: String { rawValue }
        var label: String { rawValue }
        /// API param value — nil for "All", which the backend treats as
        /// "no filter".
        var apiValue: String? {
            switch self {
            case .all: return nil
            case .al:  return "AL"
            case .nl:  return "NL"
            }
        }
    }

    /// Stat keys are the user-facing labels and the API query values both —
    /// the backend's leaderboard catalog uses these exact strings.
    static let battingStats:  [String] = ["WAR", "HR", "AVG", "OPS", "RBI", "SB"]
    static let pitchingStats: [String] = ["WAR", "ERA", "SO", "W", "WHIP", "SV"]

    static let defaultBattingStat  = "WAR"
    static let defaultPitchingStat = "WAR"

    static var currentYear: Int {
        Calendar.current.component(.year, from: Date())
    }

    // MARK: - Selection

    @Published var playerKind: PlayerKind     = .batter
    @Published var selectedStat: String       = LeaderboardsViewModel.defaultBattingStat
    @Published var selectedYear: Int          = LeaderboardsViewModel.currentYear
    @Published var selectedLeague: LeagueFilter = .all

    // MARK: - State

    @Published var entries: [LeaderboardEntry] = []
    @Published var isLoading = false
    @Published var error: String?

    private let api: APIClient

    init(api: APIClient = .shared) {
        self.api = api
    }

    /// Returns the stat list appropriate to the current `playerKind`.
    var availableStats: [String] {
        playerKind == .batter ? Self.battingStats : Self.pitchingStats
    }

    /// Reset the stat to the default for the new player kind. Called when
    /// the user toggles Batting/Pitching — keeping a batting-only stat
    /// selected when switching to Pitching would 400 the next request.
    func resetStatForCurrentKind() {
        selectedStat = (playerKind == .batter)
            ? Self.defaultBattingStat
            : Self.defaultPitchingStat
    }

    /// Whether this stat label is part of the current player kind's catalog.
    /// Used to detect cross-kind selections that need a reset.
    func statBelongsToCurrentKind() -> Bool {
        availableStats.contains(selectedStat)
    }

    // MARK: - Loading

    func load() async {
        isLoading = true
        error = nil
        do {
            let response = try await api.getLeaderboard(
                stat:       selectedStat,
                year:       selectedYear,
                playerType: playerKind.rawValue,
                league:     selectedLeague.apiValue
            )
            entries = response?.leaders ?? []
        } catch {
            self.error = error.localizedDescription
            entries = []
        }
        isLoading = false
    }
}
