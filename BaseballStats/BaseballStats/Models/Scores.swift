//
//  Scores.swift
//  BaseballStats
//
//  Codable models for MLB Stats API responses consumed by the
//  Scores tab. Two payload shapes are decoded:
//
//    1. `ScheduleResponse` — list of games for one date, with team
//       info, current score, linescore, and decisions hydrated in.
//    2. `BoxScoreResponse` — per-team batting + pitching lineups
//       for one game.
//
//  Field names mirror the API exactly so default Codable decoding
//  works without a CodingKeys map. Anything we don't need is
//  omitted; the decoder ignores unknown keys by default.
//

import Foundation

// MARK: - Schedule

struct ScheduleResponse: Codable {
    let dates: [ScheduleDate]
}

struct ScheduleDate: Codable {
    let date: String
    let games: [Game]
}

struct Game: Codable, Identifiable, Hashable {
    let gamePk: Int
    let gameDate: String              // ISO-8601 UTC, e.g. "2026-05-15T19:05:00Z"
    let status: GameStatus
    let teams: GameTeams
    let venue: Venue?
    let linescore: Linescore?
    let decisions: Decisions?
    // Authoritative BDL team ids straight off `BDLGame.{away,home}_team.id`.
    // `teams.{away,home}.team.id` carries the MLBAM id (good for logos);
    // these carry the BDL id (good for joining against BDL lineup /
    // stat rows). Optional so the struct remains decodable from older
    // payloads that didn't include them.
    let bdlAwayTeamId: Int?
    let bdlHomeTeamId: Int?

    var id: Int { gamePk }

    /// Bucketed game phase so the UI can branch on intent rather
    /// than the raw API state strings.
    enum Phase {
        case preview        // scheduled / not yet started
        case live           // in progress
        case final          // ended
        case other          // postponed / canceled / suspended
    }

    var phase: Phase {
        switch status.abstractGameState {
        case "Preview": return .preview
        case "Live":    return .live
        case "Final":   return .final
        default:        return .other
        }
    }

    /// `gameDate` parsed via `Date.ISO8601FormatStyle`. nil if the
    /// API ever ships an unexpected shape — caller falls back to
    /// the raw string.
    var startDate: Date? {
        try? Date(gameDate, strategy: .iso8601)
    }
}

struct GameStatus: Codable, Hashable {
    let abstractGameState: String     // "Preview", "Live", "Final"
    let detailedState: String         // "Scheduled", "In Progress", "Final", "Postponed", …
    let statusCode: String?
    let codedGameState: String?
}

struct GameTeams: Codable, Hashable {
    let away: GameTeam
    let home: GameTeam
}

struct GameTeam: Codable, Hashable {
    let team: TeamInfo
    let score: Int?
    let leagueRecord: TeamRecord?
    let isWinner: Bool?
    let probablePitcher: PlayerInfo?
}

struct TeamInfo: Codable, Hashable {
    let id: Int
    let name: String
    let abbreviation: String?

    /// Direct MLB logo URL — `midfield.mlbstatic.com` keys off the
    /// Stats API team id, so no Lahman code mapping is needed here.
    var logoURL: URL? {
        URL(string: "https://midfield.mlbstatic.com/v1/team/\(id)/spots/120")
    }
}

struct TeamRecord: Codable, Hashable {
    let wins: Int?
    let losses: Int?
    let pct: String?
}

struct Venue: Codable, Hashable {
    let id: Int?
    let name: String?
}

// MARK: - Linescore

struct Linescore: Codable, Hashable {
    let currentInning: Int?
    let currentInningOrdinal: String?  // "9th", "Top 7th", …
    let inningState: String?           // "Top", "Bottom", "Middle", "End"
    let innings: [Inning]?
    let teams: LinescoreTeamsTotals?
    let scheduledInnings: Int?
    let isTopInning: Bool?
    let balls: Int?
    let strikes: Int?
    let outs: Int?
}

struct Inning: Codable, Hashable {
    let num: Int
    let home: InningTotals?
    let away: InningTotals?
}

struct InningTotals: Codable, Hashable {
    let runs: Int?
    let hits: Int?
    let errors: Int?
    let leftOnBase: Int?
}

struct LinescoreTeamsTotals: Codable, Hashable {
    let home: InningTotals?
    let away: InningTotals?
}

// MARK: - Decisions

struct Decisions: Codable, Hashable {
    let winner: PlayerInfo?
    let loser: PlayerInfo?
    let save: PlayerInfo?
}

struct PlayerInfo: Codable, Hashable {
    let id: Int
    let fullName: String
}

// MARK: - Box score

struct BoxScoreResponse: Codable {
    let teams: BoxScoreTeams
}

struct BoxScoreTeams: Codable {
    let away: BoxScoreTeam
    let home: BoxScoreTeam
}

struct BoxScoreTeam: Codable {
    let team: TeamInfo
    /// Keyed by "ID{playerId}" — the box-score endpoint serializes
    /// players as a dict keyed by id-prefixed string, not an array.
    let players: [String: BoxPlayer]
    /// Batting order — list of player ids in lineup order. Pinch
    /// hitters / DH appear as additional ids past the starting nine.
    let batters: [Int]
    /// Pitching order — first id is the starter; later ids are the
    /// reliever appearances in order.
    let pitchers: [Int]
}

struct BoxPlayer: Codable {
    let person: PlayerInfo
    let position: BoxPosition?
    /// This-game stats. Both sides may be nil for a player who
    /// didn't appear (e.g. position player listed as a pitcher).
    let stats: BoxStats?
    /// Season-to-date stats — used to surface AVG / ERA next to
    /// the box-score line.
    let seasonStats: BoxStats?
    /// "Williams, P" style — same string the MLB.com box score
    /// surfaces, nil if the API didn't ship it.
    let stats_battingOrder: String?

    enum CodingKeys: String, CodingKey {
        case person, position, stats, seasonStats
        case stats_battingOrder = "battingOrder"
    }
}

struct BoxPosition: Codable {
    let abbreviation: String?
}

struct BoxStats: Codable {
    let batting: BoxBatting?
    let pitching: BoxPitching?
}

struct BoxBatting: Codable, Hashable {
    let atBats: Int?
    let runs: Int?
    let hits: Int?
    let doubles: Int?
    let triples: Int?
    let homeRuns: Int?
    let rbi: Int?
    let baseOnBalls: Int?
    let strikeOuts: Int?
    /// Per-game fields the MLB Stats API box score does ship —
    /// needed by the player-profile overlay to recompute OBP
    /// accurately (HBP + SF are in the formula's denominator;
    /// SB drives the season-SB increment).
    let stolenBases: Int?
    let caughtStealing: Int?
    let hitByPitch: Int?
    let sacFlies: Int?
    let sacBunts: Int?
    let groundIntoDoublePlay: Int?
    /// "AVG" — comes through as a String like ".301" / ".000" /
    /// "---" (the latter for players with 0 PA), so we keep it raw.
    let avg: String?
    /// "OPS" — same MLB convention as AVG. Strings like ".812",
    /// ".000", "---". Game-stats OPS rarely makes sense (it's
    /// just-the-game-so-far); the seasonStats version is what the
    /// box-score table actually surfaces.
    let ops: String?
}

struct BoxPitching: Codable, Hashable {
    let inningsPitched: String?   // "5.2" — string per MLB convention
    let hits: Int?
    let runs: Int?
    let earnedRuns: Int?
    let baseOnBalls: Int?
    let strikeOuts: Int?
    let homeRuns: Int?
    let era: String?              // "2.41" — string from the API
    /// Career-side W/L/SV used by `FinalGameCard` to render the
    /// decision pitchers' updated record next to their name in the
    /// expanded view ("W: Cole (8-2)"). Only meaningful on the
    /// `seasonStats.pitching` payload — the game-stats version is
    /// per-appearance and noisy.
    let wins: Int?
    let losses: Int?
    let saves: Int?
}

// MARK: - Live feed (/api/v1.1/game/{pk}/feed/live)

/// Full-game live snapshot — `LiveGameCard` polls this every 30s to
/// drive the in-progress card and the live BoxScoreView. Decodes
/// only the `liveData` subtree; the response also contains
/// `gameData` (static metadata) which we skip.
struct LiveFeedResponse: Codable {
    let liveData: LiveData
}

struct LiveData: Codable {
    let linescore: LiveLinescore?
    let plays: LivePlays?
    /// Same shape as the standalone /boxscore endpoint nested one
    /// level deeper. Live mode uses this so the batting + pitching
    /// tables on BoxScoreView refresh in sync with the rest of the
    /// live state without a second round trip.
    let boxscore: LiveBoxscoreEnvelope?
}

struct LiveBoxscoreEnvelope: Codable {
    let teams: BoxScoreTeams
}

struct LiveLinescore: Codable {
    let currentInning: Int?
    let currentInningOrdinal: String?       // "9th", etc.
    let inningHalf: String?                  // "Top" / "Bottom"
    let inningState: String?                 // "Top" / "Bottom" / "Middle" / "End"
    let isTopInning: Bool?
    let balls: Int?
    let strikes: Int?
    let outs: Int?
    /// Offense block — current batter + base runners. The bases
    /// themselves are `PlayerInfo?` per base; non-nil means a
    /// runner is on that base.
    let offense: LiveOffense?
    /// Defense block — current pitcher.
    let defense: LiveDefense?
    let innings: [Inning]?
    let teams: LinescoreTeamsTotals?
    let scheduledInnings: Int?
}

struct LiveOffense: Codable {
    let batter: PlayerInfo?
    let onDeck: PlayerInfo?
    let inHole: PlayerInfo?
    let first: PlayerInfo?
    let second: PlayerInfo?
    let third: PlayerInfo?
}

struct LiveDefense: Codable {
    let pitcher: PlayerInfo?
    let catcher: PlayerInfo?
}

struct LivePlays: Codable {
    let currentPlay: LivePlay?
}

struct LivePlay: Codable {
    let result: LivePlayResult?
    let about: LivePlayAbout?
    let matchup: LivePlayMatchup?
    let count: LivePlayCount?
    /// Per-pitch / per-event log for this PA. We surface the last
    /// item's description as a one-line "last play" string when
    /// `result.description` is empty (mid-PA states like a single
    /// pitch before the plate appearance ends).
    let playEvents: [LivePlayEvent]?
}

struct LivePlayResult: Codable {
    let description: String?
    let event: String?
}

struct LivePlayAbout: Codable {
    let halfInning: String?
    let inning: Int?
}

struct LivePlayMatchup: Codable {
    let batter: PlayerInfo?
    let pitcher: PlayerInfo?
}

struct LivePlayCount: Codable {
    let balls: Int?
    let strikes: Int?
    let outs: Int?
}

struct LivePlayEvent: Codable {
    let details: LivePlayEventDetails?
    let isPitch: Bool?
}

struct LivePlayEventDetails: Codable {
    let description: String?
    let event: String?
}

// MARK: - BallDontLie conversion layer
//
// During the MLB-Stats-API → BallDontLie migration, the Scores tab
// still drives off the existing `Game` / `ScheduleResponse` models
// — too many call sites to swap in one pass. These conversions let
// a `BDLGame` flow into the existing model space without forcing
// downstream views to know about the new shape.
//
// Mappings:
//   • status string  → GameStatus.abstractGameState ("Live"/"Final"/"Preview")
//   • BDL team id    → TeamInfo.id (MLBAM id, looked up via bdlToLahmanTeamId)
//   • innings array  → Linescore.innings (per-inning runs only —
//                       BDL doesn't ship hits/errors at the inning
//                       granularity, just team totals)
//   • scoring_summary → not surfaced (no equivalent on legacy Game;
//                       BoxScoreView's expand pane will consume the
//                       BDL game directly in Phase 3 instead)

/// Maps BDL status string → the abstractGameState enum-like string
/// the existing `Game.phase` accessor branches on. Anything we
/// don't recognize falls through to "Preview" so the UI defaults
/// to scheduled-game chrome.
private func bdlStatusToAbstract(_ status: String) -> String {
    switch status {
    case "STATUS_FINAL":            return "Final"
    case "STATUS_IN_PROGRESS":      return "Live"
    case "STATUS_SCHEDULED":        return "Preview"
    // Delays + postponements aren't truly "live" — the live UI
    // (LIVE badge, current-inning ordinal) doesn't apply. Treat
    // them as scheduled so the card renders the start-time chrome
    // instead of "LIVE · ?" with a question-mark inning.
    case "STATUS_POSTPONED":        return "Preview"
    case "STATUS_DELAYED":          return "Preview"
    case "STATUS_RAIN_DELAY":       return "Preview"
    default:                        return "Preview"
    }
}

extension BDLTeam {
    /// MLB Stats API numeric team id — what `TeamInfo.id` and the
    /// logo CDN want. Hops through Lahman so the bridge stays one
    /// place. Falls back to the BDL id if we can't resolve, which
    /// means logos won't load for that team but everything else
    /// still renders.
    var mlbStatsApiTeamId: Int {
        mlbTeamId(forBDLId: id) ?? id
    }

    /// Project to the legacy `TeamInfo` shape.
    func toTeamInfo() -> TeamInfo {
        TeamInfo(
            id:           mlbStatsApiTeamId,
            name:         displayName,
            abbreviation: abbreviation,
        )
    }
}

// MARK: - BDL box-score synthesis
//
// BDL's `/stats?game_ids[]={id}` ships a flat array of per-player
// lines. The existing `BoxScoreView` consumes a team-nested shape:
// `BoxScoreResponse.teams.{away,home}.players: [String: BoxPlayer]`
// keyed by `"ID{id}"`, plus `batters: [Int]` / `pitchers: [Int]`
// for ordering. The synthesizer below reshapes BDL's response into
// that legacy form, using BDL player ids in place of MLBAM ids
// throughout (the player-resolve path on tap routes through
// `BallDontLieClient.resolveBDLPlayerId` to hop back to MLBAM).
//
// Notes on what doesn't come through:
//   • `seasonStats` — BDL's per-game `/stats` endpoint doesn't
//     carry season-to-date AVG / OPS / ERA. Left nil; the view
//     degrades to "—" / "(0)" placeholders for those cells.
//   • Inningss-pitched format — BDL ships true decimal (5.667);
//     the view expects baseball notation ("5.2"). Converted at
//     synth time so visuals match the legacy path.

/// "5.667" → "5.2" (5 ⅔ innings in baseball notation). Round to
/// whole + outs/3. nil passes through.
func ipToBaseballNotation(_ ip: Double?) -> String? {
    guard let ip else { return nil }
    let whole = Int(ip)
    let frac  = ip - Double(whole)
    let outs  = Int((frac * 3).rounded())
    if outs == 3 { return "\(whole + 1).0" }
    return "\(whole).\(outs)"
}

/// `0.226` → `".226"` — MLB convention for AVG / OBP / SLG / OPS
/// is to drop the leading zero. nil passes through; values >= 1
/// (impossible for AVG/OBP, possible for OPS) keep the leading
/// digit ("1.034").
func formatMLBRate(_ v: Double?) -> String? {
    guard let v else { return nil }
    let s = String(format: "%.3f", v)
    return s.hasPrefix("0.") ? String(s.dropFirst()) : s
}

/// `2.41` → `"2.41"` — ERA keeps its leading digit. nil passes
/// through.
func formatMLBEra(_ v: Double?) -> String? {
    guard let v else { return nil }
    return String(format: "%.2f", v)
}

extension Array where Element == BDLPlayerStat {
    /// Project a BDL `/stats` response into the legacy
    /// `BoxScoreResponse` shape. `game.{away,home}Team` tells us
    /// which side each per-player row belongs to (we compare
    /// `BDLPlayerStat.teamName` against BDL's team names). `lineup`
    /// is optional — when supplied, batting + pitching order is
    /// driven off `BDLGameLineup.battingOrder`; without it, rows
    /// are ordered by their position in the BDL response (a usable
    /// fallback since BDL ships starters first).
    func toBoxScoreResponse(
        awayTeam: BDLTeam,
        homeTeam: BDLTeam,
        awayBDLTeamId: Int? = nil,
        homeBDLTeamId: Int? = nil,
        lineup: [BDLGameLineup] = [],
    ) -> BoxScoreResponse {
        // Bucket the per-player lines by team. BDL's `team_name`
        // is the short franchise name ("Yankees" / "Mariners");
        // BDLTeam.name carries the same value, so a direct equality
        // check works.
        var awayStats: [BDLPlayerStat] = []
        var homeStats: [BDLPlayerStat] = []
        for s in self {
            switch s.teamName {
            case awayTeam.name: awayStats.append(s)
            case homeTeam.name: homeStats.append(s)
            default:
                // BDL is occasionally inconsistent (display name vs.
                // short name). Fall back to a substring check —
                // close enough for the box-score split.
                if let tn = s.teamName, awayTeam.displayName.contains(tn) {
                    awayStats.append(s)
                } else if let tn = s.teamName, homeTeam.displayName.contains(tn) {
                    homeStats.append(s)
                }
            }
        }

        // Prefer the explicit BDL team ids handed in by the caller
        // (sourced from `BDLGame.{away,home}_team.id` via `Game`'s
        // `bdlAwayTeamId` / `bdlHomeTeamId` fields). Those are the
        // authoritative join keys for the lineup payload. Fall back
        // to the BDLTeam param's `.id` only when the caller didn't
        // supply them — older paths might not.
        let awayJoinId = awayBDLTeamId ?? awayTeam.id
        let homeJoinId = homeBDLTeamId ?? homeTeam.id

        let lineupAway = lineup.filter { $0.team.id == awayJoinId }
        let lineupHome = lineup.filter { $0.team.id == homeJoinId }

        return BoxScoreResponse(teams: BoxScoreTeams(
            away: buildBoxScoreTeam(team: awayTeam, stats: awayStats, lineup: lineupAway),
            home: buildBoxScoreTeam(team: homeTeam, stats: homeStats, lineup: lineupHome),
        ))
    }
}

// MARK: - BDL live-feed synthesis
//
// The existing live card consumes a `LiveFeedResponse` (rich
// nested shape from MLB Stats API's `/feed/live`). BDL exposes the
// same information across two endpoints: `/plays` (sequential play
// stream) and `/plate_appearances` (per-PA state with base
// runners). The synthesizer below combines them into a minimal
// LiveFeedResponse the live card and live-situation card can
// consume unchanged.
//
// What does and doesn't come through:
//   • currentInning / isTopInning / balls / strikes / outs — from
//     the latest play.
//   • current batter / pitcher / batterId / pitcherId — from the
//     latest play; names parsed out of the "Start Batter/Pitcher"
//     text ("Quintana pitches to McCutchen").
//   • Base runners — from the latest plate appearance. We only
//     need the presence-or-absence flag (the existing UI gates a
//     filled diamond cell on `offense.first != nil`, not on the
//     runner's name); use a stub PlayerInfo with id 0 when the
//     BDL PA flag is true.
//   • Last play description — from the latest play's `text`.
//   • Linescore innings + run-totals — NOT synthesized here.
//     Stays nil; the box-score view's separate `Game.linescore`
//     read (populated by `BDLGame.toGame`) drives those rows.

extension Array where Element == BDLPlay {
    /// Build a `LiveFeedResponse` from the BDL play stream plus
    /// the matching plate-appearance stream. Returns nil if the
    /// play stream is empty (game hasn't started, or BDL hasn't
    /// recorded any plays yet).
    func toLiveFeedResponse(plateAppearances: [BDLPlateAppearance]) -> LiveFeedResponse? {
        guard let lastPlay = self.last else { return nil }
        let lastPA = plateAppearances.last

        let (pitcherName, batterName) = parseLastMatchupText(in: self)

        let inningType = lastPlay.inningType ?? ""
        let isTop = inningType.lowercased() == "top"
        let ordinal = ordinalLabel(lastPlay.inning)

        // `inningState` semantics from the MLB Stats API: "Top",
        // "Middle", "Bottom", "End", "Final". BDL doesn't ship a
        // direct equivalent — map from inning type with a fallback
        // for "End Inning" play types so the existing isGameOver
        // check (compares to "final"/"game over") still has a hint
        // when the inning rolls over.
        let inningState: String
        switch lastPlay.type {
        case "End Inning":               inningState = "End"
        case "Middle Inning":             inningState = "Middle"
        default:                          inningState = isTop ? "Top" : "Bottom"
        }

        let runnerStub: (Bool?) -> PlayerInfo? = { flag in
            flag == true ? PlayerInfo(id: 0, fullName: "") : nil
        }

        let linescore = LiveLinescore(
            currentInning:        lastPlay.inning,
            currentInningOrdinal: ordinal,
            inningHalf:           lastPlay.inningType,
            inningState:          inningState,
            isTopInning:          isTop,
            balls:                lastPlay.balls,
            strikes:              lastPlay.strikes,
            outs:                 lastPlay.outs,
            offense: LiveOffense(
                batter: lastPlay.batterId.map { PlayerInfo(id: $0, fullName: batterName ?? "—") },
                onDeck: nil,
                inHole: nil,
                first:  runnerStub(lastPA?.runnerOnFirst),
                second: runnerStub(lastPA?.runnerOnSecond),
                third:  runnerStub(lastPA?.runnerOnThird),
            ),
            defense: LiveDefense(
                pitcher: lastPlay.pitcherId.map { PlayerInfo(id: $0, fullName: pitcherName ?? "—") },
                catcher: nil,
            ),
            innings:          nil,
            teams:            nil,
            scheduledInnings: 9,
        )

        let currentPlay = LivePlay(
            result: LivePlayResult(
                description: lastPlay.text,
                event:       lastPlay.type,
            ),
            about: LivePlayAbout(
                halfInning: lastPlay.inningType,
                inning:     lastPlay.inning,
            ),
            matchup: LivePlayMatchup(
                batter:  lastPlay.batterId.map { PlayerInfo(id: $0, fullName: batterName ?? "") },
                pitcher: lastPlay.pitcherId.map { PlayerInfo(id: $0, fullName: pitcherName ?? "") },
            ),
            count: LivePlayCount(
                balls:   lastPlay.balls,
                strikes: lastPlay.strikes,
                outs:    lastPlay.outs,
            ),
            playEvents: nil,
        )

        return LiveFeedResponse(liveData: LiveData(
            linescore: linescore,
            plays:     LivePlays(currentPlay: currentPlay),
            boxscore:  nil,
        ))
    }
}

/// Parse "Pitcher pitches to Batter" from the latest "Start
/// Batter/Pitcher" play. Returns (pitcher, batter) — either side
/// nil if the format doesn't match (mid-PA replay events, or a
/// future BDL wording change).
private func parseLastMatchupText(
    in plays: [BDLPlay],
) -> (pitcher: String?, batter: String?) {
    // Walk backwards to find the most recent matchup intro.
    for play in plays.reversed() where play.type == "Start Batter/Pitcher" {
        guard let text = play.text else { continue }
        if let r = text.range(of: " pitches to ") {
            let pitcher = String(text[..<r.lowerBound]).trimmingCharacters(in: .whitespaces)
            let batter  = String(text[r.upperBound...]).trimmingCharacters(in: .whitespaces)
            return (pitcher.isEmpty ? nil : pitcher,
                    batter.isEmpty  ? nil : batter)
        }
    }
    return (nil, nil)
}

/// 1 → "1st", 2 → "2nd", 3 → "3rd", 4-20 → "4th".."20th",
/// 21+ → standard English suffix rules.
private func ordinalLabel(_ n: Int) -> String {
    let last2 = n % 100
    let last1 = n % 10
    let suffix: String
    if (11...13).contains(last2) {
        suffix = "th"
    } else {
        switch last1 {
        case 1: suffix = "st"
        case 2: suffix = "nd"
        case 3: suffix = "rd"
        default: suffix = "th"
        }
    }
    return "\(n)\(suffix)"
}

/// Heuristic for whether a `BDLPlayerStat` row is a pitching line.
/// True when the pitcher fields are populated and the batter ones
/// are not (or are zero) — pure pitchers. Two-way players who
/// batted AND pitched (Ohtani) get two rows in BDL's response, one
/// flagged each way, so this gate doesn't accidentally hide them.
private func bdlStatIsPitcher(_ s: BDLPlayerStat) -> Bool {
    s.ip != nil
}

private func buildBoxScoreTeam(
    team: BDLTeam,
    stats: [BDLPlayerStat],
    lineup: [BDLGameLineup],
) -> BoxScoreTeam {
    var players: [String: BoxPlayer] = [:]
    var battingOrder: [Int] = []
    var pitchingOrder: [Int] = []

    // Index lineup entries by BDL player id so each per-player
    // BoxPlayer construction can pull THIS-GAME's position (and
    // batting-order presence) from the lineup row rather than the
    // stat row's BDL career-position fallback. Lineup carries the
    // post-DH-rule "DH" assignment for pitchers in the batting
    // order; the stat row's `player.position` would still say "SP".
    let lineupByPid: [Int: BDLGameLineup] = Dictionary(
        lineup.map { ($0.player.id, $0) },
        uniquingKeysWith: { a, _ in a },
    )

    // Build BoxPlayer records keyed by "ID{bdl_id}" so the existing
    // dict-lookup pattern in BoxScoreView (`team.players["ID\(id)"]`)
    // keeps working unchanged.
    for s in stats {
        let pid = s.player.id
        let key = "ID\(pid)"
        let isPitcher = bdlStatIsPitcher(s)
        // BDL's per-game stat row ships SEASON-to-date AVG / OBP /
        // SLG / ERA on the same payload as the per-game counts.
        // Legacy BoxBatting / BoxPitching split these across two
        // blocks: `stats.batting` for the game's counts, and
        // `seasonStats.batting` for the season rates the box-score
        // view reads. Keep that split so `BoxScoreView` renders
        // unchanged — game-side carries the integers, season-side
        // carries the formatted rate strings.
        let batting: BoxBatting? = isPitcher && (s.atBats ?? 0) == 0 ? nil : BoxBatting(
            atBats:               s.atBats,
            runs:                 s.runs,
            hits:                 s.hits,
            doubles:              s.doubles,
            triples:              s.triples,
            homeRuns:             s.hr,
            rbi:                  s.rbi,
            baseOnBalls:          s.bb,
            strikeOuts:           s.k,
            stolenBases:          s.stolenBases,
            caughtStealing:       s.caughtStealing,
            hitByPitch:           s.hitByPitch,
            sacFlies:             s.sacFlies,
            sacBunts:             nil,
            groundIntoDoublePlay: nil,
            avg:                  nil,  // → seasonStats.batting.avg
            ops:                  nil,
        )
        let pitching: BoxPitching? = !isPitcher ? nil : BoxPitching(
            inningsPitched: ipToBaseballNotation(s.ip),
            hits:           s.pHits,
            runs:           s.pRuns,
            earnedRuns:     s.er,
            baseOnBalls:    s.pBb,
            strikeOuts:     s.pK,
            homeRuns:       s.pHr,
            era:            nil,  // → seasonStats.pitching.era
            wins:           s.wins,
            losses:         s.losses,
            saves:          s.saves,
        )
        // Season block — rate stats only. BDL doesn't ship OPS
        // directly; derive from OBP + SLG when both are present.
        let opsValue: Double? = (s.obp != nil && s.slg != nil) ? (s.obp! + s.slg!) : nil
        let seasonBatting = BoxBatting(
            atBats:               nil,
            runs:                 nil,
            hits:                 nil,
            doubles:              nil,
            triples:              nil,
            homeRuns:             nil,
            rbi:                  nil,
            baseOnBalls:          nil,
            strikeOuts:           nil,
            stolenBases:          nil,
            caughtStealing:       nil,
            hitByPitch:           nil,
            sacFlies:             nil,
            sacBunts:             nil,
            groundIntoDoublePlay: nil,
            avg:                  formatMLBRate(s.avg),
            ops:                  formatMLBRate(opsValue),
        )
        let seasonPitching = BoxPitching(
            inningsPitched: nil,
            hits:           nil,
            runs:           nil,
            earnedRuns:     nil,
            baseOnBalls:    nil,
            strikeOuts:     nil,
            homeRuns:       nil,
            era:            formatMLBEra(s.era),
            wins:           nil,
            losses:         nil,
            saves:          nil,
        )
        // Position resolution: lineup row wins (it carries the
        // game-specific assignment, including DH for a pitcher
        // in the batting order). Stat row's `player.position` is
        // a career-default fallback. When a player has a non-null
        // batting_order AND a pitcher-type position, override to
        // "DH" — the universal-DH rule means a pitcher in the
        // batting order is the team's DH, not a hitter playing P.
        let lineupRow = lineupByPid[pid]
        let lineupPos = lineupRow?.position
        let inBattingOrder = (lineupRow?.battingOrder ?? 0) > 0
        let resolvedPosition: String? = {
            let raw = lineupPos ?? s.player.position
            if inBattingOrder, let r = raw?.uppercased(),
               r == "P" || r == "SP" || r == "RP" {
                return "DH"
            }
            return raw
        }()
        players[key] = BoxPlayer(
            person:             PlayerInfo(id: pid, fullName: s.player.fullName),
            position:           BoxPosition(abbreviation: resolvedPosition),
            stats:              BoxStats(batting: batting,       pitching: pitching),
            seasonStats:        BoxStats(batting: seasonBatting, pitching: seasonPitching),
            stats_battingOrder: nil,
        )
        // Track ordering: pitchers separate from batters. Two-way
        // players appear in both arrays under the same id, which
        // mirrors what the MLB Stats API does for Ohtani.
        if isPitcher { pitchingOrder.append(pid) }
        if batting != nil && (s.atBats ?? 0) > 0 || (s.bb ?? 0) > 0 || (s.plateAppearances ?? 0) > 0 {
            battingOrder.append(pid)
        }
    }

    // Apply lineup ordering when available — `battingOrder` ints
    // come back as e.g. 100 / 200 / 300 (slot * 100), so a numeric
    // sort gives the actual lineup sequence.
    if !lineup.isEmpty {
        let battersByLineup = lineup
            .filter { ($0.battingOrder ?? 0) > 0 }
            .sorted { ($0.battingOrder ?? 0) < ($1.battingOrder ?? 0) }
            .map(\.player.id)
        let pitchersByLineup = lineup
            .filter { $0.isProbablePitcher == true || ($0.position ?? "").lowercased() == "p" || ($0.position ?? "").lowercased() == "sp" }
            .map(\.player.id)

        // Intersect with the stats-bearing ids so we don't list
        // pinch-hitters who didn't actually appear, etc.
        let appeared = Set(battingOrder + pitchingOrder)
        let orderedBatters  = battersByLineup.filter  { appeared.contains($0) }
        let orderedPitchers = pitchersByLineup.filter { appeared.contains($0) }

        // Append any actual appearances the lineup didn't cover
        // (pinch hitters, bullpen pieces) so they still render.
        let lineupBatterSet  = Set(orderedBatters)
        let lineupPitcherSet = Set(orderedPitchers)
        let extraBatters  = battingOrder.filter  { !lineupBatterSet.contains($0) }
        let extraPitchers = pitchingOrder.filter { !lineupPitcherSet.contains($0) }

        battingOrder  = orderedBatters  + extraBatters
        pitchingOrder = orderedPitchers + extraPitchers
    }

    return BoxScoreTeam(
        team:     team.toTeamInfo(),
        players:  players,
        batters:  battingOrder,
        pitchers: pitchingOrder,
    )
}

extension BDLGame {
    /// Project to the legacy `Game` shape so existing views can
    /// consume BDL results without code changes. `gamePk` is set
    /// to the BDL id (it's an int in both worlds, just a different
    /// number space — the player-resolve / box-score paths know
    /// the difference and route accordingly).
    func toGame() -> Game {
        let abstract = bdlStatusToAbstract(status)
        let status = GameStatus(
            abstractGameState: abstract,
            detailedState:     detailedStateFromBDL(),
            statusCode:        nil,
            codedGameState:    nil,
        )
        let awayInfo = GameTeam(
            team:           awayTeam.toTeamInfo(),
            score:          awayTeamData?.runs,
            leagueRecord:   nil,
            isWinner:       nil,
            probablePitcher: nil,
        )
        let homeInfo = GameTeam(
            team:           homeTeam.toTeamInfo(),
            score:          homeTeamData?.runs,
            leagueRecord:   nil,
            isWinner:       nil,
            probablePitcher: nil,
        )
        return Game(
            gamePk:        id,
            gameDate:      date,
            status:        status,
            teams:         GameTeams(away: awayInfo, home: homeInfo),
            venue:         venue.map { Venue(id: nil, name: $0) },
            linescore:     toLinescore(),
            decisions:     nil,
            bdlAwayTeamId: awayTeam.id,
            bdlHomeTeamId: homeTeam.id,
        )
    }

    /// Approximate `detailedState` from BDL's status enum. We can't
    /// surface every Stats-API nuance (scheduled vs. warmup vs.
    /// pre-game) — collapse to the buckets that drive UI branches.
    private func detailedStateFromBDL() -> String {
        switch status {
        case "STATUS_FINAL":       return "Final"
        case "STATUS_IN_PROGRESS": return "In Progress"
        case "STATUS_SCHEDULED":   return "Scheduled"
        case "STATUS_POSTPONED":   return "Postponed"
        case "STATUS_DELAYED":     return "Delayed"
        default:                   return status
        }
    }

    /// Build a legacy `Linescore` from BDL's `inning_scores` arrays.
    /// BDL only ships per-team per-inning runs (no hits / errors /
    /// LOB at that granularity), so we leave those fields nil and
    /// surface the totals row from `home_team_data` / `away_team_data`.
    private func toLinescore() -> Linescore? {
        let awayInns = awayTeamData?.inningScores ?? []
        let homeInns = homeTeamData?.inningScores ?? []
        let inningCount = max(awayInns.count, homeInns.count)

        var innings: [Inning] = []
        for i in 0..<inningCount {
            let awayR = i < awayInns.count ? awayInns[i] : nil
            let homeR = i < homeInns.count ? homeInns[i] : nil
            innings.append(Inning(
                num:  i + 1,
                home: InningTotals(runs: homeR, hits: nil, errors: nil, leftOnBase: nil),
                away: InningTotals(runs: awayR, hits: nil, errors: nil, leftOnBase: nil),
            ))
        }

        let teamsTotals = LinescoreTeamsTotals(
            home: InningTotals(
                runs:       homeTeamData?.runs,
                hits:       homeTeamData?.hits,
                errors:     homeTeamData?.errors,
                leftOnBase: nil,
            ),
            away: InningTotals(
                runs:       awayTeamData?.runs,
                hits:       awayTeamData?.hits,
                errors:     awayTeamData?.errors,
                leftOnBase: nil,
            ),
        )

        // `currentInning` — for live games, BDL's `period` is the
        // current inning number. For finals, BDL doesn't ship a
        // value here; leave nil and let the existing UI fall back
        // to inning-array length.
        let currentInning = (status == "STATUS_IN_PROGRESS") ? period : nil

        return Linescore(
            currentInning:        currentInning,
            currentInningOrdinal: nil,
            inningState:          nil,
            innings:              innings.isEmpty ? nil : innings,
            teams:                teamsTotals,
            scheduledInnings:     9,
            isTopInning:          nil,
            balls:                nil,
            strikes:              nil,
            outs:                 nil,
        )
    }
}
