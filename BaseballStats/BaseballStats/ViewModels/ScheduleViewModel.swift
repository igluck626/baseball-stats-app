//
//  ScheduleViewModel.swift
//  BaseballStats
//
//  Drives the Home tab's Schedule sheet. Fetches the full regular-
//  season schedule for the favorite team in one paginated call
//  via `BallDontLieClient.getTeamSeasonGames(season:teamId:)`,
//  then groups the result by month so the sheet can render the
//  list with one sticky header per month.
//

import Combine
import Foundation

@MainActor
final class ScheduleViewModel: ObservableObject {
    /// Ordered (month-name, games) buckets. Month name is the user's
    /// locale's full month label ("March" / "April" / …). Games
    /// inside each bucket are ascending by start time.
    @Published var gamesByMonth: [(month: String, games: [Game])] = []
    @Published var isLoading: Bool = false
    @Published var didLoad: Bool = false
    @Published var error: String?

    private let bdl: BallDontLieClient

    init(bdl: BallDontLieClient = .shared) {
        self.bdl = bdl
    }

    func load(bdlTeamId: Int) async {
        isLoading = true
        error = nil
        let year = Calendar.current.component(.year, from: Date())
        do {
            let bdlGames = try await bdl.getTeamSeasonGames(
                season: year, teamId: bdlTeamId,
            )
            let games = bdlGames
                .map { $0.toGame() }
                .sorted {
                    ($0.startDate ?? .distantFuture)
                        < ($1.startDate ?? .distantFuture)
                }
            self.gamesByMonth = Self.group(games: games)
            self.didLoad = true
        } catch {
            self.error = error.localizedDescription
            self.gamesByMonth = []
        }
        isLoading = false
    }

    /// Bucket games by their `(year, month)` in the user's local
    /// calendar and return ordered (month-name, [Game]) pairs.
    /// Year+month composite key — covers the (unlikely but
    /// theoretical) case of a postseason wrap straddling the new
    /// year, even though `getTeamSeasonGames` filters to regular
    /// season only.
    private static func group(games: [Game]) -> [(month: String, games: [Game])] {
        var buckets: [(yearMonth: Int, monthName: String, games: [Game])] = []
        var index: [Int: Int] = [:]
        let cal = Calendar.current
        for game in games {
            guard let start = game.startDate else { continue }
            let comps = cal.dateComponents([.year, .month], from: start)
            let ym = (comps.year ?? 0) * 100 + (comps.month ?? 0)
            if let idx = index[ym] {
                buckets[idx].games.append(game)
            } else {
                index[ym] = buckets.count
                buckets.append((
                    ym,
                    monthFormatter.string(from: start),
                    [game],
                ))
            }
        }
        return buckets
            .sorted { $0.yearMonth < $1.yearMonth }
            .map { ($0.monthName, $0.games) }
    }

    private static let monthFormatter: DateFormatter = {
        let f = DateFormatter()
        f.locale   = .current
        f.timeZone = .current
        f.dateFormat = "LLLL"   // full standalone month name
        return f
    }()
}
