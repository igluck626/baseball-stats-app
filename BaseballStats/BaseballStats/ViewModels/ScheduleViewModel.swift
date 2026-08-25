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
    /// "W-L after this game", keyed by `gamePk`, for completed games only.
    /// Populated by the walk in `load`; a game with no entry renders no record,
    /// which is how upcoming rows stay as they were.
    @Published var recordAfter: [Int: String] = [:]
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
            self.recordAfter = Self.walkRecord(games: games, bdlTeamId: bdlTeamId)
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
    /// Running "W-L after this game" for every completed game, in date order.
    ///
    /// TWO THINGS HERE ARE LOAD-BEARING AND BOTH LOOK OPTIONAL.
    ///
    /// 1. `games` must already be REGULAR SEASON ONLY. `getTeamSeasonGames`
    ///    filters `seasonType == "regular"` inside the client, and this walk
    ///    depends on that having happened. BDL returns spring training in the
    ///    same season query — 194 rows for 2026 against 163 regular ones — so
    ///    walking the unfiltered set produces a record that is wrong by the
    ///    number of exhibition games played (it read 84-79 over 163 games in
    ///    late August, when the true figure was 69-63 over 132). It is wrong
    ///    silently, and only obviously wrong if you happen to notice a team
    ///    cannot have played 163 games in August.
    ///
    /// 2. The nil-score guard is not redundant with the `.final` check.
    ///    `HomeGameUtils.favoriteWon` returns FALSE when either score is
    ///    missing, so a final game with no score would book a silent loss and
    ///    shift every subsequent row by one. Reading the scores directly and
    ///    skipping the pair when either is nil is what prevents that. No game
    ///    in the current season triggers it; it is insurance against a data
    ///    gap that would otherwise be invisible in the middle of a 162-row list
    ///    and merely wrong at the end.
    ///
    /// A postponed game is not `.final`, so it is skipped and the sequence
    /// stays continuous across the gap — no special case needed.
    private static func walkRecord(games: [Game], bdlTeamId: Int) -> [Int: String] {
        var wins = 0, losses = 0
        var out: [Int: String] = [:]
        for game in games where game.phase == .final {
            let (favScore, oppScore) = HomeGameUtils.scores(
                game: game, favoriteBDLId: bdlTeamId,
            )
            guard let f = favScore, let o = oppScore else { continue }
            if f > o { wins += 1 } else { losses += 1 }
            out[game.gamePk] = "\(wins)-\(losses)"
        }
        return out
    }

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
