//
//  BallDontLieClient.swift
//  BaseballStats
//
//  Direct client for the BallDontLie MLB API — talked to from iOS
//  without going through our Railway backend. Sole external stats
//  data source for the app (the prior MLB Stats API client was
//  removed for App Store compliance — every scores / box / live /
//  profile-overlay surface now routes through BDL).
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

    // MARK: - In-process TTL cache
    //
    // Multiple views in the same app session ask for the same
    // BDL data (Scores tab + Box Score + player profile overlay
    // all converge on today's games). A short-lived per-process
    // cache collapses those repeats to one HTTP round trip per
    // TTL window. Reset by `clearCache()` on pull-to-refresh and
    // by app relaunch (the cache is in-memory).
    //
    // Decoded values are stored (not raw Data) so the `getGameStats`
    // live-vs-final TTL decision can read the response without a
    // re-decode round trip. Type erasure via `Any` keeps the
    // dictionary single-typed; readers cast back to their concrete
    // generic type at the call site.

    private struct CacheEntry {
        let value: Any
        let expiresAt: Date
        var isValid: Bool { Date() < expiresAt }
    }

    private var cache: [String: CacheEntry] = [:]
    private let cacheLock = NSLock()

    private func cachedValue<T>(_ key: String) -> T? {
        cacheLock.lock(); defer { cacheLock.unlock() }
        guard let entry = cache[key] else { return nil }
        if !entry.isValid {
            cache.removeValue(forKey: key)
            return nil
        }
        return entry.value as? T
    }

    private func storeInCache(_ key: String, _ value: Any, ttl: TimeInterval) {
        cacheLock.lock(); defer { cacheLock.unlock() }
        cache[key] = CacheEntry(
            value: value,
            expiresAt: Date().addingTimeInterval(ttl),
        )
    }

    /// Drop every cached entry. Called by ScoresView's pull-to-
    /// refresh so a deliberate user-initiated refresh bypasses
    /// any stale-window cached values.
    func clearCache() {
        cacheLock.lock(); defer { cacheLock.unlock() }
        cache.removeAll()
    }

    // MARK: - Games

    /// Games for one local date. `date` is `yyyy-MM-dd` in the
    /// user's local calendar — but BDL buckets games by UTC start
    /// time, not local date. A 7pm-ET game on Tuesday starts at
    /// 23:00 UTC Tuesday (BDL bucket: Tuesday); a 7pm-PT game on
    /// Tuesday starts at 02:00 UTC Wednesday (BDL bucket:
    /// Wednesday). To return the MLB "Tuesday slate" we have to
    /// query THREE UTC buckets and filter client-side by Eastern
    /// local date (MLB schedules off ET).
    func getGames(date: String) async throws -> [BDLGame] {
        let key = "games:\(date)"
        if let cached: [BDLGame] = cachedValue(key) { return cached }

        // Compute the ±1-day envelope around the requested date.
        // We over-fetch by two UTC days so no edge case (early
        // morning local opening on the next-day's slate, very
        // late finals on the previous slate) can leak through.
        let neighbors = Self.neighborDates(of: date)
        var items: [URLQueryItem] = neighbors.map {
            URLQueryItem(name: "dates[]", value: $0)
        }
        items.append(URLQueryItem(name: "per_page", value: "100"))

        let envelope: BDLDataEnvelope<BDLGame> = try await fetch(path: "/mlb/v1/games", query: items)

        // Client-side filter: keep only games whose Eastern-local
        // start date matches the requested date. Dedupe by id in
        // case BDL returns the same game under multiple buckets
        // (shouldn't happen, but the union of three queries makes
        // belt-and-suspenders cheap).
        var seen: Set<Int> = []
        var filtered: [BDLGame] = []
        for g in envelope.data {
            if seen.contains(g.id) { continue }
            seen.insert(g.id)
            if Self.easternDateString(for: g) == date {
                filtered.append(g)
            }
        }
        storeInCache(key, filtered, ttl: 30)
        return filtered
    }

    /// `yyyy-MM-dd` for the day BEFORE and AFTER the input plus
    /// the input itself, for the BDL multi-bucket query above.
    /// Static + ISO-8601-shaped so the parse is unambiguous.
    private static func neighborDates(of yyyymmdd: String) -> [String] {
        let fmt = DateFormatter()
        fmt.calendar = .init(identifier: .gregorian)
        fmt.timeZone = .init(identifier: "UTC") ?? .current
        fmt.locale   = .init(identifier: "en_US_POSIX")
        fmt.dateFormat = "yyyy-MM-dd"
        guard let day = fmt.date(from: yyyymmdd) else { return [yyyymmdd] }
        let prev = day.addingTimeInterval(-86_400)
        let next = day.addingTimeInterval( 86_400)
        return [fmt.string(from: prev), yyyymmdd, fmt.string(from: next)]
    }

    /// Convert a BDL game's UTC start time to the date portion in
    /// US Eastern time, which is what MLB's schedule uses to
    /// assign a game to a calendar day. Falls back to the raw
    /// string's date prefix if parsing fails (shouldn't, but
    /// keeps the filter from dropping rows on a quirky payload).
    private static let easternFormatter: DateFormatter = {
        let f = DateFormatter()
        f.calendar = .init(identifier: .gregorian)
        f.timeZone = TimeZone(identifier: "America/New_York") ?? .current
        f.locale   = .init(identifier: "en_US_POSIX")
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()

    private static func easternDateString(for game: BDLGame) -> String? {
        guard let parsed = game.startDate else {
            // BDL response was unparseable — fall back to the
            // UTC date prefix so the game still surfaces somewhere.
            return String(game.date.prefix(10))
        }
        return easternFormatter.string(from: parsed)
    }

    /// Team-scoped schedule for one date. Used by the player-profile
    /// live-stats overlay — answers "does my team play today?"
    /// without pulling the full 15-game daily slate.
    func getTeamGames(date: String, teamId: Int) async throws -> [BDLGame] {
        let key = "team_games:\(date):\(teamId)"
        if let cached: [BDLGame] = cachedValue(key) { return cached }
        let items: [URLQueryItem] = [
            URLQueryItem(name: "dates[]",    value: date),
            URLQueryItem(name: "team_ids[]", value: String(teamId)),
            URLQueryItem(name: "per_page",   value: "100"),
        ]
        let envelope: BDLDataEnvelope<BDLGame> = try await fetch(path: "/mlb/v1/games", query: items)
        storeInCache(key, envelope.data, ttl: 30)
        return envelope.data
    }

    // MARK: - Box score

    /// Per-player batting + pitching lines for one game. Returns a
    /// flat list — caller groups by team via `BDLPlayerStat.team`
    /// (vs. the old client's team-nested response shape).
    func getGameStats(gameId: Int) async throws -> [BDLPlayerStat] {
        let key = "game_stats:\(gameId)"
        if let cached: [BDLPlayerStat] = cachedValue(key) { return cached }
        // BDL's /stats endpoint accepts game_ids[] (plural) per the
        // OpenAPI; the singular live-API quirk only applies to
        // /plays and /plate_appearances.
        let items: [URLQueryItem] = [
            URLQueryItem(name: "game_ids[]", value: String(gameId)),
            URLQueryItem(name: "per_page",   value: "100"),
        ]
        let envelope: BDLDataEnvelope<BDLPlayerStat> = try await fetch(
            path: "/mlb/v1/stats", query: items,
        )
        // Content-aware TTL: a recorded W/L/SV decision (non-null
        // wins/losses on any row) means the game's done. Final
        // games get a 5-minute cache; live games get 60s so the
        // box-score numbers stay current under the polling loop.
        let isFinal = envelope.data.contains { stat in
            (stat.wins ?? 0) > 0 || (stat.losses ?? 0) > 0 || (stat.saves ?? 0) > 0
        }
        storeInCache(key, envelope.data, ttl: isFinal ? 300 : 60)
        return envelope.data
    }

    /// Starting lineup for one game — batting order + position +
    /// probable-pitcher flag. Used by the box-score view to render
    /// players in lineup order rather than the arbitrary order
    /// `/stats` returns them.
    func getGameLineup(gameId: Int) async throws -> [BDLGameLineup] {
        // Diagnostic: confirms the new game_ids[]-using code path
        // is the one running in the deployed bundle. Remove once
        // the lineup data confirms working in the box score UI.
        print("getGameLineup using game_ids[]: \(gameId)")
        // Cache temporarily disabled while we verify the
        // `game_ids[]` plural-form fix lands cleanly. Once a
        // post-deploy session confirms the lineup data is correct,
        // re-enable the cached-read + storeInCache around this
        // body. (Even disabled, the cache is in-memory only and
        // would clear at app relaunch — so a "stale lineup
        // surviving across install" symptom shouldn't be possible
        // by construction. The disable is belt-and-suspenders.)
        // let key = "game_lineup:\(gameId)"
        // if let cached: [BDLGameLineup] = cachedValue(key) { return cached }
        // BDL's /lineups endpoint silently ignores `game_id`
        // (singular) and paginates the global lineup firehose —
        // exact same trap as `/stats` had with the same param
        // name. The plural array form `game_ids[]` is the one
        // that actually filters. Verified by curl: singular
        // returned 50 entries for completely different games;
        // plural returned 20 entries (10 + 10) for the requested
        // game's two teams.
        let items: [URLQueryItem] = [
            URLQueryItem(name: "game_ids[]", value: String(gameId)),
            URLQueryItem(name: "per_page",   value: "100"),
        ]
        let envelope: BDLDataEnvelope<BDLGameLineup> = try await fetch(
            path: "/mlb/v1/lineups", query: items,
        )
        // Lineups are set before first pitch and don't change
        // mid-game (pinch hitters/runners show up in /stats and
        // /plays, not /lineups). Long TTL is safe.
        // storeInCache(key, envelope.data, ttl: 300)
        return envelope.data
    }

    // MARK: - Live game

    /// Sequential play log for one game. Pagination is via cursor;
    /// the live card only needs the latest few, but a full pull is
    /// supported by walking `next_cursor` if a future caller wants
    /// the whole stream. Returns plays in the order BDL ships them
    /// (chronological — earliest first; latest is at the tail).
    func getPlays(gameId: Int) async throws -> [BDLPlay] {
        let key = "plays:\(gameId)"
        if let cached: [BDLPlay] = cachedValue(key) { return cached }
        let items: [URLQueryItem] = [
            URLQueryItem(name: "game_id",  value: String(gameId)),
            URLQueryItem(name: "per_page", value: "100"),
        ]
        let result: [BDLPlay] = try await fetchAllPages(path: "/mlb/v1/plays", baseQuery: items)
        // Live polling loop fires every 30s; matching the cache
        // TTL means each user sees one network round-trip per poll
        // window even if multiple views subscribe to the same game.
        storeInCache(key, result, ttl: 30)
        return result
    }

    /// Per-PA log with base-runner state and pitch-by-pitch detail.
    /// The live card uses the latest entry's `runnerOn{First,Second,
    /// Third}` to render the diamond — `/plays` doesn't carry that.
    func getPlateAppearances(gameId: Int) async throws -> [BDLPlateAppearance] {
        let key = "pas:\(gameId)"
        if let cached: [BDLPlateAppearance] = cachedValue(key) { return cached }
        let items: [URLQueryItem] = [
            URLQueryItem(name: "game_id",  value: String(gameId)),
            URLQueryItem(name: "per_page", value: "100"),
        ]
        let result: [BDLPlateAppearance] = try await fetchAllPages(
            path: "/mlb/v1/plate_appearances", baseQuery: items,
        )
        storeInCache(key, result, ttl: 30)
        return result
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
