//
//  MLBStatsAPIClient.swift
//  BaseballStats
//
//  Direct client for MLB's public Stats API — talked to from iOS
//  without going through our Railway backend. The Scores tab is
//  the only consumer today; it needs near-real-time game state
//  (linescore, decisions, live-inning) and shipping that through
//  a backend cache would add latency for no benefit.
//
//  Three endpoints in use:
//    • /api/v1/schedule        — list of games for a date
//    • /api/v1/game/{pk}/boxscore — batting + pitching lineups
//    • /api/v1.1/game/{pk}/feed/live — currently unused; reserved
//
//  This client is intentionally separate from `APIClient` (which
//  hits our Railway backend) so the two never tangle. All methods
//  are `async throws` and return decoded Codable models.
//

import Foundation

/// Not `@MainActor` — the client wraps `URLSession` + `JSONDecoder`,
/// which are safe off the main thread. Letting it stay nonisolated
/// means `MLBStatsAPIClient.shared` can be passed as a default
/// argument from non-MainActor contexts (the matching `APIClient`
/// pattern requires `@MainActor` because its callers do).
final class MLBStatsAPIClient: @unchecked Sendable {
    static let shared = MLBStatsAPIClient()
    private let session: URLSession
    private let decoder: JSONDecoder

    private init(session: URLSession = .shared) {
        self.session = session
        self.decoder = JSONDecoder()
        // MLB Stats API uses lower-camelCase keys natively, which is
        // what Swift's default Codable expects — no key conversion.
    }

    /// `/api/v1/schedule?sportId=1&date=YYYY-MM-DD&hydrate=team,linescore,decisions`
    ///
    /// One date per call. The hydrate string is what gives us
    /// `teams.away.team.name`, the inning-by-inning `linescore`, and
    /// `decisions` (winner/loser/save pitcher).
    func getSchedule(date: Date) async throws -> ScheduleResponse {
        let iso = Self.scheduleDateFormatter.string(from: date)
        var components = URLComponents(string: "https://statsapi.mlb.com/api/v1/schedule")!
        components.queryItems = [
            URLQueryItem(name: "sportId",  value: "1"),
            URLQueryItem(name: "date",     value: iso),
            URLQueryItem(name: "hydrate",  value: "team,linescore,decisions,probablePitcher"),
        ]
        return try await fetch(components.url!)
    }

    /// `/api/v1/game/{gamePk}/boxscore`
    func getBoxScore(gamePk: Int) async throws -> BoxScoreResponse {
        let url = URL(string: "https://statsapi.mlb.com/api/v1/game/\(gamePk)/boxscore")!
        return try await fetch(url)
    }

    /// `/api/v1.1/game/{gamePk}/feed/live` — full live game state.
    /// Drives the live game card (current batter / pitcher / count /
    /// base runners / last play) and the live BoxScoreView. Includes
    /// the boxscore subtree so live mode can render the same batting
    /// + pitching tables off the same response.
    func getLiveFeed(gamePk: Int) async throws -> LiveFeedResponse {
        let url = URL(string: "https://statsapi.mlb.com/api/v1.1/game/\(gamePk)/feed/live")!
        return try await fetch(url)
    }

    // MARK: - Internals

    private func fetch<T: Decodable>(_ url: URL) async throws -> T {
        let (data, response) = try await session.data(from: url)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            let code = (response as? HTTPURLResponse)?.statusCode ?? -1
            throw MLBStatsAPIError.badStatus(code)
        }
        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            throw MLBStatsAPIError.decoding(error)
        }
    }

    /// `yyyy-MM-dd` in the user's local timezone — what the schedule
    /// endpoint expects, and what the date strip pills are keyed on.
    /// Pinned to local timezone so "today" maps to the user's calendar
    /// day, not UTC's.
    static let scheduleDateFormatter: DateFormatter = {
        let f = DateFormatter()
        f.calendar = .init(identifier: .gregorian)
        f.timeZone = .current
        f.locale = .init(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()
}

enum MLBStatsAPIError: Error {
    case badStatus(Int)
    case decoding(Error)
}
