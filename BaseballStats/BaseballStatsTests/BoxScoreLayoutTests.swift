//
//  BoxScoreLayoutTests.swift
//  BaseballStatsTests
//
//  The box score's layout arithmetic (`BoxScoreLayout.swift`): today's widths
//  where the box score is narrow, tables that only ever widen to fill a card
//  with room to spare, and which of the three layouts each measured window gets
//  at each text size.
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

    // MARK: - Modes

    typealias Mode = BoxScoreLayout.Mode

    /// Every content width measured in the wide-layout survey (2026-10-08) and
    /// on the unfolded Duo, with what each gets at the default size, the largest
    /// standard size and the first accessibility size.
    static let measured: [(name: String, width: CGFloat, large: Mode, xxxL: Mode, ax1: Mode)] = [
        ("iPhone Duo folded",           386, .compact, .compact, .compact),
        ("iPhone 17 upright",           402, .compact, .compact, .compact),
        ("iPhone 17 Pro Max upright",   440, .compact, .compact, .compact),
        ("iPad 13 split, 1/3",          450, .compact, .compact, .compact),
        ("iPad 13 upright split, 1/2",  511, .compact, .compact, .compact),
        ("iPad 13 split, 1/2",          683, .medium,  .medium,  .medium),
        ("iPad mini upright",           744, .medium,  .medium,  .medium),
        ("iPhone 17 on its side",       750, .medium,  .medium,  .medium),
        ("emulated 800",                800, .medium,  .medium,  .medium),
        // Xcode 27.1 beta Duo simulator: the 871pt window less its 68pt status column.
        ("iPhone Duo unfolded",         803, .medium,  .medium,  .medium),
        ("iPhone 17 Pro Max on its side", 832, .wide,  .medium,  .medium),
        ("iPad 11 upright",             834, .wide,    .medium,  .medium),
        ("iPad 13 split, 2/3",          911, .wide,    .medium,  .medium),
        ("iPad 13 upright",            1032, .wide,    .medium,  .medium),
        ("iPad mini on its side",      1133, .wide,    .medium,  .medium),
        ("iPad 11 on its side",        1210, .wide,    .wide,    .medium),
        ("iPad 13 on its side",        1376, .wide,    .wide,    .medium),
    ]

    @Test func eachMeasuredWidthGetsItsMode() {
        for m in Self.measured {
            #expect(BoxScoreLayout.mode(contentWidth: m.width, typeSize: .large) == m.large, "\(m.name), default text")
            #expect(BoxScoreLayout.mode(contentWidth: m.width, typeSize: .xxxLarge) == m.xxxL, "\(m.name), xxxL")
            #expect(BoxScoreLayout.mode(contentWidth: m.width, typeSize: .accessibility1) == m.ax1, "\(m.name), AX1")
        }
    }

    /// Both cards' natural tables, their padding, the gap and the column's
    /// padding — from the tables' own column definitions at each text size.
    @Test func theTwoTeamWidthIsTheTablesNaturalWidthTwice() {
        #expect(BoxScoreLayout.twoTeamWidth(.large) == 816)        // 2 × (356 + 28) + 16 + 32
        #expect(BoxScoreLayout.twoTeamWidth(.xSmall) == 816)       // smaller text never narrows a column
        #expect(abs(BoxScoreLayout.twoTeamWidth(.xLarge)! - 934.67) < 0.01)
        #expect(abs(BoxScoreLayout.twoTeamWidth(.xxLarge)! - 1053.33) < 0.01)
        #expect(BoxScoreLayout.twoTeamWidth(.xxxLarge) == 1172)
        for ax in DynamicTypeSize.allCases where ax.isAccessibilitySize {
            #expect(BoxScoreLayout.twoTeamWidth(ax) == nil, "\(ax): always one team")
        }
        // Exactly at the cutoff, each card holds its natural table and no more.
        let inner = BoxScoreLayout.teamCardInner(contentWidth: 816, mode: .wide)
        #expect(inner == BoxTableColumns.natural(name: BoxScoreLayout.nameColumn).width)
        #expect(BoxScoreLayout.mode(contentWidth: 815.9, typeSize: .large) == .medium)
    }

    /// The arithmetic agrees with the cards the phone's layout measures
    /// (NameColumnTests: iPhone 17 342, Pro Max 380).
    @Test func theCardWidthsMatchTheMeasuredOnes() {
        #expect(BoxScoreLayout.teamCardInner(contentWidth: 402, mode: .compact) == 342)
        #expect(BoxScoreLayout.teamCardInner(contentWidth: 440, mode: .compact) == 380)
        #expect(BoxScoreLayout.teamCardInner(contentWidth: 1032, mode: .medium) == 640, "the 700pt column")
        #expect(BoxScoreLayout.teamCardInner(contentWidth: 1376, mode: .wide) == 548, "1,200pt column, two cards")
        #expect(BoxScoreLayout.columnCap(.compact) == 700 && BoxScoreLayout.columnCap(.medium) == 700)
        #expect(BoxScoreLayout.columnCap(.wide) == 1200)
    }

    /// Every mode reads in one order, Team Stats and Game Leaders after the
    /// teams' tables; a block with nothing to show is left out.
    @Test func theCardsComeInOneOrder() {
        #expect(BoxScoreLayout.blocks(linescore: true, note: false, statsOrLeaders: true)
                == [.linescore, .teams, .statsAndLeaders])
        #expect(BoxScoreLayout.blocks(linescore: true, note: true, statsOrLeaders: true)
                == [.linescore, .note, .teams, .statsAndLeaders])
        #expect(BoxScoreLayout.blocks(linescore: false, note: false, statsOrLeaders: false)
                == [.teams], "no empty row where neither card has anything to show")
    }

    /// The card a resize scrolls back to: the last whose top has gone under the
    /// bars; none while the first card's top is still in view.
    @Test func theTopCardIsTheOneBeingRead() {
        #expect(BoxScoreLayout.topCard(["header": 16, "linescore": 200, "teams": 420]) == nil)
        #expect(BoxScoreLayout.topCard(["header": -300, "linescore": -110, "teams": 90]) == "linescore")
        #expect(BoxScoreLayout.topCard(["header": -900, "linescore": -700, "teams": -40, "statsAndLeaders": 800]) == "teams")
        #expect(BoxScoreLayout.topCard([:]) == nil)
    }
}
