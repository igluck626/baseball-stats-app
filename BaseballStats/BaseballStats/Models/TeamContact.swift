//
//  TeamContact.swift
//  BaseballStats
//
//  Each team's AVG, expected batting average (xBA) and balls hit 95+ mph in one
//  game — computed by the backend (`team_contact.py`), never here, and shipped
//  on the live snapshot (`team_contact`) and by `GET /games/{bdl_id}/team-contact`
//  for a finished game. The server also decides whether it's worth showing:
//  enough of each side's contact tracked, and, live, enough at-bats.
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
    /// The server's decision. Rendered only when true.
    let show: Bool
    /// Why it's hidden: "no_data", "untracked", "too_early".
    let reason: String?

    /// Whether the block renders: the server said so AND the numbers it needs
    /// are there. A `show` with a missing number would draw a blank, so it
    /// doesn't render either.
    var isDisplayable: Bool {
        show && away.avg != nil && away.xba != nil && home.avg != nil && home.xba != nil
    }

    /// ".222", "1.000" — batting-average style, rounded once, here.
    static func rate(_ value: Double?) -> String {
        guard let value else { return "—" }
        let s = String(format: "%.3f", value)
        return s.hasPrefix("0") ? String(s.dropFirst()) : s
    }
}
