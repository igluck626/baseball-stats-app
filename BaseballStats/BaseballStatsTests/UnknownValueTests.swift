//
//  UnknownValueTests.swift
//
//  ⚠️ AN UNKNOWN IS NEVER A ZERO. A value balldontlie didn't ship is NULL on
//  the server, and the server shows "—" for any total built on it. These
//  tests run REAL responses (testdata/unknowns/, captured 2026-10-19) through
//  every place the app sums or formats one itself: the box-score records,
//  the Game Logs totals, the career row, and today's overlay.
//
//  The players: Isaac Collins (9 games with AB or BB and an unknown double,
//  HBP, SF and SB; his 2026 PA / HBP / SF / CS / IBB are NULL), Billy Cook
//  (28 pinch-runner games with SB and CS NULL, no AB and no BB), Codi Heuer
//  (4 pitching games with HBP NULL), Will Warren (his 05-06 start's decision
//  unknown since the guarded write of 2026-10-19; the record before it is kept
//  too, flags clear).
//
//  `older-server-*` are the same endpoints answered by the build-17-era
//  backend (the build18 worktree's server, run locally): no `*_unknown`
//  fields. Build 18 must read them, as unknown-free.
//

import Foundation
import Testing
@testable import BaseballStats

private func fixture(_ name: String) throws -> Data {
    let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().appendingPathComponent("testdata/unknowns/\(name).json")
    return try Data(contentsOf: url)
}

private func decode<T: Decodable>(_ type: T.Type, _ name: String) throws -> T {
    try JSONDecoder().decode(type, from: fixture(name))
}

@Suite("Unknown values")
struct UnknownValueTests {

    // MARK: Box score — batter totals at a date

    @Test func collinsDoublesAreUnknownOnTheLiveServer() throws {
        let rec = try APIClient.batterStatsAtDate(from: fixture("collins-batter-stats-at-date"))
        #expect(rec.doublesUnknown && rec.triplesUnknown)
        // The number underneath is the known games' 22 — never shown.
        #expect(rec.doubles == 22)
        #expect(rec.figure(.doubles, bump: 0) == "—")
        #expect(rec.figure(.triples, bump: 1) == "—")
        // HR is never unknown.
        #expect(rec.figure(.homeRuns, bump: 0) == "6")
    }

    @Test func anOlderServerWithoutTheFlagsStillDecodes() throws {
        let rec = try APIClient.batterStatsAtDate(from: fixture("older-server-collins-batter-stats-at-date"))
        #expect(!rec.doublesUnknown && !rec.triplesUnknown)
        #expect(rec.figure(.doubles, bump: 0) == "22")
    }

    // MARK: Box score — pitcher record at a date

    @Test func warrenRecordDecodesFromTheLiveServer() throws {
        let rec = try APIClient.pitcherRecord(from: fixture("warren-pitcher-record-at-date"))
        #expect(!rec.winsUnknown && !rec.lossesUnknown && !rec.savesUnknown)
        #expect(rec.figure(for: "W", bump: 0) == "11-6")
        #expect(rec.figure(for: "L", bump: 1) == "11-7")
        #expect(rec.figure(for: "SV", bump: 0) == "0")
    }

    @Test func anOlderServerPitcherRecordStillDecodes() throws {
        let rec = try APIClient.pitcherRecord(from: fixture("older-server-warren-pitcher-record-at-date"))
        #expect(!rec.winsUnknown)
        #expect(rec.figure(for: "W", bump: 0) == "11-6")
    }

    /// The live record after the guarded write (2026-10-19): Warren's 05-06 start
    /// is stored with its decision unknown, so the server sends the known games'
    /// 11-5 with every flag set — and each figure shows "—".
    @Test func warrensLiveRecordAfterTheWriteShowsADash() throws {
        let rec = try APIClient.pitcherRecord(from: fixture("warren-pitcher-record-at-date-flagged"))
        #expect(rec.winsUnknown && rec.lossesUnknown && rec.savesUnknown)
        #expect(rec.wins == 11 && rec.losses == 5)
        #expect(rec.figure(for: "W", bump: 1) == "—")
        #expect(rec.figure(for: "L", bump: 0) == "—")
        #expect(rec.figure(for: "SV", bump: 0) == "—")
        #expect(rec.figure(for: "HLD", bump: 0) == nil)
    }

    // MARK: Game Logs totals

    @Test func collinsSeasonTotalsMatchTheServersOwnWindow() throws {
        let log = try decode(GameLogResponse.self, "collins-gamelogs-batting")
        let games = try #require(log.games)
        let w = WindowSnapshot.computeBatting(games: games)
        // Unknown, as the server's own season window has them.
        #expect(w.doubles == nil && w.triples == nil && w.tb == nil)
        #expect(w.sb == nil && w.obp == nil && w.slg == nil && w.ops == nil)
        #expect(log.splits?.season?.SB == nil && log.splits?.season?.OBP == nil)
        // Never unknown: AB, H, HR — and AVG on them.
        #expect(w.ab == games.reduce(0) { $0 + ($1.AB ?? 0) } && w.ab! > 0)
        #expect(w.hr != nil && w.avg != nil && log.splits?.season?.BA != nil)
    }

    @Test func overOnlyRecordedGamesTheTotalsAreNumbersAgain() throws {
        let games = try #require(try decode(GameLogResponse.self, "collins-gamelogs-batting").games)
        let recorded = games.filter { $0.doubles != nil && $0.HBP != nil && $0.SF != nil && $0.SB != nil && $0.CS != nil }
        let w = WindowSnapshot.computeBatting(games: recorded)
        #expect(w.doubles != nil && w.sb != nil && w.obp != nil && w.slg != nil)
    }

    @Test func cookPinchRunnerGamesLeaveSBAndCSUnknownButNotOBP() throws {
        let games = try #require(try decode(GameLogResponse.self, "cook-gamelogs-batting").games)
        let pinch = games.filter { $0.SB == nil }
        #expect(pinch.count == 28)
        #expect(pinch.allSatisfy { ($0.AB ?? 0) == 0 && ($0.BB ?? 0) == 0 })
        let w = WindowSnapshot.computeBatting(games: games)
        // SB / CS: unknown on ANY game.
        #expect(w.sb == nil && w.cs == nil)
        // A no-AB/no-BB game's other NULLs read as 0 (the activity rule).
        #expect(w.obp != nil && w.slg != nil && w.doubles != nil)
    }

    @Test func heuerPitchingHBPIsUnknown() throws {
        let games = try #require(try decode(GameLogResponse.self, "heuer-gamelogs-pitching").games)
        #expect(games.filter { $0.HBP == nil }.count == 4)
        let w = WindowSnapshot.computePitching(games: games)
        #expect(w.hbp == nil)
        #expect(w.era != nil && w.ip != nil)
        // Every decision is recorded, so the month W-L is a number.
        #expect(UnknownRule.winLoss(games) != nil)
    }

    // MARK: Career row

    @Test func collinsCareerPAAndOBPAreUnknownLikeTheServers() throws {
        let career = try decode(PlayerCareerStats.self, "collins-career-batting")
        let seasons = try #require(career.seasons)
        let agg = BattingCareerAgg.compute(seasons: seasons, currentSeason: 2026)
        #expect(agg.pa == nil && agg.hbp == nil && agg.sf == nil && agg.cs == nil && agg.ibb == nil)
        #expect(agg.obp == nil && agg.kPct == nil && agg.woba == nil)
        // The server's own career totals agree.
        #expect(career.career_totals?.OBP == nil && career.career_totals?.AVG != nil)
        // AB, H and HR come from balldontlie's season stats — never unknown.
        #expect(agg.avg != nil && agg.slg != nil)
    }

    @Test func aPastSeasonNullStillReadsAsZero() throws {
        // The same rows a year on: 2026 is past, its NULL "not recorded".
        let seasons = try #require(try decode(PlayerCareerStats.self, "collins-career-batting").seasons)
        let agg = BattingCareerAgg.compute(seasons: seasons, currentSeason: 2027)
        #expect(agg.pa != nil && agg.obp != nil)
    }

    @Test func heuerCareerHBPAndFIPAreUnknown() throws {
        let seasons = try #require(try decode(PitcherCareerStats.self, "heuer-career-pitching").seasons)
        let agg = PitchingCareerAgg.compute(seasons: seasons, currentSeason: 2026)
        #expect(agg.hbp == nil && agg.fip == nil)
        #expect(agg.r != nil && agg.era != nil)
    }

    // MARK: Today's overlay

    /// balldontlie's own /stats lines for two real finals (fetched 2026-10-18):
    /// 5058322, Will Warren's 05-06 start, one of the 12 games whose decisions
    /// it never shipped (no line has a W or an L); 5057802, his 03-28 start
    /// (4.1 IP, no decision), whose decisions it did ship.
    private func gameLines(_ name: String) throws -> [BDLPlayerStat] {
        struct Page: Decodable { let data: [BDLPlayerStat] }
        let d = JSONDecoder(); d.keyDecodingStrategy = .convertFromSnakeCase
        return try d.decode(Page.self, from: fixture(name)).data
    }

    @Test func aFinalWithNoWinOrLossLeavesTheDecisionsUnknown() throws {
        let lines = try gameLines("bdl-stats-5058322-unshipped")
        #expect(!UnknownRule.decisionsShipped(lines))
        #expect(UnknownRule.decisionUnknowns(gameLines: lines, isLive: false) == ["W", "L", "SV"])
        // The same lines while the game is live: no decision yet is a real 0.
        #expect(UnknownRule.decisionUnknowns(gameLines: lines, isLive: true).isEmpty)
    }

    @Test func aFinalWithItsDecisionsLeavesNothingUnknown() throws {
        let lines = try gameLines("bdl-stats-5057802-shipped")
        #expect(UnknownRule.decisionsShipped(lines))
        #expect(UnknownRule.decisionUnknowns(gameLines: lines, isLive: false).isEmpty)
    }

    @Test func warrensUnshippedStartMakesTheMergedRecordADash() throws {
        let lines = try gameLines("bdl-stats-5058322-unshipped")
        let warren = try #require(lines.first { $0.player.id == 6274 })
        var line = try #require(PlayerViewModel.bdlPitchingLine(warren))
        let stats = try decode(PitcherCurrentStats.self, "warren-pitching-current")
        // Live: his 0s stand, the season record passes through with them.
        #expect(makeEffectivePitching(overnight: stats, recent: line).W == stats.standard?.W)
        // Final, decisions not shipped: W, L and SV unknown.
        line.unknown = UnknownRule.decisionUnknowns(gameLines: lines, isLive: false)
        let e = makeEffectivePitching(overnight: stats, recent: line)
        #expect(e.W == nil && e.L == nil && e.SV == nil)
        // Innings, strikeouts and ERA are unaffected.
        #expect(e.IP != nil && e.SO != nil && e.ERA != nil)
    }

    @Test func anUnknownSeasonPAStaysUnknownWithTodayAdded() throws {
        let stats = try decode(PlayerCurrentStats.self, "collins-stats-current")
        var today = BoxBattingLine(games: 1, AB: 4, R: 0, H: 1, doubles: 0, triples: 0, HR: 0, RBI: 0,
                                   BB: 0, SO: 1, SB: 0, HBP: 0, SF: 0)
        let e = makeEffectiveBatting(overnight: stats, recent: today)
        // His season PA / HBP / SF are NULL: PA and the computed OBP can't be known.
        #expect(stats.standard?.PA == nil && e.PA == nil)
        #expect(e.SB == (stats.standard?.SB ?? -1))
        // Today's line shipped without its steals: SB unknown, not +0.
        today.unknown = ["SB"]
        #expect(makeEffectiveBatting(overnight: stats, recent: today).SB == nil)
        // No overlay: the season row passes through as it is.
        #expect(makeEffectiveBatting(overnight: stats, recent: nil).PA == nil)
    }

    /// balldontlie's own season stats (Michael Helman, fetched 2026-10-16,
    /// as the app's balldontlie client decodes them) ship no HBP or SF.
    @Test func balldontliesSeasonTotalsLeaveHBPAndSFUnknown() throws {
        struct Page: Decodable { let data: [BDLSeasonStat] }
        let d = JSONDecoder(); d.keyDecodingStrategy = .convertFromSnakeCase
        let row = try #require(try d.decode(Page.self, from: fixture("bdl-season-stats-helman")).data.first)
        let line = row.toBattingLine()
        #expect(line.unknown == ["HBP", "SF"])
        #expect(line.AB == row.battingAb)
    }
}
