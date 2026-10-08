//
//  TeamContact.swift
//  BaseballStats
//
//  Each team's AVG, expected batting average (xBA) and balls hit 95+ mph in one
//  game — computed by the backend (`team_contact.py`), never here, and shipped
//  on the live snapshot (`team_contact`) and by `GET /games/{bdl_id}/team-contact`
//  for a finished game. The server also decides whether it's worth showing:
//  enough of each side's contact tracked, and, live, an at-bat for each side.
//

import Foundation

struct TeamContact: Codable, Hashable {
    struct Side: Codable, Hashable {
        let ab: Int
        let h: Int
        let avg: Double?
        let xba: Double?
        let hardHit: Int
        /// The share of this side's balls in play that carry an xBA. Optional
        /// with the other diagnostics: the display decision is the server's.
        let trackedShare: Double?

        enum CodingKeys: String, CodingKey {
            case ab, h, avg, xba
            case hardHit = "hard_hit"
            case trackedShare = "tracked_share"
        }
    }

    let away: Side
    let home: Side
    /// The server's decision on the contact numbers (xBA's row). Rendered only
    /// when true.
    let show: Bool
    /// Why it's hidden: "no_data", "untracked".
    let reason: String?
    /// The Team Stats rows. Absent from an older server, hence optional — the
    /// card then doesn't render.
    let stats: TeamStats?

    /// ".222", "1.000" — batting-average style, rounded once, here. Also used by
    /// `TeamStats`.
    static func rate(_ value: Double?) -> String {
        guard let value else { return "—" }
        let s = String(format: "%.3f", value)
        return s.hasPrefix("0") ? String(s.dropFirst()) : s
    }
}


/// Team Stats for one game, computed by the backend (`team_stats.py`). The server
/// decides which rows show, and in what order (`rows`); a row it holds back —
/// xBA before a side has batted, RISP when the play stream can't be read — is simply
/// not listed. Unknown row keys (a newer server) are skipped.
struct TeamStats: Codable, Hashable {
    struct Side: Codable, Hashable {
        let avg: Double?
        let xba: Double?
        let hardHit: Int?
        let hr: Int?
        let rispH: Int?
        let rispAb: Int?
        let lob: Int?
        let bb: Int?
        let so: Int?
        let sb: Int?
        let dp: Int?
        let pitches: Int?

        enum CodingKeys: String, CodingKey {
            case avg, xba, hr, lob, bb, so, sb, dp, pitches
            case hardHit = "hard_hit"
            case rispH = "risp_h"
            case rispAb = "risp_ab"
        }
    }

    let rows: [String]
    let away: Side
    let home: Side

    /// One displayed row: its label and each side's value.
    struct Row: Hashable, Identifiable {
        let key: String
        let label: String
        let away: String
        let home: String
        var id: String { key }
    }

    static let labels: [String: String] = [
        "avg": "AVG", "xba": "xBA", "hard_hit": "Hit 95+ mph", "hr": "HR",
        "risp": "RISP", "lob": "LOB", "bb": "BB", "so": "SO", "sb": "SB",
        "dp": "Double plays", "pitches": "Pitches",
    ]

    /// The rows to draw, in the server's order. A row whose value is missing for
    /// either side is skipped rather than drawn with a blank.
    var displayRows: [Row] {
        rows.compactMap { key in
            guard let label = Self.labels[key],
                  let a = Self.value(key, away), let h = Self.value(key, home) else { return nil }
            return Row(key: key, label: label, away: a, home: h)
        }
    }

    static func value(_ key: String, _ s: Side) -> String? {
        func n(_ v: Int?) -> String? { v.map(String.init) }
        switch key {
        case "avg": return s.avg.map { TeamContact.rate($0) }
        case "xba": return s.xba.map { TeamContact.rate($0) }
        case "hard_hit": return n(s.hardHit)
        case "hr": return n(s.hr)
        case "risp":
            guard let h = s.rispH, let ab = s.rispAb else { return nil }
            return "\(h)-for-\(ab)"
        case "lob": return n(s.lob)
        case "bb": return n(s.bb)
        case "so": return n(s.so)
        case "sb": return n(s.sb)
        case "dp": return n(s.dp)
        case "pitches": return n(s.pitches)
        default: return nil
        }
    }
}
