//
//  UnknownRule.swift
//  BaseballStats
//
//  The server's "unknown" rules, mirrored on the device so a sum the app
//  builds itself never turns an unknown into a short number.
//
//  ⚠️ A NULL IS NOT A ZERO. When balldontlie ships a line without its detail
//  (no doubles, no HBP, no SB), the server stores NULL and shows "—" for any
//  total built on it. The app sums game logs and season rows on its own in a
//  few places (Game Logs totals, the career row, today's overlay); each one
//  used `?? 0`, which printed a short total with no sign anything was missing.
//
//  The rules, exactly as the server applies them:
//    • Game logs, batting — the ACTIVITY RULE: a NULL double, triple, HBP, SF,
//      IBB or PA on a game with AB or BB makes the total unknown; on a game
//      with neither it reads as 0. SB and CS are the exception: unknown on
//      ANY game, because a pinch runner's line has no AB and no BB and is
//      exactly where a steal happens.
//    • Game logs, pitching — every game is activity: any NULL is unknown.
//    • Careers — a total is unknown when a CURRENT-season row (a played one)
//      has the column NULL; a NULL in a PAST season is "not recorded" (SF
//      before 1954, say) and still reads as 0, as it always has. Only the
//      columns the server fills from the game logs can be unknown this way.
//

import Foundation

enum UnknownRule {

    // MARK: - Known arithmetic

    /// `a + b`, or nil when either is unknown.
    static func plus(_ a: Int?, _ b: Int?) -> Int? {
        guard let a, let b else { return nil }
        return a + b
    }

    // MARK: - Game logs

    /// Batting game-log fields whose NULL is unknown only on a game with AB or BB.
    static let battingActivityFields: Set<String> = ["doubles", "triples", "HBP", "SF", "IBB", "PA"]
    /// Batting game-log fields whose NULL is unknown on ANY game.
    static let battingAnyRowFields: Set<String> = ["SB", "CS"]

    /// True when this game's NULL in `field` makes a batting total unknown.
    static func battingUnknown(_ field: String, value: Int?, ab: Int?, bb: Int?) -> Bool {
        guard value == nil else { return false }
        if battingAnyRowFields.contains(field) { return true }
        guard battingActivityFields.contains(field) else { return false }
        return (ab ?? 0) > 0 || (bb ?? 0) > 0
    }

    /// Sum of one batting game-log field under the activity rule: nil when a
    /// game makes it unknown, otherwise the sum with a no-activity NULL as 0.
    static func battingSum(_ games: [GameLog], _ field: String, _ value: (GameLog) -> Int?) -> Int? {
        var total = 0
        for g in games {
            let v = value(g)
            if battingUnknown(field, value: v, ab: g.AB, bb: g.BB) { return nil }
            total += v ?? 0
        }
        return total
    }

    /// Sum of one pitching game-log field: nil when any game has it NULL.
    static func pitchingSum(_ games: [GameLog], _ value: (GameLog) -> Int?) -> Int? {
        var total = 0
        for g in games {
            guard let v = value(g) else { return nil }
            total += v
        }
        return total
    }

    /// A pitcher's W-L over games: nil when any game's decision is unknown
    /// (`result` NULL — balldontlie didn't ship the game's decisions).
    static func winLoss(_ games: [GameLog]) -> (w: Int, l: Int)? {
        guard !games.contains(where: { $0.result == nil }) else { return nil }
        return (games.filter { $0.result == "W" }.count, games.filter { $0.result == "L" }.count)
    }

    // MARK: - Decisions in a final

    /// Whether a FINAL game's balldontlie lines carry its decisions — the
    /// server's `_decisions_shipped`: some pitcher (a line with innings) has a
    /// win or a loss. balldontlie sends 0, not null, when it hasn't them, and a
    /// final always has a winner and a loser, so none at all means unshipped.
    static func decisionsShipped(_ lines: [BDLPlayerStat]) -> Bool {
        let pitchers = lines.filter { $0.ip != nil }
        return pitchers.isEmpty || pitchers.contains { ($0.wins ?? 0) > 0 || ($0.losses ?? 0) > 0 }
    }

    /// The decisions a pitcher's line from this game leaves unknown: W, L and
    /// SV for a final whose decisions weren't shipped; none for a live game
    /// (no decision yet is a real 0 until the final) or a final that has them.
    static func decisionUnknowns(gameLines: [BDLPlayerStat], isLive: Bool) -> Set<String> {
        (isLive || decisionsShipped(gameLines)) ? [] : ["W", "L", "SV"]
    }

    // MARK: - Careers

    /// Batting season columns the server fills from the game logs — the only
    /// ones a current season can leave unknown.
    static let battingSeasonUnknownable: Set<String> = ["PA", "HBP", "SF", "CS", "IBB", "GIDP", "SH"]
    /// Pitching season columns the server fills from the game logs.
    static let pitchingSeasonUnknownable: Set<String> = ["R", "HBP"]

    /// The server's current season: its calendar year.
    static var currentSeason: Int { Calendar(identifier: .gregorian).component(.year, from: Date()) }

    /// A career (or multi-season) sum: nil when a played row of the current
    /// season has the column unknown, otherwise the sum with a past NULL as 0.
    /// `unknownable` false means the column is never unknown — the plain sum.
    static func careerSum<S>(
        _ seasons: [S], unknownable: Bool, currentSeason: Int = UnknownRule.currentSeason,
        year: (S) -> Int?, played: (S) -> Bool, value: (S) -> Int?
    ) -> Int? {
        var total = 0
        for s in seasons {
            if let v = value(s) { total += v; continue }
            if unknownable, (year(s) ?? 0) >= currentSeason, played(s) { return nil }
        }
        return total
    }
}

// MARK: - Point-in-time records (box score, game card)

extension PitcherRecord {
    /// The record a decision line shows, `bump` added for a game the log
    /// hasn't absorbed: "12-6" for W or L, "31" for SV — or "—" when the
    /// server flags the count unknown (a game whose decisions balldontlie
    /// didn't ship). nil for any other tag.
    func figure(for tag: String, bump: Int) -> String? {
        switch tag {
        case "W":
            return (winsUnknown || lossesUnknown) ? "—" : "\(wins + bump)-\(losses)"
        case "L":
            return (winsUnknown || lossesUnknown) ? "—" : "\(wins)-\(losses + bump)"
        case "SV":
            return savesUnknown ? "—" : "\(saves + bump)"
        default:
            return nil
        }
    }
}

extension BatterStatsAtDate {
    enum Total { case homeRuns, doubles, triples }

    /// The season total a notable-plays line shows, `bump` added for a game
    /// the log hasn't absorbed — or "—" when the server flags it unknown (a
    /// NULL double or triple on a game with AB or BB; HR is never unknown).
    func figure(_ total: Total, bump: Int) -> String {
        switch total {
        case .homeRuns: return "\(homeRuns + bump)"
        case .doubles:  return doublesUnknown ? "—" : "\(doubles + bump)"
        case .triples:  return triplesUnknown ? "—" : "\(triples + bump)"
        }
    }
}
