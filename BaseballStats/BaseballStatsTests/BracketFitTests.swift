//
//  BracketFitTests.swift
//  BaseballStatsTests
//
//  The Standings bracket's fit (`BracketTreeCanvas.fitScale`): shrunk to the
//  window's measured width when its smallest text stays at 9pt or more, drawn
//  at full size and scrolled when it doesn't, never enlarged.
//

import SwiftUI
import UIKit
import Testing
@testable import BaseballStats

@MainActor
struct BracketFitTests {
    typealias Canvas = BracketTreeCanvas<Int, EmptyView>

    /// The live bracket at the default text size: four 144pt columns, 50pt
    /// gaps and 22pt insets.
    let natural: CGFloat = 770

    private func k(_ container: CGFloat, text: CGFloat = 11) -> CGFloat {
        Canvas.fitScale(container: container, natural: natural, smallestText: text)
    }

    @Test func theNaturalWidthIsTheLayouts() {
        let m = BracketMetrics.live
        #expect(m.sideInset + m.boxW / 2 + 3 * (m.boxW + m.colGap) + m.boxW / 2 + m.sideInset == natural)
    }

    @Test func aWindowWideEnoughDrawsItAsItIs() {
        #expect(k(810) == 1)
        #expect(k(1032) == 1, "iPad 13 portrait")
        #expect(k(1366) == 1, "never enlarged")
    }

    @Test func aWindowALittleTooNarrowShrinksItToFit() {
        for w: CGFloat in [744, 683] {   // iPad mini portrait, iPad 13 landscape half
            let s = k(w)
            #expect(s < 1)
            #expect(abs(natural * s + 2 * Canvas.padding - w) < 0.001, "\(w): fills the width exactly")
            #expect(s * 11 >= Canvas.minimumTextSize, "\(w): smallest text \(s * 11)pt")
        }
    }

    @Test func aWindowTooNarrowForReadableTextScrollsAtFullSize() {
        for w: CGFloat in [402, 450, 511, 600] {   // iPhone 17 upright, the splits
            #expect(k(w) == 1, "\(w)")
        }
    }

    @Test func aSmallerTextSizeStillShrinks() {
        // Below the default the 9pt floor decides, as at the default.
        let m = metrics(.medium)
        let s = Canvas.fitScale(container: 683, natural: m.natural, smallestText: m.smallest, typeSize: .medium)
        #expect(s < 1 && s * m.smallest >= Canvas.minimumTextSize, "\(s) × \(m.smallest)pt")
        #expect(k(600, text: 11) == 1, "600pt would take 11pt text to 7.9pt, so it scrolls")
    }

    /// The bracket's size at a text size, from the system's own metrics: the
    /// layout scales with `.subheadline` (`@ScaledMetric` in LiveBracketView),
    /// its smallest text is `.caption2`.
    private func metrics(_ size: UIContentSizeCategory) -> (natural: CGFloat, smallest: CGFloat) {
        let traits = UITraitCollection(preferredContentSizeCategory: size)
        let k = UIFontMetrics(forTextStyle: .subheadline).scaledValue(for: 1, compatibleWith: traits)
        return (natural * k, UIFont.preferredFont(forTextStyle: .caption2, compatibleWith: traits).pointSize)
    }

    @Test(arguments: [(UIContentSizeCategory.extraLarge, DynamicTypeSize.xLarge),
                      (.extraExtraExtraLarge, .xxxLarge),
                      (.accessibilityMedium, .accessibility1)])
    func aLargerTextSizeIsNeverShrunk(size: UIContentSizeCategory, type: DynamicTypeSize) {
        let m = metrics(size)
        for w: CGFloat in [744, 683, 1032, 1133] {   // iPad mini, 683, iPad 13 both ways
            let unruled = Canvas.fitScale(container: w, natural: m.natural, smallestText: m.smallest)
            let s = Canvas.fitScale(container: w, natural: m.natural, smallestText: m.smallest, typeSize: type)
            #expect(s == 1, "\(type) at \(w)pt: full size, scrolled (the 9pt floor alone would give \(unruled))")
        }
        // Without the rule, the 9pt floor alone would shrink it on an iPad mini.
        #expect(Canvas.fitScale(container: 744, natural: m.natural, smallestText: m.smallest) < 1)
    }

    @Test func beforeTheFirstMeasurementNothingShrinks() {
        #expect(k(0) == 1)
    }
}
