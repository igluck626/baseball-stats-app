//
//  APIClient.swift
//  BaseballStats
//
//  Networking layer for the Railway-hosted FastAPI backend. Plain
//  URLSession + async/await — no third-party dependencies.
//

import Foundation

// MARK: - Errors

/// Errors surfaced to ViewModels. The backend returns 404 with a
/// `{"detail": "..."}` body for missing data, which we translate to
/// `.notFound` so callers can render an empty state instead of a crash.
enum APIError: Error, LocalizedError {
    case invalidURL
    case invalidResponse
    case notFound(String)
    case http(status: Int, message: String?)
    case decoding(underlying: Error)
    case transport(underlying: Error)

    var errorDescription: String? {
        switch self {
        case .invalidURL:
            return "Invalid request URL."
        case .invalidResponse:
            return "Server returned an unexpected response."
        case .notFound(let detail):
            return detail
        case .http(let status, let message):
            return message ?? "Server error (\(status))."
        case .decoding(let err):
            return "Could not parse server response: \(err.localizedDescription)"
        case .transport(let err):
            return "Network error: \(err.localizedDescription)"
        }
    }
}

// MARK: - Client

final class APIClient {
    /// Shared instance — most callers use this. Inject a custom one in tests.
    static let shared = APIClient()

    /// Production backend on Railway. Hardcoded for now; once we add a
    /// staging environment we'll wire this through Info.plist or a build
    /// config.
    static let baseURL = URL(string: "https://baseball-stats-app-production-0ef1.up.railway.app")!

    private let session: URLSession
    private let decoder: JSONDecoder

    init(session: URLSession = .shared) {
        self.session = session
        self.decoder = JSONDecoder()
    }

    // MARK: Endpoints

    /// `GET /players/search?name=...`. Returns an empty array on 404
    /// (the backend's "no matches" signal) so callers can treat empty
    /// results as a UI state rather than an error.
    func searchPlayers(name: String) async throws -> [PlayerSearchResult] {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.count >= 2 else { return [] }

        let url = try buildURL(
            path: "/players/search",
            query: [URLQueryItem(name: "name", value: trimmed)]
        )

        do {
            let response: SearchResponseEnvelope = try await get(url)
            return response.results
        } catch APIError.notFound {
            return []
        }
    }

    /// `GET /players/{id}/stats/current`. Returns nil on 404 — the player
    /// has no current-season batting line (e.g. retired, or pitcher-only).
    func getPlayerCurrentStats(playerId: Int) async throws -> PlayerCurrentStats? {
        let url = try buildURL(path: "/players/\(playerId)/stats/current")
        return try await getOptional(url)
    }

    /// `GET /players/{id}/stats/career`. Returns nil on 404.
    func getPlayerCareerStats(playerId: Int) async throws -> PlayerCareerStats? {
        let url = try buildURL(path: "/players/\(playerId)/stats/career")
        return try await getOptional(url)
    }

    /// `GET /players/{id}/pitching/current`. Returns nil on 404 — the
    /// player has no current-season pitching line (the common case for
    /// position players).
    func getPitcherCurrentStats(playerId: Int) async throws -> PitcherCurrentStats? {
        let url = try buildURL(path: "/players/\(playerId)/pitching/current")
        return try await getOptional(url)
    }

    /// `GET /players/{id}/pitching/career`. Returns nil on 404.
    func getPitcherCareerStats(playerId: Int) async throws -> PitcherCareerStats? {
        let url = try buildURL(path: "/players/\(playerId)/pitching/career")
        return try await getOptional(url)
    }

    /// `GET /players/{id}/gamelogs/batting?season=...`. Backend defaults
    /// `season` to the current year when omitted. Returns nil on 404.
    func getPlayerGameLogs(playerId: Int, season: Int? = nil) async throws -> GameLogResponse? {
        var query: [URLQueryItem] = []
        if let season {
            query.append(URLQueryItem(name: "season", value: String(season)))
        }
        let url = try buildURL(
            path: "/players/\(playerId)/gamelogs/batting",
            query: query
        )
        return try await getOptional(url)
    }

    /// `GET /teams/standings?year=...`. Returns nil on 404.
    func getStandings(year: Int) async throws -> StandingsResponse? {
        let url = try buildURL(
            path: "/teams/standings",
            query: [URLQueryItem(name: "year", value: String(year))]
        )
        return try await getOptional(url)
    }

    // MARK: - Internals

    private func buildURL(path: String, query: [URLQueryItem] = []) throws -> URL {
        guard var components = URLComponents(
            url: APIClient.baseURL.appendingPathComponent(path),
            resolvingAgainstBaseURL: false
        ) else {
            throw APIError.invalidURL
        }
        if !query.isEmpty {
            components.queryItems = query
        }
        guard let url = components.url else { throw APIError.invalidURL }
        return url
    }

    /// Generic GET that decodes the response body into `T` or throws.
    private func get<T: Decodable>(_ url: URL) async throws -> T {
        let (data, response) = try await dataTask(for: url)

        guard let http = response as? HTTPURLResponse else {
            throw APIError.invalidResponse
        }

        switch http.statusCode {
        case 200..<300:
            do {
                return try decoder.decode(T.self, from: data)
            } catch {
                throw APIError.decoding(underlying: error)
            }
        case 404:
            throw APIError.notFound(decodeDetail(from: data) ?? "Not found.")
        default:
            throw APIError.http(status: http.statusCode, message: decodeDetail(from: data))
        }
    }

    /// Like `get`, but maps 404 to nil so callers can render empty states
    /// without a do/catch.
    private func getOptional<T: Decodable>(_ url: URL) async throws -> T? {
        do {
            let value: T = try await get(url)
            return value
        } catch APIError.notFound {
            return nil
        }
    }

    /// URLSession.data wrapper that translates transport-level failures
    /// (no network, DNS, TLS, ...) into APIError.transport.
    private func dataTask(for url: URL) async throws -> (Data, URLResponse) {
        do {
            return try await session.data(from: url)
        } catch {
            throw APIError.transport(underlying: error)
        }
    }

    /// Pull `detail` out of FastAPI's standard error body
    /// (`{"detail": "..."}`). Returns nil when the body isn't shaped that way.
    private func decodeDetail(from data: Data) -> String? {
        struct DetailEnvelope: Decodable { let detail: String }
        return (try? JSONDecoder().decode(DetailEnvelope.self, from: data))?.detail
    }
}

// MARK: - Envelopes

/// `GET /players/search` returns `{ "query": "...", "results": [...] }`.
/// We expose only `results` to callers.
private struct SearchResponseEnvelope: Decodable {
    let query: String
    let results: [PlayerSearchResult]
}
