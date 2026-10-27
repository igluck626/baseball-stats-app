//
//  TeamSeasonStats.swift
//  BaseballStats
//
//  GET /teams/season-stats — every club's season totals with MLB and league
//  ranks, regular season only, for the team page's Season Stats card. The
//  server ranks everything, Run differential included (from its standings);
//  the app only displays.
//

import Foundation

struct TeamSeasonStatsResponse: Decodable {
    let season: Int
    let stale: Bool?
    let teams: [Team]

    struct Team: Decodable {
        let team: String
        let bdlId: Int?
        let league: String?
        let stats: [String: Stat]
    }

    struct Stat: Decodable, Equatable {
        let label: String
        let value: Double?
        let mlbRank: Int?
        let mlbTied: Bool?
    }
}

/// What the card shows, in order, and how each value and rank reads.
enum SeasonStatsFormat {
    /// Run differential, on its own row above Batting and Pitching.
    static let overall = ["RD"]
    static let batting = ["R", "AVG", "OBP", "SLG", "OPS", "HR", "SB"]
    static let pitching = ["RA", "ERA", "WHIP", "SO", "SV", "HRA"]

    /// "+138", "−192", ".235", "3.24", "1,140"; "—" when the server has no value.
    static func value(_ key: String, _ v: Double?) -> String {
        guard let v else { return "—" }
        switch key {
        case "RD":
            let n = Int(v.rounded())
            return n > 0 ? "+" + StatNumber.grouped(n) : (n < 0 ? "−" + StatNumber.grouped(-n) : "0")
        case "ERA", "WHIP":
            return String(format: "%.2f", v)
        case "AVG", "OBP", "SLG", "OPS":
            let s = String(format: "%.3f", v)
            return s.hasPrefix("0.") ? String(s.dropFirst()) : s
        default:
            return StatNumber.grouped(Int(v.rounded()))
        }
    }

    /// "4th", "T-9th"; "—" without a rank.
    static func rank(_ s: TeamSeasonStatsResponse.Stat) -> String {
        guard let r = s.mlbRank else { return "—" }
        return (s.mlbTied == true ? "T-" : "") + ordinal(r)
    }

    /// What VoiceOver reads after the label and value.
    static func spokenRank(_ s: TeamSeasonStatsResponse.Stat) -> String {
        guard let r = s.mlbRank else { return "no rank" }
        return (s.mlbTied == true ? "tied " : "") + "\(ordinal(r)) of 30 in MLB"
    }

    static func ordinal(_ n: Int) -> String {
        let m = n % 100
        if (11...13).contains(m) { return "\(n)th" }
        switch n % 10 {
        case 1: return "\(n)st"
        case 2: return "\(n)nd"
        case 3: return "\(n)rd"
        default: return "\(n)th"
        }
    }
}
