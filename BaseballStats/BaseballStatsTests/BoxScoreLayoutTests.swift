//
//  BoxScoreLayoutTests.swift
//  BaseballStatsTests
//
//  The box score's column arithmetic (`BoxScoreLayout.swift`): today's widths
//  where the box score is narrow, and tables that only ever widen to fill a
//  card with room to spare.
//

import SwiftUI
import Testing
@testable import BaseballStats

struct BoxScoreLayoutTests {

    @Test func theNarrowColumnsAreTodays() {
        let phone = BoxTableColumns(name: 110)
        #expect(phone.batting.total == 216)
        #expect(phone.pitching.total == 210)
        #expect(phone.width == 326, "fits an iPhone 17 card (342pt inside) with OPS in view")
        #expect(LinescoreColumns().width(innings: 9) == 322)
    }

    @Test func aTableNeverNarrowsBelowItsNaturalWidth() {
        for inner in [0, 200, 326, 355.9] as [CGFloat] {
            #expect(BoxTableColumns.fitted(to: inner, name: 140) == .natural(name: 140))
        }
        #expect(LinescoreColumns.fitted(to: 100, innings: 9) == LinescoreColumns())
        // A 14-inning game is wider than the card: the natural cells, and the scroll.
        #expect(LinescoreColumns.fitted(to: 400, innings: 14) == LinescoreColumns())
    }

    /// The name column takes the first 30pt to its cap, then both tables run to
    /// the card's edge together.
    @Test func theNameGrowsFirstThenBothTablesReachTheEdge() {
        let c = BoxTableColumns.fitted(to: 464, name: 140)      // two teams on a 13-inch iPad, upright
        #expect(c.name == 170)
        #expect(abs(c.name + c.batting.total - 464) < 0.001)
        #expect(abs(c.name + c.pitching.total - 464) < 0.001)
        #expect(c.batting.ab > 24 && c.batting.ab < 24 * 1.8)

        let small = BoxTableColumns.fitted(to: 376, name: 140)  // 20pt spare: all of it to the name
        #expect(small.name == 160 && small.batting == .init())
    }

    @Test func stretchingStopsAtTheCaps() {
        let c = BoxTableColumns.fitted(to: 640, name: 140)      // one team in the 700pt column
        #expect(c.name == BoxTableColumns.nameCap)
        #expect(c.batting == BoxTableColumns.Batting().scaled(1.8))
        #expect(c.pitching == BoxTableColumns.Pitching().scaled(1.8))
        #expect(c.width < 640, "the rest stays empty rather than pushing numbers away from names")

        let ls = LinescoreColumns.fitted(to: 1140, innings: 9)
        #expect(ls.cell == LinescoreColumns.cellCap && ls.label == 50)
        let mid = LinescoreColumns.fitted(to: 500, innings: 9)
        #expect(mid.cell > 22 && mid.cell < 44 && abs(mid.width(innings: 9) - 500) < 0.001)
    }

    @Test func textScaleOnlyEverWidens() {
        #expect(BoxScoreLayout.textScale(.large) == 1)
        #expect(BoxScoreLayout.textScale(.xSmall) == 1, "smaller text keeps the default widths")
        let sizes: [DynamicTypeSize] = [.large, .xLarge, .xxLarge, .xxxLarge, .accessibility1, .accessibility3, .accessibility5]
        let scales = sizes.map(BoxScoreLayout.textScale)
        #expect(zip(scales, scales.dropFirst()).allSatisfy { $0 < $1 }, "\(scales)")
        #expect(BoxTableColumns.natural(name: 140, textScale: 1.25).width == 356 * 1.25)
    }
}
