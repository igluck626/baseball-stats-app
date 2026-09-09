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
    /// Which board an entry belongs to. The card needs this to know
    /// whether `detail` is a pitch type (overridable from the play
    /// stream, which names pitches differently) or a plate-appearance
    /// outcome (which only the PA feed carries).
    enum Kind { case hit, pitch }

    struct Entry: Identifiable, Equatable {
        let kind: Kind
        /// Player, plate appearance and pitch — enough to stay unique
        /// now that a man may hold several rows. `playerId + value`
        /// was sufficient only while the board was deduped; two of
        /// Halvorsen's seven top-ten pitches could share a reading.
        var id: String { "\(playerId)-\(pa.inning)-\(pa.paNumber)-\(pitchIndex)" }
        let playerId: Int
        let name: String
        /// BDL team id, for the by-team grouping.
        let teamId: Int
        let value: Double
        /// What the number amounted to — "Double" for a batted ball,
        /// "4-Seam Fastball" for a pitch. A bare velocity says less
        /// than it looks like it does.
        let detail: String?
        /// The plate appearance this measurement came from, carried so
        /// a tapped row can open that at-bat's detail sheet.
        let pa: BDLPlateAppearance
        /// Which pitch of that plate appearance — so the sheet can mark
        /// the one the row is about. A row for the ninth-fastest pitch
        /// opening a sheet where that pitch isn't picked out would
        /// leave the reader to find it by eye.
        let pitchIndex: Int
    }

    let hardestHit: [Entry]
    let fastestPitches: [Entry]

    var isEmpty: Bool { hardestHit.isEmpty && fastestPitches.isEmpty }

    /// The best `limit` events, best-first. A player may hold several
    /// rows.
    ///
    /// ⚠️ NOT deduped by player, deliberately, and this reverses an
    /// earlier call. The argument for deduping was that one man
    /// filling the board tells the reader nothing after the first row.
    /// That is wrong: on game 5059936 Seth Halvorsen threw SEVEN of the
    /// ten fastest pitches, and only three pitchers appear in the top
    /// ten at all. That concentration IS the finding — a reliever came
    /// in and threw the hardest of anyone, repeatedly — and deduping
    /// hid it behind three tidy rows.
    private static func rank(_ all: [Entry], limit: Int) -> [Entry] {
        Array(all.sorted { $0.value > $1.value }.prefix(limit))
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
        limit: Int = 10,
        nameAndTeam: (Int) -> (name: String, teamId: Int)?,
    ) -> GameLeaders? {
        var hits: [Entry] = []
        var pitches: [Entry] = []

        for pa in plateAppearances {
            guard let rows = pa.pitches, !rows.isEmpty else { continue }

            // Hardest hit: one row per ball put in play. A plate
            // appearance yields at most one, so this is per-PA by
            // nature rather than by choice.
            if let batter = pa.batterId, let who = nameAndTeam(batter) {
                for (i, pitch) in rows.enumerated() {
                    guard let ev = pitch.exitVelocity else { continue }
                    hits.append(Entry(
                        kind: .hit, playerId: batter, name: who.name, teamId: who.teamId,
                        value: ev, detail: pa.result, pa: pa, pitchIndex: i,
                    ))
                }
            }

            // Fastest pitch: one row per PITCH, not per plate
            // appearance. Taking each PA's fastest and ranking those
            // would answer a different question — it caps a pitcher at
            // one row per batter faced, so a reliever who threw the
            // three hardest pitches of the game to the same man would
            // show once. Release speed, not plate speed; see
            // `BDLPitchDetail.releaseSpeed`.
            if let pitcher = pa.pitcherId, let who = nameAndTeam(pitcher) {
                for (i, pitch) in rows.enumerated() {
                    guard let speed = pitch.releaseSpeed else { continue }
                    pitches.append(Entry(
                        kind: .pitch, playerId: pitcher, name: who.name, teamId: who.teamId,
                        value: speed, detail: pitch.pitchType, pa: pa, pitchIndex: i,
                    ))
                }
            }
        }

        let leaders = GameLeaders(
            hardestHit:     rank(hits, limit: limit),
            fastestPitches: rank(pitches, limit: limit),
        )
        return leaders.isEmpty ? nil : leaders
    }

    /// BDL's outcome sentence for each plate appearance, keyed so a
    /// leaders row can find its own.
    ///
    /// The board is built from `/plate_appearances`, which carries the
    /// outcome as a NOUN ("Double") but not as prose. The sentence
    /// lives on the play stream. Joined by `InningJoin` — see that type
    /// for why it is not an index.
    ///
    /// A PA that finds no sentence simply gets none, and its sheet
    /// opens with the noun alone.
    static func sentences(
        plays: [BDLPlay], plateAppearances: [BDLPlateAppearance],
    ) -> [String: String] {
        // Only result rows naming a batter: the others are steals,
        // relief changes and defensive changes, which belong to no
        // plate appearance and would consume another batter's slot.
        var join = InningJoin<BDLPlay>(
            plays.filter { $0.type == "Play Result" && $0.batterId != nil },
            key: { p in
                p.batterId.map {
                    InningKey(inning: p.inning, half: p.inningType, batterId: $0)
                }
            },
            order: \.order,
        )
        var out: [String: String] = [:]
        for pa in orderedPAs(plateAppearances) {
            guard let b = pa.batterId else { continue }
            let key = InningKey(inning: pa.inning, half: pa.halfInning, batterId: b)
            if let row = join.next(key), let t = row.text { out[paKey(pa)] = t }
        }
        return out
    }

    /// The play stream's pitch rows for each plate appearance, keyed the
    /// same way.
    ///
    /// Why bother, when the PA feed carries its own pitches: the play
    /// row's `type` is the RICHER vocabulary. It distinguishes
    /// "Strike Swinging" from "Strike Looking", which the plays list
    /// renders as "Swinging Strike" and "Called Strike", where the PA
    /// feed's `call_name` flattens both to "Strike". Both routes into
    /// the detail sheet should say the same thing, and the richer one
    /// is the one worth matching.
    ///
    /// ⚠️ Returned ONLY where the two feeds agree on pitch count, since
    /// the caller indexes into this list to mark a pitch and a length
    /// mismatch would mark the wrong one. On game 5059936 that is 66 of
    /// 69 plate appearances; the 3 exceptions are the out-of-order
    /// ball-in-play rows that also displaced the plays list's headline
    /// (a row arriving after the NEXT batter's marker). Those fall back
    /// to the PA feed's own coarser pitches, which is what both routes
    /// showed before this existed.
    static func pitchRows(
        plays: [BDLPlay], plateAppearances: [BDLPlateAppearance],
    ) -> [String: [BDLPlay]] {
        // Group the stream's pitch rows into at-bats, split on the
        // batter markers, keeping each at-bat's key.
        var groups: [(key: InningKey, order: Int, rows: [BDLPlay])] = []
        for p in plays.sorted(by: { $0.order < $1.order }) {
            if p.type == "Start Batter/Pitcher" {
                guard let b = p.batterId else { continue }
                groups.append((
                    InningKey(inning: p.inning, half: p.inningType, batterId: b),
                    p.order, [],
                ))
            } else if (p.text ?? "").hasPrefix("Pitch "), !groups.isEmpty {
                groups[groups.count - 1].rows.append(p)
            }
        }

        var join = InningJoin(groups, key: { $0.key }, order: { $0.order })
        var out: [String: [BDLPlay]] = [:]
        for pa in orderedPAs(plateAppearances) {
            guard let b = pa.batterId else { continue }
            let key = InningKey(inning: pa.inning, half: pa.halfInning, batterId: b)
            guard let g = join.next(key) else { continue }
            guard g.rows.count == (pa.pitches?.count ?? 0) else { continue }
            out[paKey(pa)] = g.rows
        }
        return out
    }

    /// Both joins must walk plate appearances in the same order the
    /// play stream runs, or the queues desynchronise.
    private static func orderedPAs(_ pas: [BDLPlateAppearance]) -> [BDLPlateAppearance] {
        pas.sorted { ($0.inning, $0.paNumber) < ($1.inning, $1.paNumber) }
    }

    /// Stable identity for one plate appearance, for the sentence map.
    static func paKey(_ pa: BDLPlateAppearance) -> String {
        "\(pa.inning)-\(pa.halfInning ?? "")-\(pa.paNumber)"
    }

    /// The same entries split by side, for the by-team grouping. The
    /// per-side lists are re-ranked from the FULL set rather than
    /// filtered from the overall top three — otherwise a side whose
    /// hitters all placed fourth and below would show nothing at all.
    static func byTeam(
        plateAppearances: [BDLPlateAppearance],
        awayTeamId: Int,
        homeTeamId: Int,
        limit: Int = 10,
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
