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

    // Game-unit answers from the daily record (answer is nil — the card IS the
    // answer). Milestone: the Nth of an event (date + opponent). Streak: longest
    // run of games meeting a condition, with the line over those games. Span:
    // the most of an event in any N-game window, with the line over the window.
    let milestone: AskMilestone?
    let streak: AskStreak?
    let span: AskSpan?

    // A two-way player (Ohtani) asked an ambiguous stat (strikeouts/walks) — the
    // same milestone/streak/span computed BOTH as a pitcher and as a hitter.
    let two_way: AskTwoWay?

    // Rate-leaderboard qualifier metadata.
    let stat: String?
    let min_pa: Int?

    // Header for a streak/span leaderboard, e.g. "Longest hitting streak" or
    // "Most home runs in any 20 games" (the ranked list IS the answer).
    let leaderboard_title: String?

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
    /// Streak length ("56") or span total ("80") for the game-unit leaderboards.
    /// nil on count/rate boards, which use `count` / the rate columns instead.
    let value: Int?
    /// Per-row detail under the name — a streak's season ("1941") or a span's
    /// year range ("2001–2002"). nil when there's nothing useful to add.
    let subtitle: String?
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

/// A career milestone — the Nth of an event. On success `reached` is true and
/// the game is named (date + opponent, no pitcher — game logs carry none). When
/// not reached or unpinpointable the backend sends prose in `answer` instead.
struct AskMilestone: Decodable {
    let n: Int?
    let event: String?            // singular, e.g. "home run"
    let reached: Bool?
    let date_pretty: String?      // "April 8, 1974"
    let season: Int?
    let opponent: String?         // Lahman/Retrosheet team code (e.g. "LAN")
    let home_away: String?        // "H" / "A"
    let running_total: Int?       // == n on success
    // not-reached shape
    let current_total: Int?
    let through_season: Int?
}

/// A game-unit streak (hitting / on-base / home-run / multi-hit), within one
/// season, plus the player's batting line summed over the streak's games.
struct AskStreak: Decodable {
    let type_label: String?       // "hitting streak"
    let length: Int?
    let start_pretty: String?
    let end_pretty: String?
    let season: Int?
    let restricted: Bool?         // over fully-covered seasons only
    let line: AskStatLine?
}

/// The most of an event in any N-consecutive-game window (may cross seasons),
/// plus the batting line over the window's games.
struct AskSpan: Decodable {
    let event: String?            // plural, e.g. "home runs"
    let window: Int?
    let total: Int?
    let start_pretty: String?
    let end_pretty: String?
    let start_season: Int?
    let end_season: Int?
    let cross_season: Bool?
    let restricted: Bool?
    let line: AskStatLine?
}

/// A two-way answer: the same question resolved BOTH as a pitcher and as a
/// hitter (Ohtani's strikeouts). `stat` names the ambiguous stat ("strikeouts").
struct AskTwoWay: Decodable {
    let stat: String?
    let pitching: AskTwoWaySide?
    let batting: AskTwoWaySide?
}

/// One role's side of a two-way answer — whichever of milestone/streak/span the
/// question was, or a decline (`reason`) if that role couldn't answer.
struct AskTwoWaySide: Decodable {
    let milestone: AskMilestone?
    let streak: AskStreak?
    let span: AskSpan?
    let declined: Bool?
    let reason: String?
}

/// The batting line summed over a streak's or span's games. `2B`/`3B` map to
/// `doubles`/`triples` (a Swift property can't be named with a leading digit).
struct AskStatLine: Decodable {
    let G: Int?
    let AB: Int?
    let H: Int?
    let doubles: Int?
    let triples: Int?
    let HR: Int?
    let RBI: Int?
    let BB: Int?
    let SO: Int?
    let AVG: Double?
    let OBP: Double?
    let SLG: Double?
    let OPS: Double?

    enum CodingKeys: String, CodingKey {
        case G, AB, H, HR, RBI, BB, SO, AVG, OBP, SLG, OPS
        case doubles = "2B"
        case triples = "3B"
    }
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
/// this player's `mlbam_id`. The bio fields distinguish same-name players by a
/// human-readable detail line ("2015–2025 · LAD") instead of opaque ids.
struct AskCandidate: Decodable, Identifiable {
    let name: String?
    let mlbam_id: Int?
    let retro_id: String?
    let debut: Int?
    let last_season: Int?
    let position: String?
    let team: String?

    var id: String { retro_id ?? "\(mlbam_id ?? 0)" }

    /// "2015–2025 · LAD · 3B" — the parts we have, to tell candidates apart.
    /// The team arrives as a Lahman storage code (LAN, OAK); `teamAbbreviation`
    /// is the app's existing map to the fan-friendly form (LAD, ATH).
    var detail: String? {
        var parts: [String] = []
        if let d = debut {
            parts.append(last_season.map { "\(d)–\($0)" } ?? "\(d)–")
        }
        if let t = team, !t.isEmpty { parts.append(teamAbbreviation(for: t)) }
        if let p = position, !p.isEmpty { parts.append(p) }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }
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
