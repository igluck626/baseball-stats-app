//
//  LiveGame.swift
//  BaseballStats
//
//  Codable models for our backend's live-game proxy (Phase 1) and adapters
//  that project them into the app's EXISTING view-facing types — so the live
//  surfaces render unchanged but now read ONE consistent snapshot per game
//  from our backend (O(games), shared across users) instead of polling
//  balldontlie directly (O(users), score-vs-plays skew).
//
//    GET /live/games        → LiveGamesResponse  → [Game] merges (Scores/Home)
//    GET /live/games/{id}   → LiveGameDetail     → BoxScoreResponse + LiveFeedResponse + [BDLPlay]
//
//  The backend uses the server-side BDL key. The iOS BallDontLieClient + its
//  embedded key are still used for NON-LIVE paths (finished-game box scores,
//  the whole-date scores list, standings, lineups, schedules, season stats);
//  removing that key is a future phase.
//

import Foundation

// MARK: - GET /live/games (compact list)

struct LiveGamesResponse: Codable, Hashable {
    let fetchedAt: String
    let count: Int
    let games: [LiveGameSummary]

    enum CodingKeys: String, CodingKey {
        case fetchedAt = "fetched_at"
        case count, games
    }
}

struct LiveGameSummary: Codable, Hashable, Identifiable {
    let gameId: Int
    let fetchedAt: String
    let status: String           // "in_progress" / "final" / "scheduled"
    let isLive: Bool
    let inning: Int?
    let inningHalf: String?       // "top" / "bottom" / "middle" / "end"
    let outs: Int?
    let away: LiveTeamLite
    let home: LiveTeamLite

    var id: Int { gameId }

    enum CodingKeys: String, CodingKey {
        case gameId = "game_id"
        case fetchedAt = "fetched_at"
        case status
        case isLive = "is_live"
        case inning
        case inningHalf = "inning_half"
        case outs, away, home
    }
}

struct LiveTeamLite: Codable, Hashable {
    let teamCode: String?
    let abbreviation: String?
    let name: String?
    let runs: Int?

    enum CodingKeys: String, CodingKey {
        case teamCode = "team_code"
        case abbreviation, name, runs
    }
}

// MARK: - GET /live/games/{id} (full unified snapshot)

struct LiveGameDetail: Codable, Hashable {
    let gameId: Int
    let fetchedAt: String
    let status: String
    let season: Int?
    let seasonType: String?
    let summary: LiveSummaryBlock
    let linescore: LiveLinescoreBlock
    let situation: LiveSituationBlock
    let plays: [LivePlayRow]
    let scoringPlays: [LivePlayRow]
    let batting: LiveSidePlayers<LiveBatterRow>
    let pitching: LiveSidePlayers<LivePitcherRow>

    enum CodingKeys: String, CodingKey {
        case gameId = "game_id"
        case fetchedAt = "fetched_at"
        case status, season
        case seasonType = "season_type"
        case summary, linescore, situation, plays
        case scoringPlays = "scoring_plays"
        case batting, pitching
    }
}

struct LiveSummaryBlock: Codable, Hashable {
    let away: LiveTeamFull
    let home: LiveTeamFull
    let inning: Int?
    let inningHalf: String?
    let outs: Int?
    let balls: Int?
    let strikes: Int?
    let isLive: Bool

    enum CodingKeys: String, CodingKey {
        case away, home, inning
        case inningHalf = "inning_half"
        case outs, balls, strikes
        case isLive = "is_live"
    }
}

struct LiveTeamFull: Codable, Hashable {
    let bdlId: Int?
    let teamCode: String?
    let abbreviation: String?
    let name: String?
    let location: String?
    let league: String?
    let division: String?
    let runs: Int?
    let hits: Int?
    let errors: Int?

    enum CodingKeys: String, CodingKey {
        case bdlId = "bdl_id"
        case teamCode = "team_code"
        case abbreviation, name, location, league, division, runs, hits, errors
    }
}

struct LiveLinescoreBlock: Codable, Hashable {
    let innings: [LiveInningRow]
    let awayRuns: Int?
    let homeRuns: Int?
    let awayHits: Int?
    let homeHits: Int?
    let awayErrors: Int?
    let homeErrors: Int?
    let scheduledInnings: Int?

    enum CodingKeys: String, CodingKey {
        case innings
        case awayRuns = "away_runs"
        case homeRuns = "home_runs"
        case awayHits = "away_hits"
        case homeHits = "home_hits"
        case awayErrors = "away_errors"
        case homeErrors = "home_errors"
        case scheduledInnings = "scheduled_innings"
    }
}

struct LiveInningRow: Codable, Hashable {
    let num: Int
    let away: Int?
    let home: Int?
}

struct LiveSituationBlock: Codable, Hashable {
    let batter: LivePerson?
    let pitcher: LivePerson?
    let onFirst: Bool
    let onSecond: Bool
    let onThird: Bool

    enum CodingKeys: String, CodingKey {
        case batter, pitcher
        case onFirst = "on_first"
        case onSecond = "on_second"
        case onThird = "on_third"
    }
}

struct LivePerson: Codable, Hashable {
    let id: Int?
    let name: String?
}

struct LivePlayRow: Codable, Hashable {
    let order: Int?
    let inning: Int?
    let half: String?
    let outs: Int?
    let awayScore: Int?
    let homeScore: Int?
    let scoring: Bool?
    let scoreValue: Int?
    let batterId: Int?
    let pitcherId: Int?
    let text: String?
    let type: String?

    enum CodingKeys: String, CodingKey {
        case order, inning, half, outs
        case awayScore = "away_score"
        case homeScore = "home_score"
        case scoring
        case scoreValue = "score_value"
        case batterId = "batter_id"
        case pitcherId = "pitcher_id"
        case text, type
    }
}

struct LiveSidePlayers<Row: Codable & Hashable>: Codable, Hashable {
    let away: [Row]
    let home: [Row]
}

struct LiveBatterRow: Codable, Hashable {
    let id: Int?
    let name: String?
    let position: String?
    let ab: Int?
    let r: Int?
    let h: Int?
    let rbi: Int?
    let hr: Int?
    let bb: Int?
    let k: Int?
    let avg: Double?
    let obp: Double?
    let slg: Double?
}

struct LivePitcherRow: Codable, Hashable {
    let id: Int?
    let name: String?
    let ip: String?
    let h: Int?
    let r: Int?
    let er: Int?
    let bb: Int?
    let k: Int?
    let hr: Int?
    let era: Double?
    let w: Int?
    let l: Int?
    let sv: Int?
    /// Per-pitcher game pitch count from the backend (`pc`), sourced from the PA
    /// feed. nil when the feed carries no count yet (never 0 for that reason).
    let pc: Int?

    enum CodingKeys: String, CodingKey {
        case id, name, ip, h, r, er, bb, k, hr, era, w, l, sv, pc
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id   = try c.decodeIfPresent(Int.self,    forKey: .id)
        name = try c.decodeIfPresent(String.self, forKey: .name)
        // `ip` ships from balldontlie (via our proxy) as a NUMBER in baseball
        // notation (2.2 = 2⅔ innings), though a string form has been seen too.
        // Decode EITHER into the "2.2" string the IP formatter expects — a bare
        // numeric would otherwise throw a typeMismatch that fails the WHOLE
        // LiveGameDetail decode, which (via getOptional/try?) silently reads as
        // "game over" and kills live box-score polling. Mirrors the same
        // flexible `ip` handling in BDLPlayerStat.init(from:).
        if let s = try? c.decode(String.self, forKey: .ip) {
            ip = s.isEmpty ? nil : s
        } else if let d = try? c.decode(Double.self, forKey: .ip) {
            ip = d == d.rounded() ? String(Int(d)) : String(format: "%.1f", d)
        } else if let i = try? c.decode(Int.self, forKey: .ip) {
            ip = String(i)
        } else {
            ip = nil
        }
        h   = try c.decodeIfPresent(Int.self,    forKey: .h)
        r   = try c.decodeIfPresent(Int.self,    forKey: .r)
        er  = try c.decodeIfPresent(Int.self,    forKey: .er)
        bb  = try c.decodeIfPresent(Int.self,    forKey: .bb)
        k   = try c.decodeIfPresent(Int.self,    forKey: .k)
        hr  = try c.decodeIfPresent(Int.self,    forKey: .hr)
        era = try c.decodeIfPresent(Double.self, forKey: .era)
        w   = try c.decodeIfPresent(Int.self,    forKey: .w)
        l   = try c.decodeIfPresent(Int.self,    forKey: .l)
        sv  = try c.decodeIfPresent(Int.self,    forKey: .sv)
        pc  = try c.decodeIfPresent(Int.self,    forKey: .pc)
    }
}

// MARK: - Formatting helpers

private func liveRateString(_ v: Double?) -> String? {
    guard let v else { return nil }
    let s = String(format: "%.3f", v)
    if s.hasPrefix("0.")  { return String(s.dropFirst()) }       // ".301"
    if s.hasPrefix("-0.") { return "-" + String(s.dropFirst(2)) }
    return s
}

private func liveEraString(_ v: Double?) -> String? {
    guard let v else { return nil }
    return String(format: "%.2f", v)
}

private func inningOrdinal(_ inning: Int?, half: String?) -> String? {
    guard let inning else { return nil }
    let suffix: String
    switch inning % 10 {
    case 1 where inning % 100 != 11: suffix = "st"
    case 2 where inning % 100 != 12: suffix = "nd"
    case 3 where inning % 100 != 13: suffix = "rd"
    default:                          suffix = "th"
    }
    let halfWord: String
    switch (half ?? "").lowercased() {
    case "top":    halfWord = "Top "
    case "bottom": halfWord = "Bot "
    case "middle": halfWord = "Mid "
    case "end":    halfWord = "End "
    default:       halfWord = ""
    }
    return "\(halfWord)\(inning)\(suffix)"
}

// MARK: - Adapters → existing view-facing types

extension LiveTeamFull {
    /// MLBAM-id `TeamInfo` (drives the logo CDN) via the shared Lahman→MLBAM
    /// map; falls back to the BDL id only if the code can't be resolved.
    var teamInfo: TeamInfo {
        let mlbId = teamCode.flatMap { lahmanTeamIdToMLBId($0) } ?? bdlId ?? 0
        return TeamInfo(id: mlbId, name: name ?? "", abbreviation: abbreviation)
    }
}

extension LivePlayRow {
    /// Map to the existing `BDLPlay` the play-by-play UI groups + filters
    /// (by `type` "Start Batter/Pitcher" markers and `scoringPlay`).
    func toBDLPlay(gameId: Int) -> BDLPlay {
        BDLPlay(
            gameId:        gameId,
            order:         order ?? 0,
            type:          type,
            text:          text,
            homeScore:     homeScore ?? 0,
            awayScore:     awayScore ?? 0,
            inning:        inning ?? 0,
            inningType:    half.map { $0.prefix(1).uppercased() + $0.dropFirst() },  // "top" → "Top"
            scoringPlay:   scoring ?? false,
            scoreValue:    scoreValue,
            outs:          outs,
            balls:         nil,   // backend play block carries no per-pitch count
            strikes:       nil,
            batterId:      batterId,
            pitcherId:     pitcherId,
            pitchType:     nil,
            pitchVelocity: nil,
            trajectory:    nil,
        )
    }
}

extension LiveGameDetail {
    var playsAsBDL: [BDLPlay] { plays.map { $0.toBDLPlay(gameId: gameId) } }

    /// The live situation card's `LiveFeedResponse`, built DIRECTLY from the
    /// backend's already-synthesized state (no client-side play synthesis) —
    /// so inning/count/bases/batter/pitcher match the same snapshot as the
    /// score and plays.
    func toLiveFeedResponse() -> LiveFeedResponse {
        let half = summary.inningHalf
        let offense = LiveOffense(
            batter:  situation.batter.map { PlayerInfo(id: $0.id ?? 0, fullName: $0.name ?? "") },
            onDeck:  nil,
            inHole:  nil,
            first:   situation.onFirst  ? PlayerInfo(id: 0, fullName: "") : nil,
            second:  situation.onSecond ? PlayerInfo(id: 0, fullName: "") : nil,
            third:   situation.onThird  ? PlayerInfo(id: 0, fullName: "") : nil,
        )
        let defense = LiveDefense(
            pitcher: situation.pitcher.map { PlayerInfo(id: $0.id ?? 0, fullName: $0.name ?? "") },
            catcher: nil,
        )
        let innings: [Inning] = linescore.innings.map {
            Inning(
                num:  $0.num,
                home: InningTotals(runs: $0.home, hits: nil, errors: nil, leftOnBase: nil),
                away: InningTotals(runs: $0.away, hits: nil, errors: nil, leftOnBase: nil),
            )
        }
        let totals = LinescoreTeamsTotals(
            home: InningTotals(runs: linescore.homeRuns, hits: linescore.homeHits,
                               errors: linescore.homeErrors, leftOnBase: nil),
            away: InningTotals(runs: linescore.awayRuns, hits: linescore.awayHits,
                               errors: linescore.awayErrors, leftOnBase: nil),
        )
        let inningStateCap = half.map { $0.prefix(1).uppercased() + $0.dropFirst() }
        let liveLine = LiveLinescore(
            currentInning:        summary.inning,
            currentInningOrdinal: inningOrdinal(summary.inning, half: half),
            inningHalf:           inningStateCap,
            inningState:          status == "final" ? "Final" : inningStateCap,
            isTopInning:          (half ?? "").lowercased() == "top",
            balls:                summary.balls,
            strikes:              summary.strikes,
            outs:                 summary.outs,
            offense:              offense,
            defense:              defense,
            innings:              innings.isEmpty ? nil : innings,
            teams:                totals,
            scheduledInnings:     linescore.scheduledInnings,
        )
        let currentPlay: LivePlay? = plays.last.map { p in
            LivePlay(
                result:  LivePlayResult(description: p.text, event: p.type),
                about:   LivePlayAbout(halfInning: p.half, inning: p.inning),
                matchup: LivePlayMatchup(
                    batter:  situation.batter.map { PlayerInfo(id: $0.id ?? 0, fullName: $0.name ?? "") },
                    pitcher: situation.pitcher.map { PlayerInfo(id: $0.id ?? 0, fullName: $0.name ?? "") },
                ),
                count:   LivePlayCount(balls: summary.balls, strikes: summary.strikes, outs: summary.outs),
                playEvents: nil,
            )
        }
        return LiveFeedResponse(liveData: LiveData(
            linescore: liveLine,
            plays:     LivePlays(currentPlay: currentPlay),
            boxscore:  nil,
        ))
    }

    /// The box-score tables' `BoxScoreResponse`, built directly from the
    /// per-player lines — same snapshot as the score/plays/situation.
    func toBoxScoreResponse() -> BoxScoreResponse {
        BoxScoreResponse(teams: BoxScoreTeams(
            away: Self.boxTeam(team: summary.away,
                               batters: batting.away, pitchers: pitching.away),
            home: Self.boxTeam(team: summary.home,
                               batters: batting.home, pitchers: pitching.home),
        ))
    }

    private static func boxTeam(team: LiveTeamFull,
                                batters: [LiveBatterRow],
                                pitchers: [LivePitcherRow]) -> BoxScoreTeam {
        var players: [String: BoxPlayer] = [:]
        var batterOrder: [Int] = []
        var pitcherOrder: [Int] = []

        for b in batters {
            guard let pid = b.id else { continue }
            let batting = BoxBatting(
                atBats: b.ab, runs: b.r, hits: b.h, doubles: nil, triples: nil,
                homeRuns: b.hr, rbi: b.rbi, baseOnBalls: b.bb, strikeOuts: b.k,
                stolenBases: nil, caughtStealing: nil, hitByPitch: nil,
                sacFlies: nil, sacBunts: nil, groundIntoDoublePlay: nil,
                avg: liveRateString(b.avg), ops: nil,
            )
            // Season AVG / OPS live in `seasonStats` — the slot the box-score
            // table's AVG/OPS columns read (BoxScoreView `battingRow`). The live
            // payload ships season `avg`/`obp`/`slg` (not a precomputed `ops`),
            // so compute OPS = OBP + SLG here, mirroring the BDL-path precedent
            // (Scores.swift `opsValue`). Counting stats stay on `stats` above.
            let seasonOPS: String? = (b.obp != nil && b.slg != nil)
                ? liveRateString(b.obp! + b.slg!)
                : nil
            let seasonBatting = BoxBatting(
                atBats: nil, runs: nil, hits: nil, doubles: nil, triples: nil,
                homeRuns: nil, rbi: nil, baseOnBalls: nil, strikeOuts: nil,
                stolenBases: nil, caughtStealing: nil, hitByPitch: nil,
                sacFlies: nil, sacBunts: nil, groundIntoDoublePlay: nil,
                avg: liveRateString(b.avg), ops: seasonOPS,
            )
            players["ID\(pid)"] = BoxPlayer(
                person:   PlayerInfo(id: pid, fullName: b.name ?? ""),
                position: BoxPosition(abbreviation: b.position),
                stats:    BoxStats(batting: batting, pitching: nil),
                seasonStats: BoxStats(batting: seasonBatting, pitching: nil),
                stats_battingOrder: nil,
            )
            batterOrder.append(pid)
        }

        for p in pitchers {
            guard let pid = p.id else { continue }
            let pitching = BoxPitching(
                inningsPitched: p.ip, hits: p.h, runs: p.r, earnedRuns: p.er,
                baseOnBalls: p.bb, strikeOuts: p.k, homeRuns: p.hr,
                era: liveEraString(p.era), wins: p.w, losses: p.l,
                saves: p.sv, pitchCount: p.pc,
            )
            // Season ERA lives in `seasonStats` — the slot the box-score table's
            // ERA column reads (BoxScoreView `pitchingRow`). Counting stats
            // (IP/H/R/ER/BB/SO) stay on `stats` above.
            let seasonPitching = BoxPitching(
                inningsPitched: nil, hits: nil, runs: nil, earnedRuns: nil,
                baseOnBalls: nil, strikeOuts: nil, homeRuns: nil,
                era: liveEraString(p.era), wins: nil, losses: nil,
                saves: nil, pitchCount: nil,
            )
            // Two-way player (e.g. Ohtani) already in the dict as a batter:
            // merge the pitching line into the same BoxPlayer rather than
            // clobbering the batting line. Preserve the batter's season batting
            // (avg/ops) while adding the pitching season ERA.
            if let existing = players["ID\(pid)"] {
                players["ID\(pid)"] = BoxPlayer(
                    person:   existing.person,
                    position: existing.position,
                    stats:    BoxStats(batting: existing.stats?.batting, pitching: pitching),
                    seasonStats: BoxStats(batting: existing.seasonStats?.batting, pitching: seasonPitching),
                    stats_battingOrder: existing.stats_battingOrder,
                )
            } else {
                players["ID\(pid)"] = BoxPlayer(
                    person:   PlayerInfo(id: pid, fullName: p.name ?? ""),
                    position: BoxPosition(abbreviation: "P"),
                    stats:    BoxStats(batting: nil, pitching: pitching),
                    seasonStats: BoxStats(batting: nil, pitching: seasonPitching),
                    stats_battingOrder: nil,
                )
            }
            pitcherOrder.append(pid)
        }

        return BoxScoreTeam(
            team:     team.teamInfo,
            players:  players,
            batters:  batterOrder,
            pitchers: pitcherOrder,
        )
    }
}

// MARK: - Live summary → Game (Scores list / Home merge)

extension Game {
    /// Return a copy of this game with its score / inning state / status
    /// refreshed from a live snapshot, preserving everything else (records,
    /// venue, dates, BDL ids). Used to merge `/live/games` into the existing
    /// date list without re-fetching finished/scheduled games.
    func merging(live: LiveGameSummary) -> Game {
        let newStatus = GameStatus(
            abstractGameState: live.isLive ? "Live" : (live.status == "final" ? "Final" : status.abstractGameState),
            detailedState:     live.isLive ? "In Progress" : status.detailedState,
            statusCode:        status.statusCode,
            codedGameState:    status.codedGameState,
        )
        let newAway = GameTeam(
            team: teams.away.team, score: live.away.runs ?? teams.away.score,
            leagueRecord: teams.away.leagueRecord, isWinner: teams.away.isWinner,
            probablePitcher: teams.away.probablePitcher,
        )
        let newHome = GameTeam(
            team: teams.home.team, score: live.home.runs ?? teams.home.score,
            leagueRecord: teams.home.leagueRecord, isWinner: teams.home.isWinner,
            probablePitcher: teams.home.probablePitcher,
        )
        let half = live.inningHalf
        let newLine = Linescore(
            currentInning:        live.inning ?? linescore?.currentInning,
            currentInningOrdinal: inningOrdinal(live.inning, half: half) ?? linescore?.currentInningOrdinal,
            inningState:          half.map { $0.prefix(1).uppercased() + $0.dropFirst() } ?? linescore?.inningState,
            innings:              linescore?.innings,
            teams:                linescore?.teams,
            scheduledInnings:     linescore?.scheduledInnings ?? 9,
            isTopInning:          (half ?? "").lowercased() == "top",
            balls:                linescore?.balls,
            strikes:              linescore?.strikes,
            outs:                 live.outs ?? linescore?.outs,
        )
        return Game(
            gamePk:        gamePk,
            gameDate:      gameDate,
            status:        newStatus,
            teams:         GameTeams(away: newAway, home: newHome),
            venue:         venue,
            linescore:     newLine,
            decisions:     decisions,
            bdlAwayTeamId: bdlAwayTeamId,
            bdlHomeTeamId: bdlHomeTeamId,
        )
    }

    /// A `.final` copy of this game carrying the given authoritative score +
    /// linescore, preserving everything else (teams, venue, records, ids).
    /// Mirrors `merging(live:)` but forces the FINAL state. Used by the box
    /// score's end-transition to fold the last live snapshot (then the refetched
    /// box score) into a single coherent final `Game` that every display path
    /// reads uniformly — so `phase == .final` + a nil live snapshot flip the
    /// whole view to final at once, with no per-path suppression.
    func asFinal(awayScore: Int?, homeScore: Int?, linescore: Linescore?) -> Game {
        let finalStatus = GameStatus(
            abstractGameState: "Final",
            detailedState:     "Final",
            statusCode:        status.statusCode,
            codedGameState:    status.codedGameState,
        )
        // Derive the winner from the final score when both are known (nil on a
        // tie / unknown), so the scores-list winner styling is right if this
        // final game object ever flows back out.
        let awayWon: Bool? = {
            guard let a = awayScore, let h = homeScore, a != h else { return nil }
            return a > h
        }()
        let newAway = GameTeam(
            team: teams.away.team, score: awayScore ?? teams.away.score,
            leagueRecord: teams.away.leagueRecord,
            isWinner: awayWon ?? teams.away.isWinner,
            probablePitcher: teams.away.probablePitcher,
        )
        let newHome = GameTeam(
            team: teams.home.team, score: homeScore ?? teams.home.score,
            leagueRecord: teams.home.leagueRecord,
            isWinner: awayWon.map { !$0 } ?? teams.home.isWinner,
            probablePitcher: teams.home.probablePitcher,
        )
        return Game(
            gamePk:        gamePk,
            gameDate:      gameDate,
            status:        finalStatus,
            teams:         GameTeams(away: newAway, home: newHome),
            venue:         venue,
            linescore:     linescore ?? self.linescore,
            decisions:     decisions,
            bdlAwayTeamId: bdlAwayTeamId,
            bdlHomeTeamId: bdlHomeTeamId,
        )
    }
}
