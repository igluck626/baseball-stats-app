//
//  News.swift
//  BaseballStats
//
//  Codable models for `GET /news` (team news ingested from MLB.com RSS).
//  Response shape: {"articles": [ { id, source_name, team_code, title,
//  summary, url, image_url, published_at }, ... ]}.
//
//  `summary` is usually null and `image_url` is almost always present, so the
//  UI is designed around an image-first card with an optional summary.
//

import Foundation

/// One news article. `published_at` arrives as an ISO-8601 UTC string; the
/// shared `JSONDecoder` has no date strategy, so we parse it here (keeping
/// the rest of the app's decoding untouched).
struct NewsArticle: Decodable, Identifiable, Hashable {
    let id: Int
    let sourceName: String
    let teamCode: String
    let title: String
    let summary: String?
    let url: String
    let imageUrl: String?
    let publishedAt: Date

    enum CodingKeys: String, CodingKey {
        case id, title, summary, url
        case sourceName  = "source_name"
        case teamCode    = "team_code"
        case imageUrl    = "image_url"
        case publishedAt = "published_at"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id         = try c.decode(Int.self, forKey: .id)
        sourceName = try c.decode(String.self, forKey: .sourceName)
        teamCode   = try c.decode(String.self, forKey: .teamCode)
        title      = try c.decode(String.self, forKey: .title)
        summary    = try c.decodeIfPresent(String.self, forKey: .summary)
        url        = try c.decode(String.self, forKey: .url)
        imageUrl   = try c.decodeIfPresent(String.self, forKey: .imageUrl)
        let raw    = try c.decode(String.self, forKey: .publishedAt)
        publishedAt = NewsArticle.parseDate(raw) ?? .distantPast
    }

    /// ISO-8601 with an offset ("2026-06-22T23:17:00+00:00"); falls back to a
    /// fractional-seconds parse for any source that adds milliseconds.
    private static func parseDate(_ s: String) -> Date? {
        Self.iso.date(from: s) ?? Self.isoFractional.date(from: s)
    }
    private static let iso: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()
    private static let isoFractional: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
}

/// Top-level `{"articles": [...]}` wrapper.
struct NewsResponse: Decodable {
    let articles: [NewsArticle]
}

extension NewsArticle {
    /// Collapse cross-team syndicated duplicates. The same wire story is
    /// stored once per team, so the league-wide feed surfaces it multiple
    /// times. Two articles are "the same" when their normalized title (cased
    /// down + whitespace-trimmed) and `publishedAt` match. Keeps the first
    /// occurrence, preserving the endpoint's newest-first ordering.
    static func deduplicated(_ articles: [NewsArticle]) -> [NewsArticle] {
        var seen = Set<String>()
        var out: [NewsArticle] = []
        for article in articles {
            let normalizedTitle = article.title
                .lowercased()
                .trimmingCharacters(in: .whitespacesAndNewlines)
            let key = "\(normalizedTitle)|\(article.publishedAt.timeIntervalSince1970)"
            if seen.insert(key).inserted {
                out.append(article)
            }
        }
        return out
    }
}
