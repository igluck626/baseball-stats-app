//
//  SeasonTeams.swift
//  BaseballStats
//
//  A traded season's clubs, from `GET /players/{id}/season-teams`: the career
//  table reads its season row as "All teams" and lists each club under it.
//
//  ⚠️ A CLUB VALUE IS SENT ONLY WHERE IT ADDS UP to the season row (the
//  per-team split rule: Retrosheet if exact, else Lahman if exact). Every
//  other value is null and shows "—" — a column a source never recorded (PA
//  and CS in Lahman's 1902), or a whole season with no exact split. Rates are
//  computed from the club's own counts and are "—" when any count they need
//  is. WAR, OPS+ and ERA+ are stored per season only: always "—" on a club.
//

import Foundation

struct SeasonTeamsResponse: Codable, Hashable {
    let player_id: Int
    let batting: [TradedSeason]
    let pitching: [TradedSeason]

    var battingByYear: [Int: TradedSeason] { Dictionary(batting.map { ($0.year, $0) }, uniquingKeysWith: { a, _ in a }) }
    var pitchingByYear: [Int: TradedSeason] { Dictionary(pitching.map { ($0.year, $0) }, uniquingKeysWith: { a, _ in a }) }
}

struct TradedSeason: Codable, Hashable {
    let year: Int
    /// "retrosheet", "lahman", "nightly" (the current season, from the game
    /// logs) — or nil: no exact split, the clubs alone with every value null.
    let source: String?
    let teams: [ClubLine]
}

struct ClubLine: Codable, Hashable {
    let team: String
    let stats: [String: Double?]

    func int(_ key: String) -> Int? { (stats[key] ?? nil).map { Int($0.rounded()) } }
    func double(_ key: String) -> Double? { stats[key] ?? nil }

    // MARK: batting, from this club's counts

    var avg: Double? {
        guard let h = int("H"), let ab = int("AB"), ab > 0 else { return nil }
        return Double(h) / Double(ab)
    }

    var tb: Int? {
        guard let h = int("H"), let d = int("doubles"), let t = int("triples"), let hr = int("HR") else { return nil }
        return h + d + 2 * t + 3 * hr
    }

    var obp: Double? {
        guard let h = int("H"), let ab = int("AB"), let bb = int("BB"),
              let hbp = int("HBP"), let sf = int("SF") else { return nil }
        let den = ab + bb + hbp + sf
        return den > 0 ? Double(h + bb + hbp) / Double(den) : nil
    }

    var slg: Double? {
        guard let tb, let ab = int("AB"), ab > 0 else { return nil }
        return Double(tb) / Double(ab)
    }

    var ops: Double? {
        guard let obp, let slg else { return nil }
        return obp + slg
    }

    var kPct: Double? {
        guard let so = int("SO"), let pa = int("PA"), pa > 0 else { return nil }
        return Double(so) / Double(pa)
    }

    var bbPct: Double? {
        guard let bb = int("BB"), let pa = int("PA"), pa > 0 else { return nil }
        return Double(bb) / Double(pa)
    }

    /// Standard linear weights (uBB = BB − IBB), as the career row computes it.
    var woba: Double? {
        guard let h = int("H"), let ab = int("AB"), let bb = int("BB"), let ibb = int("IBB"),
              let hbp = int("HBP"), let sf = int("SF"), let d = int("doubles"),
              let t = int("triples"), let hr = int("HR") else { return nil }
        let ubb = bb - ibb
        let den = ab + ubb + sf + hbp
        guard den > 0 else { return nil }
        let singles = h - d - t - hr
        let num = 0.69 * Double(ubb) + 0.72 * Double(hbp) + 0.89 * Double(singles)
            + 1.27 * Double(d) + 1.62 * Double(t) + 2.10 * Double(hr)
        return num / Double(den)
    }

    // MARK: pitching, from this club's counts

    private func perNine(_ key: String) -> Double? {
        guard let v = int(key), let ip = double("IP"), ip > 0 else { return nil }
        return Double(v) * 9 / ip
    }

    var era: Double? { perNine("ER") }
    var hPer9: Double? { perNine("H") }
    var hrPer9: Double? { perNine("HR") }
    var bbPer9: Double? { perNine("BB") }
    var soPer9: Double? { perNine("SO") }

    var whip: Double? {
        guard let h = int("H"), let bb = int("BB"), let ip = double("IP"), ip > 0 else { return nil }
        return Double(h + bb) / ip
    }

    var soBB: Double? {
        guard let so = int("SO"), let bb = int("BB"), bb > 0 else { return nil }
        return Double(so) / Double(bb)
    }

    /// FIP with the career row's 3.10 constant.
    var fip: Double? {
        guard let hr = int("HR"), let bb = int("BB"), let hbp = int("HBP"), let so = int("SO"),
              let ip = double("IP"), ip > 0 else { return nil }
        return (13 * Double(hr) + 3 * Double(bb + hbp) - 2 * Double(so)) / ip + 3.10
    }

    var winPct: Double? {
        guard let w = int("W"), let l = int("L"), w + l > 0 else { return nil }
        return Double(w) / Double(w + l)
    }
}
