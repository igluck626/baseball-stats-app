//
//  InningJoin.swift
//  BaseballStats
//
//  Pairing rows from BDL's `/plays` stream with rows from its
//  `/plate_appearances` feed.
//
//  ⚠️ THE KEY IS INNING + HALF + BATTER, CONSUMED IN ORDER — never a
//  list index. The two feeds do not line up positionally: `/plays`
//  carries rows the PA feed has no notion of (steals, pinch-hit and
//  defensive announcements, relief changes), and every one an index
//  join steps over shifts each later row onto the wrong plate
//  appearance. Measured on game 5059936, an index join hung 95.5 mph on
//  "De La Cruz stole second" and rendered an exit velocity on an
//  intentional walk.
//
//  A QUEUE per key rather than a single value, because a batter can bat
//  twice in one half-inning when the order turns over; the Nth item for
//  a key takes the Nth counterpart.
//
//  This exists because the same join was written three times — for the
//  plays list's contact metrics, for the leaders card's sentences, and
//  for its pitch vocabulary. Three copies of a rule this easy to get
//  subtly wrong is two too many.
//

import Foundation

/// Where a row sits in the game. Declared outside `InningJoin` so one
/// key type serves every instantiation — nested in the generic, a key
/// built for `InningJoin<BDLPlay>` would not typecheck against
/// `InningJoin<Group>`, and the two sides of a join are rarely the same
/// type.
struct InningKey: Hashable {
    let inning: Int
    /// Normalised "top" / "bottom". The two feeds disagree on case and
    /// wording — `/plays` ships "Top"/"Bottom" (and mid-inning marker
    /// variants), `/plate_appearances` ships "top"/"bottom".
    let half: String
    let batterId: Int

    init(inning: Int, half rawHalf: String?, batterId: Int) {
        self.inning = inning
        self.half = (rawHalf ?? "").lowercased().contains("bot") ? "bottom" : "top"
        self.batterId = batterId
    }
}

struct InningJoin<Value> {

    private var queues: [InningKey: [Value]] = [:]
    private var cursor: [InningKey: Int] = [:]

    /// Build the queues. `order` sorts within a key; pass whatever
    /// orders that feed (`BDLPlay.order`, or inning + `paNumber`).
    init(_ items: [Value], key: (Value) -> InningKey?, order: (Value) -> Int) {
        for item in items.sorted(by: { order($0) < order($1) }) {
            guard let k = key(item) else { continue }
            queues[k, default: []].append(item)
        }
    }

    /// The next unconsumed value for this key, or nil when the key has
    /// none left. Callers must request keys in the order their own rows
    /// appear, which is what keeps repeat plate appearances aligned.
    mutating func next(_ key: InningKey) -> Value? {
        let i = cursor[key, default: 0]
        guard let q = queues[key], i < q.count else { return nil }
        cursor[key] = i + 1
        return q[i]
    }
}
