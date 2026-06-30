//
//  PostseasonBracket.swift
//  BaseballStats
//
//  Turns the flat, round_code-sorted `[PostseasonSeries]` for a year into a
//  structured bracket. Phase 2a handles ONLY the modern (1995+) DS → LCS → WS
//  format; other eras resolve to `.unsupported` for now.
//
//  EXTENSION POINT: `resolvePostseasonBracket(year:series:)` is the single
//  switch where future templates plug in — the LCS era (1969–1993), the
//  classic WS-only era (pre-1969), the 1981 split season, and a flat-list
//  fallback. Add new `PostseasonBracketLayout` cases + assemblers there; the
//  view already renders `.unsupported` as a graceful placeholder.
//

import Foundation

// MARK: - Round classification

enum PostseasonLeague: String {
    case al = "AL"
    case nl = "NL"
}

/// The four modern bracket rounds, earliest → latest.
enum ModernRound {
    case wildCard
    case divisionSeries
    case championship
    case worldSeries
}

/// Map a raw Lahman `round_code` to (league, round). League is nil for the
/// World Series (cross-league). Returns nil for codes that aren't part of the
/// modern shape (legacy 1981 AEDIV/AWDIV, pre-1900 codes, …) so the assembler
/// can detect a non-modern year and fall through to `.unsupported`.
func classifyModernRound(_ code: String) -> (league: PostseasonLeague?, round: ModernRound)? {
    if code == "WS" { return (nil, .worldSeries) }

    let league: PostseasonLeague?
    if code.hasPrefix("AL") { league = .al }
    else if code.hasPrefix("NL") { league = .nl }
    else { return nil }                      // not an AL*/NL* modern code

    let body = String(code.dropFirst(2))     // strip "AL" / "NL"
    if body.hasPrefix("CS") { return (league, .championship) }     // ALCS / NLCS
    if body.hasPrefix("DS") { return (league, .divisionSeries) }   // ALDS / ALDS1 / ALDS2
    if body.hasPrefix("WC") { return (league, .wildCard) }         // ALWC / ALWC1 / …
    return nil
}

// MARK: - Assembled modern bracket

/// A fully-placed modern bracket. Per-league columns plus the shared World
/// Series. Division / Wild Card are arrays (two DS per league; one or more WC
/// in the expanded era).
struct ModernBracket {
    let year: Int
    let worldSeries: PostseasonSeries
    let alChampionship: PostseasonSeries
    let nlChampionship: PostseasonSeries
    let alDivision: [PostseasonSeries]
    let nlDivision: [PostseasonSeries]
    let alWildCard: [PostseasonSeries]
    let nlWildCard: [PostseasonSeries]

    /// Assemble from a year's flat series list. Returns nil if the year doesn't
    /// match the modern template (needs DS + LCS in both leagues and a WS), so
    /// the caller can fall through to another era / the list fallback.
    /// Buckets one year's series into the modern rounds. Unrecognized/legacy
    /// codes are ignored here (the era switch routes years that have any of
    /// them to the list fallback before this is consulted).
    private struct Buckets {
        var ws: PostseasonSeries?
        var alCS: PostseasonSeries?
        var nlCS: PostseasonSeries?
        var alDS: [PostseasonSeries] = []
        var nlDS: [PostseasonSeries] = []
        var alWC: [PostseasonSeries] = []
        var nlWC: [PostseasonSeries] = []
    }

    private static func bucket(_ series: [PostseasonSeries]) -> Buckets {
        var b = Buckets()
        for s in series {
            guard let (league, round) = classifyModernRound(s.roundCode) else { continue }
            switch (league, round) {
            case (_, .worldSeries):       b.ws = s
            case (.al?, .championship):   b.alCS = s
            case (.nl?, .championship):   b.nlCS = s
            case (.al?, .divisionSeries): b.alDS.append(s)
            case (.nl?, .divisionSeries): b.nlDS.append(s)
            case (.al?, .wildCard):       b.alWC.append(s)
            case (.nl?, .wildCard):       b.nlWC.append(s)
            default:                      break
            }
        }
        return b
    }

    private static let byCode: (PostseasonSeries, PostseasonSeries) -> Bool = {
        $0.roundCode < $1.roundCode
    }

    /// Modern (1995+): a DS + LCS in BOTH leagues and a WS. Wild Card optional
    /// (absent 1995–2011). Returns nil otherwise.
    static func assembleModern(year: Int, series: [PostseasonSeries]) -> ModernBracket? {
        let b = bucket(series)
        guard let ws = b.ws, let alCS = b.alCS, let nlCS = b.nlCS,
              !b.alDS.isEmpty, !b.nlDS.isEmpty else { return nil }
        return ModernBracket(
            year: year, worldSeries: ws, alChampionship: alCS, nlChampionship: nlCS,
            alDivision: b.alDS.sorted(by: byCode), nlDivision: b.nlDS.sorted(by: byCode),
            alWildCard: b.alWC.sorted(by: byCode), nlWildCard: b.nlWC.sorted(by: byCode),
        )
    }

    /// LCS era (≈1969–1993): ALCS + NLCS + WS and NO Division Series. Same
    /// bracket model with empty DS / WC arrays — the layout omits those columns
    /// and connects each LCS straight to the WS.
    static func assembleLCS(year: Int, series: [PostseasonSeries]) -> ModernBracket? {
        let b = bucket(series)
        guard let ws = b.ws, let alCS = b.alCS, let nlCS = b.nlCS,
              b.alDS.isEmpty, b.nlDS.isEmpty else { return nil }
        return ModernBracket(
            year: year, worldSeries: ws, alChampionship: alCS, nlChampionship: nlCS,
            alDivision: [], nlDivision: [], alWildCard: [], nlWildCard: [],
        )
    }
}

// MARK: - Inferred connections (follow winners forward)

/// Best-effort series-to-series advancement. A series connects to a later-round
/// series when its WINNER appears as a participant (winner or loser) of that
/// later series — i.e. the team it sent forward actually played there. This is
/// provable for DS → LCS and LCS → WS (each winner is a distinct next-round
/// participant). The Wild-Card → DS handoff is left to the UI to render lightly
/// (see `PostseasonBracketView`): the connection can sometimes be followed, but
/// we never fabricate one where the winner isn't found.
enum PostseasonAdvancement {
    /// Does `earlier`'s winner appear in `later`? If so they're connected.
    static func connects(_ earlier: PostseasonSeries, to later: PostseasonSeries) -> Bool {
        earlier.winner == later.winner || earlier.winner == later.loser
    }

    /// The single later-round series `earlier` fed into, if exactly one matches
    /// by following its winner forward; nil if none (don't fabricate).
    static func advanced(_ earlier: PostseasonSeries,
                         into candidates: [PostseasonSeries]) -> PostseasonSeries? {
        let matches = candidates.filter { connects(earlier, to: $0) }
        return matches.count == 1 ? matches.first : nil
    }
}

// MARK: - Era switch (extension point)

/// The resolved layout for a year. `.bracket` covers both the modern (DS→LCS→WS)
/// and LCS-era (LCS→WS) trees — same renderer, the layout omits absent columns.
enum PostseasonBracketLayout {
    case bracket(ModernBracket)                       // modern or LCS-era tree
    case classic(year: Int, series: PostseasonSeries) // pre-1969: lone World Series
    case list(year: Int, series: [PostseasonSeries])  // legacy / odd structures
}

/// Single entry point that decides how to lay out a year, driven entirely by
/// which round codes are present (no hardcoded year ranges). Any year carrying
/// an UNRECOGNIZED round code (1981 AEDIV/AWDIV…, pre-1900 formats) skips the
/// bracket/classic paths and falls to the list — its structure isn't a clean
/// tree. New eras plug in here.
func resolvePostseasonBracket(year: Int, series: [PostseasonSeries]) -> PostseasonBracketLayout {
    let allRecognized = series.allSatisfy { classifyModernRound($0.roundCode) != nil }
    if allRecognized {
        // has DS + LCS + WS → modern tree
        if let modern = ModernBracket.assembleModern(year: year, series: series) {
            return .bracket(modern)
        }
        // has LCS + WS, no DS → LCS-era tree (two columns)
        if let lcs = ModernBracket.assembleLCS(year: year, series: series) {
            return .bracket(lcs)
        }
        // only a World Series → single prominent box
        if let ws = classicWorldSeries(series) {
            return .classic(year: year, series: ws)
        }
    }
    // Anything else (legacy codes, partial/odd structures) → round-by-round list.
    return .list(year: year, series: series.sorted(by: roundDepthOrder))
}

/// The lone World Series, if the year's ONLY series is a WS (no LCS/DS/WC).
private func classicWorldSeries(_ series: [PostseasonSeries]) -> PostseasonSeries? {
    guard series.count == 1, let only = series.first,
          classifyModernRound(only.roundCode)?.round == .worldSeries else { return nil }
    return only
}

/// List ordering: earliest round first, World Series last; legacy/unrecognized
/// codes sort to the front (they're early-round equivalents). Ties broken by
/// raw round code for stability.
private func roundDepthOrder(_ a: PostseasonSeries, _ b: PostseasonSeries) -> Bool {
    let da = listRoundDepth(a.roundCode), db = listRoundDepth(b.roundCode)
    return da != db ? da < db : a.roundCode < b.roundCode
}

private func listRoundDepth(_ code: String) -> Int {
    switch classifyModernRound(code)?.round {
    case .wildCard:       return 1
    case .divisionSeries: return 2
    case .championship:   return 3
    case .worldSeries:    return 9   // always last
    case nil:             return 0   // legacy/unknown — earliest
    }
}
