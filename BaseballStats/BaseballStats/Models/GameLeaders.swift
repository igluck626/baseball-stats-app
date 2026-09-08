//
//  GameLeaders.swift
//  BaseballStats
//
//  The game's hardest-hit balls and fastest pitches, derived from the
//  plate-appearance feed the box score already fetches. Pure — no
//  fetching, no view types — so the ranking can be reasoned about and
//  tested apart from how it's drawn.
//
//  ⚠️ FLOOR: 2015. Games before that carry NO plate appearances at all
//  — not merely absent Statcast fields, an empty feed (measured on
//  2008-06-15 and 2014-06-15, both zero PAs; 2015-06-15 returned 84 PAs
//  and 341 tracked pitches). So `build` returns nil for an older game
//  and the section is absent rather than empty.
//

import Foundation

struct GameLeaders: Equatable {
    struct Entry: Identifiable, Equatable {
        /// `playerId` + the measurement, so a redraw keeps SwiftUI's
        /// diffing stable while a live game adds rows.
        var id: String { "\(playerId)-\(value)" }
        let playerId: Int
        let name: String
        /// BDL team id, for the by-team grouping.
        let teamId: Int
        let value: Double
        /// What the number amounted to — "Double" for a batted ball,
        /// "4-Seam Fastball" for a pitch. A bare velocity says less
        /// than it looks like it does.
        let detail: String?
    }

    let hardestHit: [Entry]
    let fastestPitches: [Entry]

    var isEmpty: Bool { hardestHit.isEmpty && fastestPitches.isEmpty }

    /// One entry per player, best-first, capped at `limit`.
    ///
    /// ⚠️ DEDUPED BY PLAYER deliberately. Ranking raw events instead
    /// lets one man own the whole list — a starter who touches 100
    /// four times would fill every row with the same name, which is
    /// true and tells the reader nothing they didn't learn from the
    /// first row. Deduping turns "the four fastest pitches" into "the
    /// hardest throwers", which is the question a reader of a box
    /// score is actually asking. To rank events instead, drop the
    /// `seen` check — nothing else depends on it.
    private static func rank(_ all: [Entry], limit: Int) -> [Entry] {
        var seen: Set<Int> = []
        var out: [Entry] = []
        for e in all.sorted(by: { $0.value > $1.value }) {
            guard !seen.contains(e.playerId) else { continue }
            seen.insert(e.playerId)
            out.append(e)
            if out.count == limit { break }
        }
        return out
    }

    /// Build from the two payloads the box score already holds.
    ///
    /// `nameAndTeam` resolves a BDL player id to a display name and the
    /// team id he appeared for — the caller supplies it from the
    /// `/stats` rows, which cover every id this feed mentions (21 of 21
    /// batters and 8 of 8 pitchers on game 5059936). An id that fails
    /// to resolve is dropped rather than shown as a blank row.
    ///
    /// Returns nil when the feed carries no tracked measurements at
    /// all, so the caller can omit the section entirely.
    static func build(
        plateAppearances: [BDLPlateAppearance],
        limit: Int = 3,
        nameAndTeam: (Int) -> (name: String, teamId: Int)?,
    ) -> GameLeaders? {
        var hits: [Entry] = []
        var pitches: [Entry] = []

        for pa in plateAppearances {
            guard let rows = pa.pitches, !rows.isEmpty else { continue }

            // Hardest hit: the ball this plate appearance put in play.
            // Taken from the pitch that carries an exit velocity rather
            // than from the last pitch — a foul tip after contact would
            // otherwise displace it.
            if let batter = pa.batterId,
               let who = nameAndTeam(batter),
               let best = rows.compactMap(\.exitVelocity).max() {
                hits.append(Entry(
                    playerId: batter, name: who.name, teamId: who.teamId,
                    value: best, detail: pa.result,
                ))
            }

            // Fastest pitch: the pitcher's quickest in this plate
            // appearance. Release speed, not plate speed — see
            // `BDLPitchDetail.releaseSpeed`.
            if let pitcher = pa.pitcherId,
               let who = nameAndTeam(pitcher),
               let fastest = rows.max(by: {
                   ($0.releaseSpeed ?? 0) < ($1.releaseSpeed ?? 0)
               }),
               let speed = fastest.releaseSpeed {
                pitches.append(Entry(
                    playerId: pitcher, name: who.name, teamId: who.teamId,
                    value: speed, detail: fastest.pitchType,
                ))
            }
        }

        let leaders = GameLeaders(
            hardestHit:     rank(hits, limit: limit),
            fastestPitches: rank(pitches, limit: limit),
        )
        return leaders.isEmpty ? nil : leaders
    }

    /// The same entries split by side, for the by-team grouping. The
    /// per-side lists are re-ranked from the FULL set rather than
    /// filtered from the overall top three — otherwise a side whose
    /// hitters all placed fourth and below would show nothing at all.
    static func byTeam(
        plateAppearances: [BDLPlateAppearance],
        awayTeamId: Int,
        homeTeamId: Int,
        limit: Int = 3,
        nameAndTeam: (Int) -> (name: String, teamId: Int)?,
    ) -> (away: GameLeaders?, home: GameLeaders?) {
        func side(_ teamId: Int) -> GameLeaders? {
            build(plateAppearances: plateAppearances, limit: limit) { pid in
                guard let who = nameAndTeam(pid), who.teamId == teamId else { return nil }
                return who
            }
        }
        return (side(awayTeamId), side(homeTeamId))
    }
}
