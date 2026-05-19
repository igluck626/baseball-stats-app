//
//  BallDontLieClient.swift
//  BaseballStats
//
//  Direct client for the BallDontLie MLB API — talked to from iOS
//  without going through our Railway backend. Replaces
//  `MLBStatsAPIClient` for everything except game logs (those will
//  move in a later migration phase).
//
//  Endpoints in use:
//    • /mlb/v1/games?dates[]=YYYY-MM-DD         — scores list
//    • /mlb/v1/games?dates[]=...&team_ids[]=... — team-scoped schedule
//    • /mlb/v1/stats?game_ids[]={id}            — per-player box-score lines
//    • /mlb/v1/lineups?game_id={id}             — starting lineup + DH
//    • /mlb/v1/plays?game_id={id}               — live play stream
//    • /mlb/v1/plate_appearances?game_id={id}   — PA stream (base runners)
//
//  Quirk note: BDL's live API uses SINGULAR param names on
//  /stats/{game}, /lineups, /plays, /plate_appearances despite the
//  OpenAPI spec advertising `game_ids[]`. /games on the other hand
//  takes `dates[]` and `team_ids[]` (plural). The methods below
//  send each endpoint exactly what the live API expects.
//

import Foundation

final class BallDontLieClient: @unchecked Sendable {
    static let shared = BallDontLieClient()

    private let session: URLSession
    private let decoder: JSONDecoder
    private let baseURL: String = "https://api.balldontlie.io"

    // GOAT-tier API key. Hardcoded for now — moves to a config /
    // secrets file in a later cleanup pass. Same key the backend
    // reads from the BDL_KEY env var, so anyone with the iOS bundle
    // has the same access the server does.
    private let apiKey: String = "7cb7d51d-bba5-41eb-9010-7314b5889d4e"

    private init(session: URLSession = .shared) {
        self.session = session
        self.decoder = JSONDecoder()
        // BDL ships snake_case keys; map them to Swift's camelCase
        // automatically so the Codable structs read naturally.
        decoder.keyDecodingStrategy = .convertFromSnakeCase
    }

    // MARK: - Games

    /// Games for one date. `date` is `yyyy-MM-dd` in the user's
    /// local timezone (BDL filters by date, not UTC instant).
    func getGames(date: String) async throws -> [BDLGame] {
        let items: [URLQueryItem] = [
            URLQueryItem(name: "dates[]",  value: date),
            URLQueryItem(name: "per_page", value: "100"),
        ]
        let envelope: BDLDataEnvelope<BDLGame> = try await fetch(path: "/mlb/v1/games", query: items)
        return envelope.data
    }

    /// Team-scoped schedule for one date. Used by the player-profile
    /// live-stats overlay — answers "does my team play today?"
    /// without pulling the full 15-game daily slate.
    func getTeamGames(date: String, teamId: Int) async throws -> [BDLGame] {
        let items: [URLQueryItem] = [
            URLQueryItem(name: "dates[]",    value: date),
            URLQueryItem(name: "team_ids[]", value: String(teamId)),
            URLQueryItem(name: "per_page",   value: "100"),
        ]
        let envelope: BDLDataEnvelope<BDLGame> = try await fetch(path: "/mlb/v1/games", query: items)
        return envelope.data
    }

    // MARK: - Box score

    /// Per-player batting + pitching lines for one game. Returns a
    /// flat list — caller groups by team via `BDLPlayerStat.team`
    /// (vs. the old client's team-nested response shape).
    func getGameStats(gameId: Int) async throws -> [BDLPlayerStat] {
        // BDL's /stats endpoint accepts game_ids[] (plural) per the
        // OpenAPI; the singular live-API quirk only applies to
        // /plays and /plate_appearances. Try plural first; on a
        // 400 about "must be a valid integer", fall back to singular.
        // In practice plural works for /stats.
        let items: [URLQueryItem] = [
            URLQueryItem(name: "game_ids[]", value: String(gameId)),
            URLQueryItem(name: "per_page",   value: "100"),
        ]
        let envelope: BDLDataEnvelope<BDLPlayerStat> = try await fetch(
            path: "/mlb/v1/stats", query: items,
        )
        return envelope.data
    }

    /// Starting lineup for one game — batting order + position +
    /// probable-pitcher flag. Used by the box-score view to render
    /// players in lineup order rather than the arbitrary order
    /// `/stats` returns them.
    func getGameLineup(gameId: Int) async throws -> [BDLGameLineup] {
        let items: [URLQueryItem] = [
            URLQueryItem(name: "game_id",  value: String(gameId)),
            URLQueryItem(name: "per_page", value: "100"),
        ]
        let envelope: BDLDataEnvelope<BDLGameLineup> = try await fetch(
            path: "/mlb/v1/lineups", query: items,
        )
        return envelope.data
    }

    // MARK: - Live game

    /// Sequential play log for one game. Pagination is via cursor;
    /// the live card only needs the latest few, but a full pull is
    /// supported by walking `next_cursor` if a future caller wants
    /// the whole stream. Returns plays in the order BDL ships them
    /// (chronological — earliest first; latest is at the tail).
    func getPlays(gameId: Int) async throws -> [BDLPlay] {
        let items: [URLQueryItem] = [
            URLQueryItem(name: "game_id",  value: String(gameId)),
            URLQueryItem(name: "per_page", value: "100"),
        ]
        return try await fetchAllPages(path: "/mlb/v1/plays", baseQuery: items)
    }

    /// Per-PA log with base-runner state and pitch-by-pitch detail.
    /// The live card uses the latest entry's `runnerOn{First,Second,
    /// Third}` to render the diamond — `/plays` doesn't carry that.
    func getPlateAppearances(gameId: Int) async throws -> [BDLPlateAppearance] {
        let items: [URLQueryItem] = [
            URLQueryItem(name: "game_id",  value: String(gameId)),
            URLQueryItem(name: "per_page", value: "100"),
        ]
        return try await fetchAllPages(
            path: "/mlb/v1/plate_appearances", baseQuery: items,
        )
    }

    // MARK: - Player resolver

    /// Resolve a BDL player id to our backend's player payload
    /// (MLBAM-keyed, with bio + team). Used when the user taps a
    /// player in a BDL-sourced box score and we need to push the
    /// existing PlayerProfileView, which is keyed on MLBAM ids.
    /// This call goes through OUR backend, not BDL.
    func resolveBDLPlayerId(_ bdlId: Int) async throws -> PlayerSearchResult {
        let url = APIClient.baseURL
            .appendingPathComponent("players")
            .appendingPathComponent("by-bdl-id")
            .appendingPathComponent(String(bdlId))
        let (data, response) = try await session.data(from: url)
        guard let http = response as? HTTPURLResponse,
              (200..<300).contains(http.statusCode) else {
            let code = (response as? HTTPURLResponse)?.statusCode ?? -1
            throw BallDontLieError.badStatus(code)
        }
        let dec = JSONDecoder()
        return try dec.decode(PlayerSearchResult.self, from: data)
    }

    // MARK: - Internals

    private func fetch<T: Decodable>(path: String, query: [URLQueryItem]) async throws -> T {
        guard var components = URLComponents(string: baseURL + path) else {
            throw BallDontLieError.badURL
        }
        components.queryItems = query
        guard let url = components.url else { throw BallDontLieError.badURL }

        var req = URLRequest(url: url)
        req.setValue(apiKey, forHTTPHeaderField: "Authorization")
        req.setValue("application/json", forHTTPHeaderField: "Accept")

        let (data, response) = try await session.data(for: req)
        guard let http = response as? HTTPURLResponse else {
            throw BallDontLieError.badStatus(-1)
        }
        guard (200..<300).contains(http.statusCode) else {
            throw BallDontLieError.badStatus(http.statusCode)
        }
        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            throw BallDontLieError.decoding(error)
        }
    }

    /// Walk every page of a paginated `data + meta.next_cursor`
    /// response and concatenate the results. Per-page calls share
    /// the same base query items; the cursor param is appended on
    /// each subsequent request.
    private func fetchAllPages<T: Decodable>(
        path: String, baseQuery: [URLQueryItem],
    ) async throws -> [T] {
        var all: [T] = []
        var cursor: Int? = nil
        repeat {
            var query = baseQuery
            if let cursor {
                query.append(URLQueryItem(name: "cursor", value: String(cursor)))
            }
            let envelope: BDLDataEnvelope<T> = try await fetch(path: path, query: query)
            all.append(contentsOf: envelope.data)
            cursor = envelope.meta?.nextCursor
        } while cursor != nil
        return all
    }
}

// MARK: - Envelope

/// Standard BDL response shape — `{ "data": [...], "meta": { "next_cursor": N } }`.
struct BDLDataEnvelope<T: Decodable>: Decodable {
    let data: [T]
    let meta: BDLMeta?
}

struct BDLMeta: Decodable {
    let nextCursor: Int?
    let perPage:    Int?
}

// MARK: - Errors

enum BallDontLieError: Error {
    case badURL
    case badStatus(Int)
    case decoding(Error)
}
