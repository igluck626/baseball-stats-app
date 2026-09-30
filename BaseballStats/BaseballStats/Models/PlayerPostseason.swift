//
//  PlayerPostseason.swift
//  BaseballStats
//
//  Codable models for `GET /players/{id}/postseason` — a player's postseason
//  record from our game logs (Retrosheet for published seasons, balldontlie
//  for the current one), per side: each season with its rounds, and the
//  career line. `current` carries the flags the Overview line needs.
//
//  Also here: `showsPostseasonOverviewLine`, the rule for the Overview's
//  "2026 Postseason" line. It is NOT the Standings bracket default
//  (`standingsDefault`, which runs until Opening Day) — this one ends with
//  the World Series. Two different rules, two functions.
//

import Foundation

struct PlayerPostseason: Codable, Hashable {
    let playerId: Int
    let retroLast: Int?
    let batting: PostseasonSide<PostseasonBattingTotals>?
    let pitching: PostseasonSide<PostseasonPitchingTotals>?
    let current: PostseasonCurrent
    /// Games in progress whose line is already added to the totals (the
    /// backend's live overlay); nil when none. Optional in the payload, so a
    /// backend without the overlay still decodes.
    var live: [PostseasonLiveGame]? = nil

    enum CodingKeys: String, CodingKey {
        case batting, pitching, current, live
        case playerId = "player_id"
        case retroLast = "retro_last"
    }

    /// Whether a live line is in this side's totals ("bat" / "pit").
    func isLive(batting: Bool) -> Bool {
        (live ?? []).contains { $0.sides.contains(batting ? "bat" : "pit") }
    }
}

/// One in-progress postseason game whose line the backend has added.
struct PostseasonLiveGame: Codable, Hashable {
    let gameId: String
    /// "bat" / "pit".
    let sides: [String]

    enum CodingKeys: String, CodingKey {
        case sides
        case gameId = "game_id"
    }
}

struct PostseasonSide<Totals: Codable & Hashable>: Codable, Hashable {
    /// Newest first.
    let seasons: [PostseasonSeasonLine<Totals>]
    let career: Totals
}

struct PostseasonSeasonLine<Totals: Codable & Hashable>: Codable, Hashable, Identifiable {
    let season: Int
    let source: String          // "retrosheet" / "bdl"
    let team: String
    let teamName: String?
    let totals: Totals
    let rounds: [PostseasonRoundLine<Totals>]

    var id: Int { season }

    enum CodingKeys: String, CodingKey {
        case season, source, team, totals, rounds
        case teamName = "team_name"
    }
}

struct PostseasonRoundLine<Totals: Codable & Hashable>: Codable, Hashable, Identifiable {
    let round: String           // WC / DS / CS / WS
    let roundName: String       // "ALDS", "World Series"
    let opponent: String
    let opponentName: String?
    let series: PostseasonRoundSeries
    let totals: Totals

    var id: String { round + opponent }

    enum CodingKeys: String, CodingKey {
        case round, opponent, series, totals
        case roundName = "round_name"
        case opponentName = "opponent_name"
    }
}

struct PostseasonRoundSeries: Codable, Hashable {
    let wins: Int
    let losses: Int
    /// nil while the series is still being played.
    let won: Bool?

    /// "Won 4-3", "Lost 1-2", "Leads 1-0" / "Trails 0-1" / "Tied 1-1".
    var summary: String {
        let score = "\(wins)-\(losses)"
        switch won {
        case true?:  return "Won \(score)"
        case false?: return "Lost \(score)"
        case nil:
            if wins > losses { return "Leads \(score)" }
            if wins < losses { return "Trails \(score)" }
            return "Tied \(score)"
        }
    }
}

struct PostseasonBattingTotals: Codable, Hashable {
    let G: Int
    let PA: Int
    let AB: Int
    let R: Int
    let H: Int
    let doubles: Int
    let triples: Int
    let HR: Int
    let RBI: Int
    let BB: Int
    let IBB: Int
    let SO: Int
    let SB: Int
    let CS: Int
    let HBP: Int
    let SF: Int
    let GIDP: Int
    let SH: Int
    let AVG: Double?
    let OBP: Double?
    let SLG: Double?
    let OPS: Double?
}

struct PostseasonPitchingTotals: Codable, Hashable {
    let G: Int
    let GS: Int
    let W: Int
    let L: Int
    let SV: Int
    /// Outs recorded — the exact figure. `IP` is its display form ("130.1").
    let outs: Int
    let IP: String
    let H: Int
    let R: Int
    let ER: Int
    let BB: Int
    let SO: Int
    let HR: Int
    let HBP: Int
    let ERA: Double?
    let WHIP: Double?

    /// Innings as a decimal (130.333…) — the career table's IP convention.
    var inningsDecimal: Double { Double(outs) / 3 }
}

struct PostseasonCurrent: Codable, Hashable {
    let season: Int
    /// From the first postseason game until the World Series ends; nil when
    /// balldontlie couldn't be reached.
    let leagueInProgress: Bool?
    let playerAppeared: Bool
    /// nil when he hasn't appeared, or balldontlie couldn't be reached.
    let teamEliminated: Bool?

    enum CodingKeys: String, CodingKey {
        case season
        case leagueInProgress = "league_in_progress"
        case playerAppeared = "player_appeared"
        case teamEliminated = "team_eliminated"
    }
}

// MARK: - Game logs (`GET /players/{id}/postseason/gamelogs?season=`)

/// One postseason's game-by-game lines, oldest first, per side (nil when he
/// has none on it). The backend reads the season's one source, the same rule
/// as the career line.
struct PostseasonGameLogs: Codable, Hashable {
    let playerId: Int
    let season: Int
    let source: String
    let batting: [PostseasonBattingGameLine]?
    let pitching: [PostseasonPitchingGameLine]?

    enum CodingKeys: String, CodingKey {
        case season, source, batting, pitching
        case playerId = "player_id"
    }
}

/// The fields every postseason game line carries, whichever side.
protocol PostseasonGameLine {
    var gameId: String { get }
    var date: String? { get }
    var round: String { get }
    var roundName: String { get }
    var gameNumber: Int? { get }
    var opponent: String? { get }
    var opponentName: String? { get }
    var homeAway: String? { get }
    var result: String? { get }
    var teamScore: Int? { get }
    var oppScore: Int? { get }
}

extension PostseasonGameLine {
    /// "ALDS G3", "WS G7", "WC G1" — the round short enough for a table
    /// column, and the game's number in the series (his TEAM's games, so a
    /// man who sat out Game 1 reads G2).
    var seriesGameLabel: String { postseasonSeriesGameLabel(round: round, roundName: roundName, gameNumber: gameNumber) }
    /// "W 8-5" / "L 1-6"; "—" when the score isn't known.
    var resultLabel: String { postseasonResultLabel(result: result, teamScore: teamScore, oppScore: oppScore) }
}

func postseasonSeriesGameLabel(round: String, roundName: String, gameNumber: Int?) -> String {
    let short: String
    switch round {
    case "WS": short = "WS"
    case "WC": short = "WC"
    default:   short = roundName            // "ALDS", "NLCS"
    }
    guard let gameNumber else { return short }
    return "\(short) G\(gameNumber)"
}

func postseasonResultLabel(result: String?, teamScore: Int?, oppScore: Int?) -> String {
    guard let result, let teamScore, let oppScore else { return "—" }
    return "\(result) \(teamScore)-\(oppScore)"
}

struct PostseasonBattingGameLine: Codable, Hashable, Identifiable, PostseasonGameLine {
    let gameId: String
    let date: String?
    let round: String
    let roundName: String
    let gameNumber: Int?
    let team: String?
    let opponent: String?
    let opponentName: String?
    let homeAway: String?
    let result: String?
    let teamScore: Int?
    let oppScore: Int?
    let PA: Int
    let AB: Int
    let R: Int
    let H: Int
    let doubles: Int
    let triples: Int
    let HR: Int
    let RBI: Int
    let BB: Int
    let IBB: Int
    let SO: Int
    let SB: Int
    let CS: Int
    let HBP: Int
    let SF: Int
    let GIDP: Int
    let SH: Int

    var id: String { gameId }

    enum CodingKeys: String, CodingKey {
        case date, round, team, opponent, result
        case PA, AB, R, H, doubles, triples, HR, RBI, BB, IBB, SO, SB, CS, HBP, SF, GIDP, SH
        case gameId = "game_id"
        case roundName = "round_name"
        case gameNumber = "game_number"
        case opponentName = "opponent_name"
        case homeAway = "home_away"
        case teamScore = "team_score"
        case oppScore = "opp_score"
    }
}

struct PostseasonPitchingGameLine: Codable, Hashable, Identifiable, PostseasonGameLine {
    let gameId: String
    let date: String?
    let round: String
    let roundName: String
    let gameNumber: Int?
    let team: String?
    let opponent: String?
    let opponentName: String?
    let homeAway: String?
    /// The TEAM's result; his own is `decision`.
    let result: String?
    let teamScore: Int?
    let oppScore: Int?
    /// W / L / S / H / ND.
    let decision: String?
    /// Outs recorded — the exact figure; `IP` is its display form ("6.2").
    let outs: Int
    let IP: String
    let H: Int
    let R: Int
    let ER: Int
    let BB: Int
    let SO: Int
    let HR: Int
    let HBP: Int
    let W: Int
    let L: Int
    let SV: Int
    let GS: Int

    var id: String { gameId }

    enum CodingKeys: String, CodingKey {
        case date, round, team, opponent, result, decision, outs, IP
        case H, R, ER, BB, SO, HR, HBP, W, L, SV, GS
        case gameId = "game_id"
        case roundName = "round_name"
        case gameNumber = "game_number"
        case opponentName = "opponent_name"
        case homeAway = "home_away"
        case teamScore = "team_score"
        case oppScore = "opp_score"
    }
}

// MARK: - The Overview line

/// ⚠️ THE OVERVIEW RULE: show the "<season> Postseason" line while the
/// league's postseason is in progress — first game until the World Series
/// ends — once the player has appeared in it, INCLUDING after his team is
/// eliminated. Not the bracket default (`standingsDefault`, until Opening
/// Day): a different rule, kept a different function.
///
/// Only for the side being shown, and only when that side has a line for the
/// current season. Unknown league state (balldontlie down) hides it: better
/// absent than shown past the World Series.
func showsPostseasonOverviewLine<T>(_ postseason: PlayerPostseason?,
                                    side: PostseasonSide<T>?) -> PostseasonSeasonLine<T>? {
    guard let postseason, postseason.current.leagueInProgress == true,
          postseason.current.playerAppeared,
          let line = side?.seasons.first(where: { $0.season == postseason.current.season })
    else { return nil }
    return line
}

/// ⚠️ THE POSTSEASON CAREER BOX RULE: while the league's postseason is in
/// progress, show it for ANY player with postseason history on the side being
/// shown — whether or not he or his team is in this year's (Mookie Betts on a
/// bye). Its own rule, not `showsPostseasonOverviewLine` (that one also needs
/// him to have appeared). Unknown league state hides it.
func showsPostseasonCareerBox<T>(_ postseason: PlayerPostseason?,
                                 side: PostseasonSide<T>?) -> PostseasonSide<T>? {
    guard postseason?.current.leagueInProgress == true, let side, !side.seasons.isEmpty else { return nil }
    return side
}

// MARK: - Into the career tables' row types

extension PostseasonSeasonLine where Totals == PostseasonBattingTotals {
    /// The season as a `CareerSeason`, so the postseason table formats every
    /// value exactly as the regular-season table does. WAR, OPS+ and the
    /// other season-only figures don't exist for a postseason and stay nil.
    var asCareerSeason: CareerSeason { totals.careerSeason(year: season, team: team) }
}

extension PostseasonBattingTotals {
    func careerSeason(year: Int?, team: String?) -> CareerSeason {
        CareerSeason(
            year: year, team: team, league: nil,
            G: G, PA: PA, AB: AB, R: R, H: H, doubles: doubles, triples: triples, HR: HR, RBI: RBI,
            BB: BB, IBB: IBB, HBP: HBP, SO: SO, SB: SB, CS: CS, SH: SH, SF: SF, GIDP: GIDP,
            TB: H + doubles + 2 * triples + 3 * HR,
            BA: AVG, OBP: OBP, SLG: SLG, OPS: OPS, BABIP: nil, ISO: nil,
            BB_pct: nil, K_pct: nil, wOBA: nil,
            WAR: nil, WAR_off: nil, WAR_def: nil, WAA: nil, OPS_plus: nil,
            runs_above_avg: nil, runs_above_rep: nil, leaders: nil)
    }
}

extension PostseasonSeasonLine where Totals == PostseasonPitchingTotals {
    var asCareerSeason: PitcherCareerSeason { totals.careerSeason(year: season, team: team) }
}

extension PostseasonPitchingTotals {
    func careerSeason(year: Int?, team: String?) -> PitcherCareerSeason {
        PitcherCareerSeason(
            year: year, team: team, league: nil,
            G: G, GS: GS, CG: nil, SHO: nil, GF: nil, W: W, L: L, SV: SV, IP: inningsDecimal,
            BFP: nil, H: H, R: R, ER: ER, HR: HR, BB: BB, IBB: nil, SO: SO, HBP: HBP,
            WP: nil, BK: nil, SH: nil, SF: nil, GIDP: nil, ERA: ERA, WHIP: WHIP, FIP: nil,
            BAOpp: nil, BABIP: nil,
            K_per9: outs > 0 ? Double(SO) * 27 / Double(outs) : nil,
            BB_per9: outs > 0 ? Double(BB) * 27 / Double(outs) : nil,
            HR_per9: outs > 0 ? Double(HR) * 27 / Double(outs) : nil,
            WAR: nil, WAR_def: nil, WAA: nil, ERA_plus: nil,
            runs_above_avg: nil, runs_above_rep: nil, leaders: nil)
    }
}
