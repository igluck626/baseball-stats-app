//
//  ScoresGridTests.swift
//  BaseballStatsTests
//
//  The Scores grid (`ScoresGrid`): how many game cards sit side by side at
//  every measured content width, and the one sequence — headers, games, empty
//  cells — that a single ForEach draws, so a game keeps its identity when it
//  changes section. Widths are content widths measured on devices and splits.
//

import CoreGraphics
import Testing
@testable import BaseballStats

struct ScoresGridTests {

    @Test(arguments: [
        (402, 1),   // iPhone 17 upright
        (440, 1),   // Pro Max upright
        (450, 1), (511, 1),   // the narrow splits
        (600, 1),   // medium starts, but two cards don't fit yet
        (683, 1),   // iPad 13 half split
        (723, 1),   // one point short of two 340pt cards
        (724, 2),   // two cards fit
        (744, 2),   // iPad mini upright
        (750, 2),   // iPhone 17 on its side
        (803, 2),   // unfolded Duo
        (832, 2),   // Pro Max on its side
        (834, 2),   // iPad 11 upright
        (1032, 2),  // iPad 13 upright
        (1075, 2), (1076, 3),   // the third column's edge
        (1133, 3),  // iPad mini on its side
        (1210, 3),  // iPad 11 on its side
        (1376, 3),  // iPad 13 on its side: still three, never four
    ])
    func columnsAtEachWidth(width: Int, expected: Int) {
        #expect(ScoresGrid.columns(width: CGFloat(width)) == expected, "\(width)pt")
    }

    @Test func aCardStaysBetween340And460() {
        for w in stride(from: 600, through: 1376, by: 1) {
            let width = CGFloat(w), n = ScoresGrid.columns(width: width)
            guard n > 1 else { continue }
            let content = min(width, ScoresGrid.contentCap(columns: n))
            let card = (content - 2 * ScoresGrid.padding - CGFloat(n - 1) * ScoresGrid.gap) / CGFloat(n)
            #expect(card >= ScoresGrid.minCard && card <= ScoresGrid.maxCard, "\(w)pt: \(n) cards of \(card)")
        }
    }

    @Test func oneColumnIsTodaysReadingWidth() {
        #expect(ScoresGrid.contentCap(columns: 1) == ReadableContentWidth.standard)
        #expect(ScoresGrid.contentCap(columns: 2) == CGFloat(2 * 460 + 12 + 32))
        #expect(ScoresGrid.contentCap(columns: 3) == CGFloat(3 * 460 + 24 + 32))
    }
}

struct ScoresRowsTests {
    struct G: Identifiable { let id: Int }
    typealias Row = ScoresGrid.Row<G>

    private func describe(_ rows: [Row]) -> [String] {
        rows.map {
            switch $0 {
            case .header(let t): return t
            case .item(let g): return "\(g.id)"
            case .spacer: return "_"
            }
        }
    }

    @Test func oneColumnIsTheListAsItWas() {
        // Headers and games in order, no empty cells: on a phone the grid lays
        // out exactly the single list it replaces.
        let rows = ScoresGrid.rows(sections: [("Live", [G(id: 1)]), ("Upcoming", []), ("Completed", [G(id: 2), G(id: 3)])],
                                   columns: 1)
        #expect(describe(rows) == ["Live", "1", "Completed", "2", "3"])
    }

    @Test func everyHeaderStartsARow() {
        let rows = ScoresGrid.rows(sections: [("Live", [G(id: 1)]), ("Completed", [G(id: 2), G(id: 3), G(id: 4), G(id: 5)])],
                                   columns: 3)
        #expect(describe(rows) == ["Live", "_", "_", "1", "_", "_",
                                   "Completed", "_", "_", "2", "3", "4", "5", "_", "_"])
        for (i, row) in rows.enumerated() {
            if case .header = row { #expect(i % 3 == 0, "header at \(i)") }
        }
        let two = ScoresGrid.rows(sections: [("Live", [G(id: 1), G(id: 2)]), ("Completed", [G(id: 3)])], columns: 2)
        #expect(describe(two) == ["Live", "_", "1", "2", "Completed", "_", "3", "_"])
    }

    @Test func aGameKeepsItsIDWhenItChangesSection() {
        // The ids are what ONE ForEach keys on: the game that finished has the
        // same id under Completed as it had under Live, and no id repeats.
        let live = ScoresGrid.rows(sections: [("Live", [G(id: 7)]), ("Completed", [G(id: 2)])], columns: 2)
        let done = ScoresGrid.rows(sections: [("Live", []), ("Completed", [G(id: 7), G(id: 2)])], columns: 2)
        #expect(live.contains { $0.id == .item(7) } && done.contains { $0.id == .item(7) })
        for rows in [live, done] { #expect(Set(rows.map(\.id)).count == rows.count, "ids are unique") }
    }
}
