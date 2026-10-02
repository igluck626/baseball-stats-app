//
//  GameRecords.swift
//  BaseballStats
//
//  `GET /games/records?date=` — each team's record ENTERING and AFTER every
//  regular-season game on a date, keyed by the card's own `gamePk`. The Scores
//  cards and the box-score header print "(W-L)" from this, so a 1986 game shows
//  the 1986 record as of that game rather than today's standings.
//

import Foundation

struct GameRecordsResponse: Decodable {
    let date: String
    let games: [GameRecordEntry]
}

struct GameRecordEntry: Decodable, Equatable {
    struct WL: Decodable, Equatable {
        let w: Int
        let l: Int
    }

    struct Side: Decodable, Equatable {
        let team: String?
        let before: WL?
        let after: WL?
    }

    let gamePk: Int
    let final: Bool
    let postseason: Bool
    /// False for a forfeit that was never played — it counts, but has no card.
    let played: Bool
    /// "V" / "H" when the game was forfeited; the card keeps the field score.
    let forfeit: String?
    let away: Side
    let home: Side

    var isForfeit: Bool { forfeit != nil }

    enum CodingKeys: String, CodingKey {
        case gamePk = "game_pk"
        case final, postseason, played, forfeit, away, home
    }
}

/// The display rules, in one place so every card and the box-score header
/// agree, and so they are testable without a view.
enum GameRecordDisplay {
    /// The "(W-L)" a card shows for one side, or nil for none.
    ///
    /// - A FINAL card shows the record AFTER the game. When the server has not
    ///   caught up yet (its current-season refresh runs every few minutes, a
    ///   card can go final first), it is the record entering the game moved by
    ///   this game's result as the card shows it — never the pre-game record
    ///   under a final score. A card with no winner (a tie) keeps it unchanged.
    /// - A LIVE or SCHEDULED card shows the record ENTERING the game.
    /// - A POSTSEASON game shows none: its card carries the series line instead.
    /// - No entry (the records call failed or is still loading): none.
    static func record(_ entry: GameRecordEntry?, home: Bool, cardIsFinal: Bool,
                       wonOnCard: Bool?) -> GameRecordEntry.WL? {
        guard let entry, !entry.postseason else { return nil }
        let side = home ? entry.home : entry.away
        guard cardIsFinal else { return side.before }
        if let after = side.after { return after }
        guard let before = side.before else { return nil }
        switch wonOnCard {
        case true?:  return .init(w: before.w + 1, l: before.l)
        case false?: return .init(w: before.w, l: before.l + 1)
        case nil:    return before
        }
    }

    /// The date to ask `/games/records` for a game's record: the date its card
    /// is listed under. A Retrosheet game's `gameDate` is its date at midnight
    /// UTC — read as an instant it lands on the previous evening in American
    /// time zones — so the date is taken from the string; a balldontlie game's
    /// is its start time, whose EASTERN date is the one the slate and the
    /// endpoint both key on. Returned as local midnight of that y-m-d, which
    /// `APIClient` formats back to the same y-m-d.
    static func recordsDate(for game: Game, calendar: Calendar = .current) -> Date? {
        let c: DateComponents
        if game.gamePk < 0 {
            let parts = game.gameDate.prefix(10).split(separator: "-").compactMap { Int($0) }
            guard parts.count == 3 else { return nil }
            c = DateComponents(year: parts[0], month: parts[1], day: parts[2])
        } else {
            guard let start = game.startDate,
                  let eastern = TimeZone(identifier: "America/New_York") else { return nil }
            var et = Calendar(identifier: .gregorian)
            et.timeZone = eastern
            c = et.dateComponents([.year, .month, .day], from: start)
        }
        return calendar.date(from: c)
    }

    /// "(93-68)".
    static func text(_ wl: GameRecordEntry.WL?) -> String? {
        wl.map { "(\($0.w)-\($0.l))" }
    }
}
