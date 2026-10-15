//
//  HomeColumnsTests.swift
//  BaseballStatsTests
//
//  Home's columns (`HomeColumns`): when it splits into two at every measured
//  content width, and where each section goes in two. One column is
//  SwiftUI's own VStackLayout, the stack Home always had.
//

import CoreGraphics
import Testing
@testable import BaseballStats

struct HomeColumnsTests {

    @Test(arguments: [
        (402, false), (440, false), (683, false),
        (744, false),   // iPad mini upright: one column
        (750, false),   // iPhone 17 on its side
        (799, false),
        (800, true),    // two 400pt columns
        (803, true),    // unfolded Duo
        (832, true), (834, true), (1032, true), (1133, true), (1376, true),
    ])
    func twoColumnsAtEachWidth(width: Int, expected: Bool) {
        #expect(HomeColumns.twoColumns(width: CGFloat(width)) == expected, "\(width)pt")
    }

    @Test func theCapIsTheReadingWidthOrTwoColumns() {
        #expect(HomeColumns.contentCap(twoColumns: false) == ReadableContentWidth.standard)
        #expect(HomeColumns.contentCap(twoColumns: true) == 1120)
    }

    // hero, news, leaders, tools, favourites — News and Leaders on the right.
    private let columns: [HomeColumns.Column] = [.left, .right, .right, .left, .left]
    private let heights: [CGFloat] = [340, 280, 400, 150, 140]

    @Test func twoColumnsSplitByColumnInOrder() {
        let r = HomeColumns.frames(width: 1000, columns: columns) { i, w in
            CGSize(width: w, height: heights[i])
        }
        // Left: hero, tools, favourites. Right: news, leaders.
        #expect(r.frames[0] == CGRect(x: 0, y: 0, width: 500, height: 340))
        #expect(r.frames[3] == CGRect(x: 0, y: 354, width: 500, height: 150))
        #expect(r.frames[4] == CGRect(x: 0, y: 518, width: 500, height: 140))
        #expect(r.frames[1] == CGRect(x: 500, y: 0, width: 500, height: 280))
        #expect(r.frames[2] == CGRect(x: 500, y: 294, width: 500, height: 400))
        #expect(r.size == CGSize(width: 1000, height: 694), "the taller column sets the height")
    }

    @Test func withNoNewsTheRightColumnStartsWithLeaders() {
        // News appears only once articles load; until then it isn't a section.
        let cols: [HomeColumns.Column] = [.left, .right, .left, .left]
        let r = HomeColumns.frames(width: 1000, columns: cols) { i, w in
            CGSize(width: w, height: [340, 400, 150, 140][i])
        }
        #expect(r.frames[1] == CGRect(x: 500, y: 0, width: 500, height: 400))
        #expect(r.frames[0].minY == 0 && r.frames[2].minY == 354)
    }
}
