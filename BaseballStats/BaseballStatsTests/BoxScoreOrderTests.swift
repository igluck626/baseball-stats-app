//
//  BoxScoreOrderTests.swift
//  BaseballStatsTests
//
//  Substitute placement in the current-season box score.
//
//  The historical box score sets a pinch hitter in under the man he
//  replaced; the BDL path used to append him at the bottom, because
//  `/lineups` carries only the nine starters and `/stats` carries no
//  batting order at all. `substituteBattingOrders` recovers the slot
//  from the plate-appearance sequence.
//
//  The thing actually worth testing is not that the derivation works
//  — it's that it REFUSES when the sequence can't be trusted. A
//  shifted rotation puts every later substitute in a plausible-looking
//  wrong slot, and a wrong slot reads as fact. An append is visibly an
//  append. So `countMismatchFallsBackToAppending` is the load-bearing
//  test here, not `pinchHittersLandInTheSlotTheyReplaced`.
//
//  Fixtures are real BDL payloads (BoxScoreOrderFixtures.json),
//  captured 2026-09-06, trimmed to the decoded fields with nulls
//  dropped.
//

import Foundation
import Testing
@testable import BaseballStats

private final class FixtureAnchor {}

private struct BoxOrderFixture: Decodable {
    let gameId: Int
    let away: BDLTeam
    let home: BDLTeam
    let lineups: [BDLGameLineup]
    let stats: [BDLPlayerStat]
    let pas: [BDLPlateAppearance]
}

private func fixture(_ name: String) throws -> BoxOrderFixture {
    let bundle = Bundle(for: FixtureAnchor.self)
    let url = try #require(
        bundle.url(forResource: "BoxScoreOrderFixtures", withExtension: "json"),
        "fixture JSON missing from the test bundle",
    )
    let decoder = JSONDecoder()
    decoder.keyDecodingStrategy = .convertFromSnakeCase
    let all = try decoder.decode([String: BoxOrderFixture].self, from: Data(contentsOf: url))
    return try #require(all[name], "no fixture named \(name)")
}

private func boxScore(_ fx: BoxOrderFixture, isFinal: Bool) -> BoxScoreResponse {
    fx.stats.toBoxScoreResponse(
        awayTeam:         fx.away,
        homeTeam:         fx.home,
        awayBDLTeamId:    fx.away.id,
        homeBDLTeamId:    fx.home.id,
        lineup:           fx.lineups,
        plateAppearances: fx.pas,
        isFinal:          isFinal,
    )
}

private extension BoxScoreTeam {
    func player(_ fullName: String) -> BoxPlayer? {
        players.values.first { $0.person.fullName == fullName }
    }
    /// Batting-order codes in the order the table will render them.
    var renderedCodes: [String?] {
        batters.map { players["ID\($0)"]?.stats_battingOrder }
    }
    var renderedNames: [String] {
        batters.compactMap { players["ID\($0)"]?.person.fullName }
    }
}

// MARK: - The substitute lands in the slot he took

@Test func pinchHittersLandInTheSlotTheyReplaced() throws {
    // TB @ TEX, 2026-09-05. Three substitutes across the two sides.
    // Ground truth was established independently of the derivation:
    // for each substitute, the starter in the slot he was assigned had
    // stopped batting, and the substitute's first plate appearance
    // falls exactly NINE positions after that starter's last — i.e. he
    // batted the very next time that slot came round. Any other slot
    // would put him a non-multiple of nine away from some starter's
    // last PA.
    let fx = try fixture("pinchHitters")
    let bs = boxScore(fx, isFinal: true)

    // TEX bats the bottom half.
    let tex = bs.teams.home
    #expect(tex.player("Justin Foscue")?.stats_battingOrder == "101")
    #expect(tex.player("Nicky Lopez")?.stats_battingOrder  == "801")
    // The men they came in for keep depth 0 and stay where they were.
    #expect(tex.player("Joc Pederson")?.stats_battingOrder == "100")
    #expect(tex.player("Cody Freeman")?.stats_battingOrder == "800")

    // TB bats the top half.
    let tb = bs.teams.away
    #expect(tb.player("Ryan Vilade")?.stats_battingOrder == "401")
    #expect(tb.player("Liam Hicks")?.stats_battingOrder   == "400")

    // The indent the view actually draws.
    #expect(BoxScoreView.substitutionDepth(try #require(tex.player("Justin Foscue"))) == 1)
    #expect(BoxScoreView.substitutionDepth(try #require(tex.player("Joc Pederson"))) == 0)

    // And he is SET IN UNDER the man he replaced, not appended: Foscue
    // renders immediately after Pederson, not at the bottom of the table.
    let names = tex.renderedNames
    let pederson = try #require(names.firstIndex(of: "Joc Pederson"))
    let foscue   = try #require(names.firstIndex(of: "Justin Foscue"))
    #expect(foscue == pederson + 1)
    #expect(foscue != names.count - 1, "appended at the bottom, not indented")

    // The whole table is in code order.
    let codes = tex.renderedCodes.compactMap { $0.flatMap(Int.init) }
    #expect(codes == codes.sorted())
    #expect(codes.count == tex.batters.count, "every batter carries a code")
}

// MARK: - A game with no substitutes is unchanged

@Test func gameWithNoSubstitutesRendersNineFlatRows() throws {
    // ATL @ PHI, 2026-09-05. Neither side used a substitute batter,
    // and both halves reconcile, so both tables place all nine.
    let fx = try fixture("noSubstitutes")
    let bs = boxScore(fx, isFinal: true)

    for team in [bs.teams.away, bs.teams.home] {
        #expect(team.batters.count == 9)
        // Slots 1...9, every one of them at depth 0, so nothing indents.
        #expect(team.renderedCodes == ["100", "200", "300", "400",
                                       "500", "600", "700", "800", "900"])
        for pid in team.batters {
            let p = try #require(team.players["ID\(pid)"])
            #expect(BoxScoreView.substitutionDepth(p) == 0)
        }
    }
}

// MARK: - A faulted side places only what it can prove

@Test func aFaultedSidePlacesOnlyLocallyProvenSubstitutes() throws {
    // LAD @ COL, 2026-08-20. COL's half carries 42 PA rows against 41
    // counted — an inning-ending caught stealing writes a row for a
    // batter who then leads off the next inning. The side-wide rotation
    // walk refuses it, correctly: every slot behind that point is
    // shifted. But the fault is in one place, and a substitute whose
    // neighbours agree on both sides is unaffected by it.
    let fx = try fixture("countMismatch")
    let bs = boxScore(fx, isFinal: true)
    let col = bs.teams.home

    #expect(col.player("Jordan Beck")?.stats_battingOrder == "501")
    #expect(col.player("Willi Castro")?.stats_battingOrder == "901")
    #expect(col.player("Zac Veen")?.stats_battingOrder == "500")
    #expect(col.player("Ezequiel Tovar")?.stats_battingOrder == "900")

    // Set in under the men they came in for, not appended.
    let names = col.renderedNames
    #expect(names.firstIndex(of: "Jordan Beck")
            == (names.firstIndex(of: "Zac Veen").map { $0 + 1 }))
    #expect(names.firstIndex(of: "Willi Castro")
            == (names.firstIndex(of: "Ezequiel Tovar").map { $0 + 1 }))

    // The nine starters keep their declared slots regardless — those
    // come from the lineup, which the damaged sequence can't touch.
    for slot in 1...9 {
        let code = String(slot * 100)
        #expect(col.renderedCodes.contains(code), "slot \(slot) missing")
    }
    // LAD's half is intact and unaffected.
    #expect(bs.teams.away.renderedCodes.allSatisfy { $0 != nil })
}

// MARK: - Live games are owned by a different path

@Test func liveGameDerivesNothing() throws {
    // A live game's box score is not built here: BoxScoreView
    // subscribes to LiveGameStore and overwrites `boxScore` from
    // LiveGameDetail on every snapshot. Deriving slots for one would
    // show the indent on first load and lose it a tick later, so the
    // derivation declines outright.
    let fx = try fixture("liveGame")
    let bs = boxScore(fx, isFinal: false)

    for team in [bs.teams.away, bs.teams.home] {
        #expect(team.renderedCodes.allSatisfy { $0 == nil })
        for pid in team.batters {
            let p = try #require(team.players["ID\(pid)"])
            #expect(BoxScoreView.substitutionDepth(p) == 0)
        }
    }

    // Same payload, marked final, does place substitutes — so the
    // refusal above is the `isFinal` guard and not an empty feed.
    let asFinal = boxScore(fx, isFinal: true)
    let placed = [asFinal.teams.away, asFinal.teams.home]
        .flatMap(\.renderedCodes)
        .contains { $0 != nil }
    #expect(placed)
}

// MARK: - The historical path is untouched

@Test func historicalBattingOrderCodesStillDecodeAndIndent() throws {
    // The historical box score arrives from our own backend with MLB's
    // battingOrder string already on the row, decoded straight into
    // `stats_battingOrder`. Nothing in the substitute work touches that
    // path; this pins the shape it depends on.
    let json = """
    {"teams":{"away":{"team":{"id":115,"name":"Colorado Rockies"},
      "batters":[1,2,3],"pitchers":[],
      "players":{
        "ID1":{"person":{"id":1,"fullName":"Drew Stubbs"},
               "position":{"abbreviation":"LF"},"battingOrder":"600"},
        "ID2":{"person":{"id":2,"fullName":"Ben Paulsen"},
               "position":{"abbreviation":"PH"},"battingOrder":"902"},
        "ID3":{"person":{"id":3,"fullName":"Carlos Gonzalez"},
               "position":{"abbreviation":"RF"},"battingOrder":"906"}}},
      "home":{"team":{"id":121,"name":"New York Mets"},
        "batters":[],"pitchers":[],"players":{}}}}
    """
    let bs = try JSONDecoder().decode(BoxScoreResponse.self, from: Data(json.utf8))
    let away = bs.teams.away

    #expect(away.player("Drew Stubbs")?.stats_battingOrder == "600")
    #expect(BoxScoreView.substitutionDepth(try #require(away.player("Drew Stubbs"))) == 0)
    // Two substitutes deep in the ninth slot, and the sixth man in it.
    #expect(BoxScoreView.substitutionDepth(try #require(away.player("Ben Paulsen"))) == 2)
    #expect(BoxScoreView.substitutionDepth(try #require(away.player("Carlos Gonzalez"))) == 6)

    // The label a reader sees is the GAME position, which is why the
    // historical path shows "PH" for a man who never took the field and
    // a real position for one who did. The BDL path has only a roster
    // position, so it shows that instead — an indented row with a
    // position on it either way, never an indented bare name.
    #expect(away.player("Ben Paulsen")?.position?.abbreviation == "PH")
    #expect(away.player("Carlos Gonzalez")?.position?.abbreviation == "RF")
}


// MARK: - The team resolver's primary path

//  `bdlTeams(forGame:fromStats:)` read `s.player.team`, which /stats never
//  populates, so it fell through to `stubBDLTeam` on every game from May
//  2026 until the row-level `team` was decoded. These pin the primary path
//  actually running, because "it compiles" was true for three and a half
//  months while it never once resolved.

private func gameForFixtureTeams(
    away: BDLTeam, home: BDLTeam, bdlIds: Bool,
) -> Game {
    func info(_ t: BDLTeam) -> GameTeam {
        GameTeam(
            team: TeamInfo(id: 0, name: t.displayName, abbreviation: t.abbreviation),
            score: nil, leagueRecord: nil, isWinner: nil, probablePitcher: nil,
        )
    }
    return Game(
        gamePk: 5059890,
        gameDate: "2026-09-05T23:05:00Z",
        status: GameStatus(abstractGameState: "Final", detailedState: "Final",
                           statusCode: nil, codedGameState: nil),
        teams: GameTeams(away: info(away), home: info(home)),
        venue: nil, linescore: nil, decisions: nil,
        bdlAwayTeamId: bdlIds ? away.id : nil,
        bdlHomeTeamId: bdlIds ? home.id : nil,
        bdlGameId: 5059890,
    )
}

@MainActor
@Test func statsRowsCarryTheirTeamAtRowLevel() throws {
    let fx = try fixture("pinchHitters")
    // The field the resolver used to read, on a real payload.
    #expect(fx.stats.allSatisfy { $0.player.team == nil },
            "/stats does not nest team under player — that was the bug")
    // The field it reads now.
    #expect(fx.stats.allSatisfy { $0.team != nil })
    #expect(Set(fx.stats.compactMap { $0.team?.id }) == [27, 28])
}

@MainActor
@Test func resolverProducesRealTeamIdsNotTheStub() throws {
    let fx = try fixture("pinchHitters")
    // Deliberately WITHOUT the caller-supplied BDL ids: this is the shape
    // that used to resolve to two stubs, both with id 0, collapsing the
    // lineup split on both sides at once.
    let game = gameForFixtureTeams(away: fx.away, home: fx.home, bdlIds: false)
    let vm = BoxScoreViewModel(game: game)
    let (away, home) = vm.bdlTeams(forGame: game, fromStats: fx.stats)

    #expect(away.id == 27, "TB resolved to \(away.id) — 0 means the stub ran")
    #expect(home.id == 28, "TEX resolved to \(home.id) — 0 means the stub ran")
    #expect(away.abbreviation == "TB")
    #expect(home.abbreviation == "TEX")
    // Short franchise name, which only the real BDL object carries — the
    // stub sets both `name` and `displayName` to TeamInfo.name.
    #expect(away.name == "Rays")
    #expect(home.name == "Rangers")
}

@MainActor
@Test func resolvedTeamsCarryTheBoxScoreWithoutCallerSuppliedIds() throws {
    let fx = try fixture("pinchHitters")
    let game = gameForFixtureTeams(away: fx.away, home: fx.home, bdlIds: false)
    let vm = BoxScoreViewModel(game: game)
    let (away, home) = vm.bdlTeams(forGame: game, fromStats: fx.stats)

    // Nil join ids, exactly as the failing scenario had them. The lineup
    // split now falls back to the RESOLVED team's id rather than zero, so
    // both sides still place their substitutes.
    let bs = fx.stats.toBoxScoreResponse(
        awayTeam: away, homeTeam: home,
        awayBDLTeamId: nil, homeBDLTeamId: nil,
        lineup: fx.lineups, plateAppearances: fx.pas, isFinal: true)

    #expect(bs.teams.away.player("Ryan Vilade")?.stats_battingOrder == "401")
    #expect(bs.teams.home.player("Justin Foscue")?.stats_battingOrder == "101")
    #expect(bs.teams.away.renderedCodes.allSatisfy { $0 != nil })
    #expect(bs.teams.home.renderedCodes.allSatisfy { $0 != nil })
}


// MARK: - The acceptance case

//  TB @ TEX, 2026-09-05, game 5059902 — the SECOND of two games between
//  these teams that BDL files under that date (its `dates[]` is
//  UTC-keyed, so a 7pm ET game the evening before lands in the same
//  bucket). This is the one a reader sees under September 5, and the one
//  reported from the device as placing nothing.
//
//  BDL drops Yandy Diaz's fifth plate appearance in the 10th: 40 PA rows
//  against 41 counted, and the rotation breaks where slot 1 should be.
//  Both gates fire, so the side-wide walk refuses — and every one of the
//  three substitutes still carries its own proof.
//
//  Ground truth from the published box score: Piper batted in Hicks's
//  fourth slot, Vilade in Palacios's eighth.

@Test func theDroppedPlateAppearanceStillPlacesEverySubstitute() throws {
    let fx = try fixture("missingPARow")
    let bs = boxScore(fx, isFinal: true)
    let tb = bs.teams.away

    #expect(tb.player("Kenny Piper")?.stats_battingOrder  == "401")
    #expect(tb.player("Ryan Vilade")?.stats_battingOrder  == "801")
    #expect(tb.player("Jorge Mateo")?.stats_battingOrder  == "802")
    #expect(tb.player("Liam Hicks")?.stats_battingOrder   == "400")
    #expect(tb.player("Richie Palacios")?.stats_battingOrder == "800")

    // Piper sits under Hicks; Vilade then Mateo under Palacios, in the
    // order they entered.
    #expect(tb.renderedNames == [
        "Yandy Diaz", "Jonathan Aranda", "Junior Caminero",
        "Liam Hicks", "Kenny Piper",
        "Chandler Simpson", "Victor Mesa Jr.", "Cedric Mullins",
        "Richie Palacios", "Ryan Vilade", "Jorge Mateo",
        "Taylor Walls",
    ])
    for sub in ["Kenny Piper", "Ryan Vilade", "Jorge Mateo"] {
        let p = try #require(tb.player(sub))
        #expect(BoxScoreView.substitutionDepth(p) > 0, "\(sub) must indent")
    }
    #expect(bs.teams.home.renderedCodes.allSatisfy { $0 != nil })
}

