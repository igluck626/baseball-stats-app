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

    /// `GET /players/heat?limit=N`. League-wide hot/cold leaders for the
    /// Search-tab discovery shelves — six lists (hot/cold × hitters/
    /// starters/relievers). The backend always returns the keys (empty when
    /// no heat is computed yet), so this never 404s.
    func getHeatLeaders(limit: Int = 12) async throws -> HeatLeadersResponse {
        let url = try buildURL(
            path: "/players/heat",
            query: [URLQueryItem(name: "limit", value: String(limit))]
        )
        return try await get(url)
    }

    /// `GET /news?team={lahmanCode}&limit=N`. Newest-first team news. Pass a
    /// Lahman team code to scope to one team, or nil for league-wide. The
    /// backend wraps the list in `{"articles": [...]}`.
    func getNews(team: String?, limit: Int = 15) async throws -> [NewsArticle] {
        var query = [URLQueryItem(name: "limit", value: String(limit))]
        if let team {
            query.append(URLQueryItem(name: "team", value: team))
        }
        let url = try buildURL(path: "/news", query: query)
        let response: NewsResponse = try await get(url)
        return response.articles
    }

    /// `GET /players/by-mlb-id/{id}`. Direct lookup by MLB Stats API id.
    /// Returns nil on 404 (no player by that id in our DB). Used by the
    /// Scores tab when the user taps a player in a box score — the
    /// live-feed names players by MLBAM id and we need to resolve to a
    /// `PlayerSearchResult` to drive `PlayerProfileView`.
    func getPlayerByMlbId(_ mlbId: Int) async throws -> PlayerSearchResult? {
        let url = try buildURL(path: "/players/by-mlb-id/\(mlbId)")
        return try await getOptional(url)
    }

    /// `GET /players/{id}/stats/current`. Returns nil on 404 — the player
    /// has no current-season batting line (e.g. retired, or pitcher-only).
    func getPlayerCurrentStats(playerId: Int) async throws -> PlayerCurrentStats? {
        let url = try buildURL(path: "/players/\(playerId)/stats/current")
        return try await getOptional(url)
    }

    /// `GET /players/{id}/batter-stats-at-date?game_date=YYYY-MM-DD`.
    /// Returns the batter's cumulative HR / 2B / 3B through (and
    /// including) `gameDate`, plus an `includes_today` signal the
    /// caller can use to decide whether to add today's just-hit
    /// counts on top. Same shape and motivation as the pitcher
    /// endpoint — point-in-time totals from our own game logs so
    /// notable-plays lines stay accurate even when BDL's season-stats
    /// snapshot is stale.
    func getBatterStatsAtDate(
        playerId: Int, gameDate: String,
    ) async throws -> BatterStatsAtDate? {
        let url = try buildURL(
            path: "/players/\(playerId)/batter-stats-at-date",
            query: [URLQueryItem(name: "game_date", value: gameDate)],
        )
        guard let resp: BatterStatsAtDateResponse = try await getOptional(url) else {
            return nil
        }
        return BatterStatsAtDate(
            homeRuns:      resp.home_runs,
            doubles:       resp.doubles,
            triples:       resp.triples,
            includesToday: resp.includes_today,
        )
    }

    /// `GET /players/{id}/pitcher-record-at-date?game_date=YYYY-MM-DD`.
    /// Returns the pitcher's cumulative W/L/SV through (and including)
    /// `gameDate`, counted from `pitching_gamelogs` for the season
    /// the date falls in. Used by the box-score and score-card views
    /// to render an accurate AS-OF record next to a decision pitcher,
    /// independent of any later games that may have updated BDL's
    /// season-stats snapshot.
    func getPitcherRecordAtDate(
        playerId: Int, gameDate: String,
    ) async throws -> PitcherRecord? {
        let url = try buildURL(
            path: "/players/\(playerId)/pitcher-record-at-date",
            query: [URLQueryItem(name: "game_date", value: gameDate)],
        )
        guard let resp: PitcherRecordResponse = try await getOptional(url) else {
            return nil
        }
        return PitcherRecord(
            wins:          resp.wins,
            losses:        resp.losses,
            saves:         resp.saves,
            includesToday: resp.includes_today,
        )
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

    /// `GET /players/{id}/gamelogs/batting?season=...`. Returns nil on
    /// 404 (no batting logs cached for that season). The backend
    /// auto-fetches from the MLB Stats API on cache miss, so the first
    /// call for a given (player, season) can be slow.
    func getBattingGameLogs(playerId: Int, season: Int) async throws -> GameLogResponse? {
        let url = try buildURL(
            path: "/players/\(playerId)/gamelogs/batting",
            query: [URLQueryItem(name: "season", value: String(season))]
        )
        return try await getOptional(url)
    }

    /// `GET /players/{id}/gamelogs/pitching?season=...`. Returns nil on
    /// 404. Same auto-fetch caveat as `getBattingGameLogs`.
    func getPitchingGameLogs(playerId: Int, season: Int) async throws -> GameLogResponse? {
        let url = try buildURL(
            path: "/players/\(playerId)/gamelogs/pitching",
            query: [URLQueryItem(name: "season", value: String(season))]
        )
        return try await getOptional(url)
    }

    /// `GET /players/{id}/awards`. Returns nil on 404 (player has
    /// no award rows, no All-Star selections, and no vote-share
    /// rows — common for players who never finished in a vote).
    func getPlayerAwards(playerId: Int) async throws -> PlayerAwardsResponse? {
        let url = try buildURL(path: "/players/\(playerId)/awards")
        return try await getOptional(url)
    }

    /// `GET /awards/voting?award=&year=&league=`. Returns nil on
    /// 404 (no voting results for that triple — possible for
    /// pre-1956 years before Lahman's share file has data).
    func getAwardVoting(
        award: String,
        year: Int,
        league: String
    ) async throws -> AwardVotingResponse? {
        let url = try buildURL(
            path: "/awards/voting",
            query: [
                URLQueryItem(name: "award", value: award),
                URLQueryItem(name: "year",  value: String(year)),
                URLQueryItem(name: "league", value: league),
            ]
        )
        return try await getOptional(url)
    }

    /// `GET /awards/available`. Enumerates which (award, year, league) voting
    /// combinations exist, grouped per award (newest year first), so the
    /// picker offers only valid choices. No params; the backend always returns
    /// a body (empty `awards` if the DB has none), so this never 404s.
    func getAwardsAvailable() async throws -> AwardsAvailableResponse {
        let url = try buildURL(path: "/awards/available")
        return try await get(url)
    }

    /// `GET /postseason/available`. Years that have postseason data, newest
    /// first — drives the Playoff History year picker. No params; always
    /// returns a body, so this never 404s.
    func getPostseasonAvailable() async throws -> PostseasonAvailableResponse {
        let url = try buildURL(path: "/postseason/available")
        return try await get(url)
    }

    /// `GET /postseason?year=YYYY`. Every postseason series that year, both
    /// leagues. Series come back sorted by `round_code` (not bracket order);
    /// the client assembles the bracket. An empty `series` is a valid response.
    func getPostseason(year: Int) async throws -> PostseasonYearResponse {
        let url = try buildURL(
            path: "/postseason",
            query: [URLQueryItem(name: "year", value: String(year))]
        )
        return try await get(url)
    }

    /// `GET /postseason/champions`. Every World Series result, newest year
    /// first, in one call — drives the Playoff History champions list. No
    /// params; always returns a body, so this never 404s.
    func getPostseasonChampions() async throws -> PostseasonChampionsResponse {
        let url = try buildURL(path: "/postseason/champions")
        return try await get(url)
    }

    /// `GET /teams/standings?year=...`. Returns nil on 404.
    func getStandings(year: Int) async throws -> StandingsResponse? {
        let url = try buildURL(
            path: "/teams/standings",
            query: [URLQueryItem(name: "year", value: String(year))]
        )
        return try await getOptional(url)
    }

    /// `GET /teams/{team_id}/history`. Returns nil on 404 — happens
    /// only for codes the franchise-resolution can't map to any
    /// `team_seasons` row (extreme historical typos, etc.).
    func getTeamHistory(teamId: String) async throws -> TeamHistoryResponse? {
        let url = try buildURL(path: "/teams/\(teamId)/history")
        return try await getOptional(url)
    }

    /// `GET /teams/{team_id}/postseason`. Returns nil on 404. Empty
    /// `postseason` is a valid response for a franchise with no
    /// playoff appearances yet — the call still succeeds.
    func getTeamPostseason(teamId: String) async throws -> TeamPostseasonResponse? {
        let url = try buildURL(path: "/teams/\(teamId)/postseason")
        return try await getOptional(url)
    }

    /// `GET /teams/{team_id}/awards`. Returns nil on 404 (no franchise
    /// match). An empty `awards` array is a valid success response for
    /// a franchise with no major-award winners on record. Winners are
    /// grouped by award type (MVP / CY Young / ROY / Gold Glove /
    /// Silver Slugger), sorted year-desc within each group.
    func getTeamAwards(teamId: String) async throws -> TeamAwardsResponse? {
        let url = try buildURL(path: "/teams/\(teamId)/awards")
        return try await getOptional(url)
    }

    /// `GET /leaderboards?stat=&year=&player_type=&league=&team=`.
    /// Returns nil on 404 (e.g. a season with no qualifying rate-stat
    /// leaders). Sort order is decided server-side: ERA / WHIP
    /// ascending, everything else descending. Pass `league = nil` to
    /// combine both leagues; "AL" / "NL" filter server-side. Pass
    /// `team = nil` for all teams; otherwise a Lahman team code
    /// ("NYA" / "LAN" / "FLO" / …) — the backend expands historical
    /// variants ("FLO" → also "MIA", etc.).
    func getLeaderboard(
        stat: String,
        year: Int?,
        playerType: String,
        mode: String = "season",
        league: String? = nil,
        team: String? = nil,
        yearFrom: Int? = nil,
        yearTo: Int? = nil,
        limit: Int = 25
    ) async throws -> LeaderboardResponse? {
        var query: [URLQueryItem] = [
            URLQueryItem(name: "stat",        value: stat),
            URLQueryItem(name: "player_type", value: playerType),
            URLQueryItem(name: "mode",        value: mode),
            URLQueryItem(name: "limit",       value: String(limit)),
        ]
        // `year` is required only in season mode; we omit it in
        // all-time / career so the URL doesn't carry a value the
        // backend would silently ignore.
        if let year {
            query.append(URLQueryItem(name: "year", value: String(year)))
        }
        if let league {
            query.append(URLQueryItem(name: "league", value: league))
        }
        if let team {
            query.append(URLQueryItem(name: "team", value: team))
        }
        // Year-range floor + ceiling for the all-time / career
        // slider. Caller passes nil for either bound when it's at the
        // edge of the slider's bounds, keeping the URL clean.
        if let yearFrom {
            query.append(URLQueryItem(name: "year_from", value: String(yearFrom)))
        }
        if let yearTo {
            query.append(URLQueryItem(name: "year_to", value: String(yearTo)))
        }
        let url = try buildURL(path: "/leaderboards", query: query)
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
            // URLComponents leaves "+" literal in query values, but the
            // server decodes "+" as a space (form-urlencoded). Force it to
            // %2B so stats like "OPS+"/"ERA+" survive the round-trip. Safe:
            // real spaces are already encoded as %20 (never "+"), so any
            // remaining "+" is a literal plus from a value like "ERA+".
            components.percentEncodedQuery = components.percentEncodedQuery?
                .replacingOccurrences(of: "+", with: "%2B")
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

/// Pitcher's cumulative W/L/SV through a given date — three plain
/// counters extracted from a `pitching_gamelogs` row scan on the
/// backend, plus the `includesToday` signal callers use to decide
/// whether to manually fold today's just-decided game into the
/// record (true → already counted, false → caller should bump).
struct PitcherRecord: Codable, Hashable {
    let wins:          Int
    let losses:        Int
    let saves:         Int
    /// True iff at least one gamelog row in the response falls on
    /// today's Eastern-local calendar date. False means the
    /// nightly / catch-up hasn't reached today yet and the caller
    /// needs to manually add the pitcher's current-game decision.
    let includesToday: Bool
}

/// Raw shape `GET /players/{id}/pitcher-record-at-date` returns.
/// `player_id` and `game_date` are passed-through echoes from the
/// query; we extract just the counters + `includes_today` into
/// `PitcherRecord` at the API-client boundary so call sites don't
/// depend on the round-trip metadata.
private struct PitcherRecordResponse: Decodable {
    let player_id:      Int
    let game_date:      String
    let wins:           Int
    let losses:         Int
    let saves:          Int
    let includes_today: Bool
}

/// Sister to `PitcherRecord` — batter's cumulative HR / 2B / 3B
/// through a given date, plus the `includesToday` signal callers
/// use to decide whether to add today's per-game counts on top
/// (true → already counted, false → caller should add).
struct BatterStatsAtDate: Codable, Hashable {
    let homeRuns:      Int
    let doubles:       Int
    let triples:       Int
    /// True iff at least one gamelog row in the response falls on
    /// today's Eastern-local calendar date. Same semantics as
    /// `PitcherRecord.includesToday`.
    let includesToday: Bool
}

/// Raw shape `GET /players/{id}/batter-stats-at-date` returns.
/// snake_case mirrors the wire format (the shared decoder doesn't
/// apply `convertFromSnakeCase`).
private struct BatterStatsAtDateResponse: Decodable {
    let player_id:      Int
    let game_date:      String
    let home_runs:      Int
    let doubles:        Int
    let triples:        Int
    let includes_today: Bool
}

/// Response from `GET /teams/{team_id}/postseason` — every series
/// involving any historical teamID under the franchise (winner or
/// loser side), sorted year desc / round.
struct TeamPostseasonResponse: Codable {
    let team_id: String
    let postseason: [TeamPostseasonSeries]
}

/// One row of postseason-series outcome. Round names are already in
/// display form ("World Series", "ALDS", "AL Wild Card", …) — the
/// backend's `_POSTSEASON_ROUND_DISPLAY` map normalizes Lahman's
/// ALDS1/ALDS2/etc. variants down to their bare round name.
struct TeamPostseasonSeries: Codable, Identifiable, Hashable {
    var id: String { "\(year)-\(round)" }
    let year: Int
    let round: String
    let won: Bool
    let opponent: String
    let wins: Int
    let losses: Int
}

/// Response from `GET /teams/{team_id}/awards` — every major-award
/// winner across the franchise's history, grouped by award type and
/// sorted year-desc within each group.
struct TeamAwardsResponse: Codable {
    let team_id: String
    let awards: [TeamAwardGroup]
}

/// One award type ("MVP", "CY Young", "Rookie of the Year",
/// "Gold Glove", "Silver Slugger") and its franchise winners.
struct TeamAwardGroup: Codable, Identifiable {
    var id: String { award }
    let award: String
    let winners: [TeamAwardWinner]
}

/// One franchise award winner. `player_id` is the MLBAM id — resolve
/// it to a `PlayerSearchResult` via `getPlayerByMlbId` to push the
/// player profile on tap. `league` is nil for pre-1969 single-league
/// votes.
struct TeamAwardWinner: Codable, Identifiable {
    var id: String { "\(year)-\(player_id)" }
    let year: Int
    let player_id: Int
    let name: String
    let league: String?
}
