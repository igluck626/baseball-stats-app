//
//  BDLRetroMatch.swift
//  BaseballStats
//
//  Pairs a Retrosheet-served game with BDL's row for the same game.
//

import Foundation

/// Attaches BDL's game id (and team ids) to games served by our own endpoint.
///
/// WHY THIS EXISTS. For 2000-2025 the box score is built from our Retrosheet
/// tables — BDL has no lineup for those seasons — but PLAY-BY-PLAY is BDL's
/// alone and is addressed by BDL's game id, which a synthetic negative
/// `gamePk` cannot supply. So the two lists for a given day are matched once,
/// at slate load, where the whole day is in hand. That is also the only place
/// a doubleheader can be resolved, because resolving it needs both halves.
///
/// FAILURE IS SAFE AND MUST STAY SAFE. An unmatched game keeps its Retrosheet
/// box score, lineup, positions, decisions, season lines and linescore, and
/// loses only the plays list and the colour swatch. Nothing here can fail the
/// box score, because nothing here feeds it.
enum BDLRetroMatch {

    /// Retrosheet home-team code → BDL abbreviation.
    ///
    /// ⚠️ THIS IS NOT A GENERAL TEAM TABLE, and it is the fourth mapping in
    /// this app for a reason. `TeamNames` maps MLBAM ids to display names,
    /// `BDLTeamIds` maps our team keys to BDL ids, and `team_seasons` server-
    /// side maps Retrosheet codes to the club's contemporary name. NONE of
    /// them joins a Retrosheet CODE to a BDL ABBREVIATION, which is what
    /// matching two same-day slates needs.
    ///
    /// Only the codes that DIFFER are listed; the rest are identical in both
    /// systems and fall through unchanged, so this stays a list of exceptions
    /// rather than a table to keep in sync.
    ///
    /// ⚠️ THE LAST THREE ARE FRANCHISE HISTORY, AND THEY ARE THE ONES A
    /// SPOT-CHECK MISSES. Retrosheet writes the code the club used AT THE
    /// TIME; BDL writes the club's CURRENT identity for every season it
    /// serves. So BDL calls the 2004 Montreal Expos "Washington Nationals",
    /// and the 2005 Florida Marlins "MIA". Building this list from one recent
    /// season's codes yields twelve entries and silently loses 1,452 games —
    /// every Marlins home game before 2012, every Expos home game, and the
    /// 2025 Athletics. It was built that way first, and the misses are how it
    /// was found. The list below came from enumerating every home code in
    /// 2000-2025, not from a sample.
    static let retroToBDL: [String: String] = [
        "ANA": "LAA",   // Angels — Anaheim / Los Angeles of Anaheim
        "CHA": "CHW",   // White Sox
        "CHN": "CHC",   // Cubs
        "KCA": "KC",    // Royals
        "LAN": "LAD",   // Dodgers
        "NYA": "NYY",   // Yankees
        "NYN": "NYM",   // Mets
        "SDN": "SD",    // Padres
        "SFN": "SF",    // Giants
        "SLN": "STL",   // Cardinals
        "TBA": "TB",    // Rays
        "WAS": "WSH",   // Nationals
        "FLO": "MIA",   // Marlins, 2000-2011 — Florida before the rename
        "MON": "WSH",   // Expos, 2000-2004 — BDL files them under the Nationals
        "ATH": "OAK",   // Athletics, 2025 — Retrosheet dropped the city, BDL did not
    ]

    static func bdlAbbreviation(forRetro code: String) -> String {
        retroToBDL[code.uppercased()] ?? code.uppercased()
    }

    /// One disagreement between the two ways of identifying a doubleheader
    /// half. Surfaced rather than swallowed — see `match`.
    struct Discrepancy {
        let retroGamePk: Int
        let bdlGameId: Int
        let retroScore: (away: Int?, home: Int?)
        let bdlScore: (away: Int?, home: Int?)
    }

    /// Pair each Retrosheet game with the BDL game for the same matchup.
    ///
    /// THE KEY IS START TIME, NOT SCORE. Two halves of a doubleheader share a
    /// date and both clubs and differ only in when they began, so ordering
    /// BDL's same-matchup games by start time reproduces Retrosheet's trailing
    /// game number (0 for a single game, 1 then 2 for a doubleheader). Score
    /// would ALMOST work — eight of the 795 doubleheaders in this span ended
    /// with identical scores in both halves — and "almost" is the wrong
    /// property for an identity.
    ///
    /// The score is still compared, as a CROSS-CHECK: a pairing where the
    /// clock and the scoreboard disagree is reported through `onDiscrepancy`
    /// rather than silently accepted, because it means one of the two
    /// assumptions is wrong and that is worth knowing.
    /// BDL games belonging to `localDate`'s BASEBALL DAY.
    ///
    /// ⚠️ BDL GROUPS BY UTC, WHICH IS NOT THE DAY THE GAME WAS PLAYED. A night
    /// game on the west coast starts after midnight UTC, so `dates[]=D` omits
    /// it and lists the previous evening's west-coast games instead. Matching
    /// our local-dated slate against that bucket loses about one game in six.
    /// (The same trap cost `/admin/repair-attributed` 30% of its corrections
    /// earlier, for the same reason.)
    ///
    /// The window runs from noon UTC on the day to noon UTC the next, which
    /// contains exactly one local day of North American baseball: first pitch
    /// falls between roughly 16:00Z and 08:00Z the following morning for every
    /// zone from Eastern to Pacific. It needs no per-park timezone table and
    /// no score, and it cannot admit two games of the same series, which a
    /// plain two-day concatenation would.
    static func baseballDay(_ bdlGames: [BDLGame], localDate: Date) -> [BDLGame] {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = TimeZone(identifier: "UTC") ?? .current
        let day = cal.startOfDay(for: localDate)
        guard let next = cal.date(byAdding: .day, value: 1, to: day) else { return bdlGames }
        let f = ISO8601DateFormatter()
        f.timeZone = TimeZone(identifier: "UTC")
        let lo = String(f.string(from: day).prefix(10)) + "T12:00:00"
        let hi = String(f.string(from: next).prefix(10)) + "T12:00:00"
        return bdlGames.filter { $0.date >= lo && $0.date < hi }
    }

    static func match(
        retroGames: [Game],
        bdlGames: [BDLGame],
        onDiscrepancy: ((Discrepancy) -> Void)? = nil,
    ) -> [Game] {
        guard !bdlGames.isEmpty else { return retroGames }

        // Bucket BDL's day by matchup, each bucket ordered by first pitch.
        var buckets: [String: [BDLGame]] = [:]
        for g in bdlGames {
            let key = matchupKey(away: g.awayTeam.abbreviation,
                                 home: g.homeTeam.abbreviation)
            buckets[key, default: []].append(g)
        }
        for k in buckets.keys {
            buckets[k]?.sort { $0.date < $1.date }
        }

        // Consume in the same order on our side, so the nth of a matchup here
        // meets the nth there. Our slate is sorted by game id, whose trailing
        // digit is the game number, so the orders already agree.
        var consumed: [String: Int] = [:]
        return retroGames.map { game -> Game in
            let awayRetro = game.teams.away.team.abbreviation ?? ""
            let homeRetro = game.teams.home.team.abbreviation ?? ""
            let key = matchupKey(away: bdlAbbreviation(forRetro: awayRetro),
                                 home: bdlAbbreviation(forRetro: homeRetro))
            guard let bucket = buckets[key] else {
                return game          // no counterpart: plays and swatch absent
            }
            var taken = consumed[key, default: 0]
            guard taken < bucket.count else { return game }

            let ourAway = game.teams.away.score
            let ourHome = game.teams.home.score
            func agrees(_ g: BDLGame) -> Bool {
                guard let a = ourAway, let h = ourHome,
                      let ta = g.awayTeamData?.runs, let th = g.homeTeamData?.runs
                else { return false }
                return a == ta && h == th
            }

            // START TIME IS STILL THE KEY: take the earliest counterpart not
            // already used. The score only breaks a tie the clock cannot,
            // and there is one — BDL sometimes lists the SAME game twice.
            // SD@CHC on 2005-04-13 appears as ids 3527 and 3528, identical
            // timestamp and identical score, alongside the real nightcap; in
            // pure time order the second half of our doubleheader takes the
            // duplicate opener and shows the wrong game's plays. Skipping
            // ahead to a candidate whose score agrees steps over the copy.
            var chosen = taken
            if !agrees(bucket[taken]) {
                if let better = (taken..<bucket.count).first(where: { agrees(bucket[$0]) }) {
                    chosen = better
                }
            }
            // Everything up to and including the chosen row is spent, so a
            // skipped duplicate cannot be handed to the next game either.
            taken = chosen + 1
            consumed[key] = taken
            let bdlGame = bucket[chosen]

            if let a = ourAway, let h = ourHome,
               let ta = bdlGame.awayTeamData?.runs, let th = bdlGame.homeTeamData?.runs,
               a != ta || h != th {
                onDiscrepancy?(Discrepancy(
                    retroGamePk: game.gamePk, bdlGameId: bdlGame.id,
                    retroScore: (a, h), bdlScore: (ta, th)))
            }
            return game.attaching(bdlGame: bdlGame)
        }
    }

    private static func matchupKey(away: String?, home: String?) -> String {
        "\(away?.uppercased() ?? "")@\(home?.uppercased() ?? "")"
    }
}

extension Game {
    /// Copy carrying BDL's ids for the same game. Touches ONLY the three id
    /// fields: the scores, teams, linescore and everything the box score reads
    /// stay exactly as our own endpoint served them.
    func attaching(bdlGame: BDLGame) -> Game {
        Game(
            gamePk:        gamePk,
            gameDate:      gameDate,
            status:        status,
            teams:         teams,
            venue:         venue,
            linescore:     linescore,
            decisions:     decisions,
            bdlAwayTeamId: bdlGame.awayTeam.id,
            bdlHomeTeamId: bdlGame.homeTeam.id,
            bdlGameId:     bdlGame.id,
        )
    }
}
