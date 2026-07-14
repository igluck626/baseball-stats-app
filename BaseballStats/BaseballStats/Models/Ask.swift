//
//  Ask.swift
//  BaseballStats
//
//  Wire models for `POST /ask` — the natural-language stats endpoint.
//
//  The backend returns ONE variable-shape JSON object whose populated
//  fields depend on how the question was interpreted (a single count, a
//  leaderboard, rate stats, splits, a decline, an out-of-scope note, or an
//  ambiguous-name prompt). Rather than model each shape separately, every
//  field here is optional and the UI switches on which ones are present.
//  Keys are snake_case verbatim — the shared decoder does NOT apply
//  `convertFromSnakeCase`. Extra keys the backend adds later are ignored,
//  and any key we don't declare (e.g. `understood_as`, `timing_ms`) simply
//  isn't decoded, so it can't cause a type-mismatch failure.
//

import Foundation

/// One `/ask` response. All fields optional; the view decides what to
/// render from which fields are non-nil (see `AskAnswerView`).
struct AskResponse: Decodable {
    /// The question, echoed back.
    let question: String?
    /// The phrased, prose answer — present in essentially every non-error
    /// case (including declines and out-of-scope, where it carries the
    /// friendly "why not" sentence).
    let answer: String?

    // Single situational count (e.g. "how many times…").
    let count: Int?

    // Structured payloads — at most one is populated per answer.
    let sample: [AskPlay]?
    let leaders: [AskLeader]?
    let rates: AskRates?
    let splits: [AskSplit]?

    // Rate-leaderboard qualifier metadata.
    let stat: String?
    let min_pa: Int?

    // For a situational COUNT: which stat-table column to emphasize (the one
    // the question asked about, e.g. "HR" / "SO"). nil = emphasize nothing.
    let highlighted_stat: String?

    // Resolution + provenance.
    let player_resolved: AskPlayerResolved?
    let source: String?
    let game_coverage: AskGameCoverage?
    let count_data: AskCountData?

    // Disposition flags. `declined` / `out_of_scope` are GOOD answers
    // ("we don't have that / can't express that") — the UI styles them
    // neutrally, not as failures. `ambiguous` surfaces name candidates.
    let ambiguous: Bool?
    let declined: Bool?
    let out_of_scope: Bool?
    let reason: String?
    let error: String?
    let cached: Bool?
}

/// One example play backing a situational count (the "show me" evidence).
struct AskPlay: Decodable, Identifiable {
    let game_id: String?        // Retrosheet game id, e.g. "NYA202105280"
    let game_date: String?
    let opponent: String?
    let home_team: String?
    let away_team: String?
    let inning: Int?
    let count: String?          // pitch count as "3-2"
    let pitcher_id: String?     // present when the subject is the batter
    let batter_id: String?      // present when the subject is the pitcher
    let description: String?    // raw Retrosheet event text (not shown to users)

    var id: String { (game_id ?? "") + "|" + (game_date ?? "") + "|" + (description ?? "") }
}

/// One ranked row of a leaderboard. Count boards populate `count`; rate
/// boards populate `PA` + the rate columns. `mlbam_id` (when present) lets
/// the row tap through to the player profile.
struct AskLeader: Decodable, Identifiable {
    let rank: Int?
    let player_name: String?
    let mlbam_id: Int?
    let retro_id: String?
    let count: Int?
    let PA: Int?
    let AB: Int?
    let H: Int?
    let HR: Int?
    let AVG: Double?
    let OBP: Double?
    let SLG: Double?
    let OPS: Double?

    var id: String { retro_id ?? "\(rank ?? 0)-\(player_name ?? "?")" }
}

/// The slash line + volume for a single rate query. The core set (PA/AB/H/HR/
/// RBI + the slash line) is shown up front; the rest (2B/3B/BB/HBP/SF/SO) is
/// the "More" detail.
struct AskRates: Decodable {
    let PA: Int?
    let AB: Int?
    let H: Int?
    let doubles: Int?
    let triples: Int?
    let HR: Int?
    let RBI: Int?
    let SO: Int?
    let BB: Int?
    let HBP: Int?
    let SF: Int?
    let AVG: Double?
    let OBP: Double?
    let SLG: Double?
    let OPS: Double?
}

/// One row of a split table (e.g. vs LHP / vs RHP, by count, by month).
struct AskSplit: Decodable, Identifiable {
    let split_value: String?
    let PA: Int?
    let AB: Int?
    let H: Int?
    let HR: Int?
    let RBI: Int?
    let SO: Int?
    let AVG: Double?
    let OBP: Double?
    let SLG: Double?
    let OPS: Double?

    var id: String { split_value ?? "?" }
}

/// Who the question resolved to — or, when `candidates` is set, the set of
/// same-name players to disambiguate between.
struct AskPlayerResolved: Decodable {
    let name: String?
    let mlbam_id: Int?
    let retro_id: String?
    let role: String?
    let candidates: [AskCandidate]?
}

/// One same-name candidate. Tapping re-asks the original question pinned to
/// this player's `mlbam_id`.
struct AskCandidate: Decodable, Identifiable {
    let name: String?
    let mlbam_id: Int?
    let retro_id: String?

    var id: String { retro_id ?? "\(mlbam_id ?? 0)" }
}

/// Coverage caveat: how complete the play-by-play is for the queried span.
/// `note` is the human-readable footnote the UI shows neutrally.
struct AskGameCoverage: Decodable {
    let complete: Bool?
    let min_pct: Double?
    let low_seasons: [Int]?
    let note: String?
}

/// Whether pitch-count data was recorded for the queried era, with a `note`
/// explaining any gap. Also shown as a neutral footnote.
struct AskCountData: Decodable {
    let available: Bool?
    let pct: Double?
    let note: String?
}
