//
//  NameColumnTests.swift
//
//  The box score's name column: 110pt on a phone held upright, as before; 30pt
//  more where the card has room, so a name fits beside the current batter's
//  marker ("M. Murakami 1B" truncated on iPad on 2026-10-08).
//

import Testing
import UIKit
@testable import BaseballStats

struct NameColumnTests {
    @Test func phonesHeldUprightKeep110() {
        #expect(BoxScoreView.nameColumnWidth(forCardInner: 342) == 110)   // iPhone 17
        #expect(BoxScoreView.nameColumnWidth(forCardInner: 380) == 110)   // iPhone 17 Pro Max
        #expect(BoxScoreView.nameColumnWidth(forCardInner: 405) == 110)
        #expect(BoxScoreView.nameColumnWidth(forCardInner: 0) == 110)     // before the first measurement
    }

    @Test func roomierCardsGet140() {
        #expect(BoxScoreView.nameColumnWidth(forCardInner: 406) == 140)
        #expect(BoxScoreView.nameColumnWidth(forCardInner: 672) == 140)   // 700pt column (iPad, iPhone landscape)
    }

    /// The arithmetic behind the threshold, with the system font at the default
    /// text size: the current batter's row is bold, preceded by a caption2 marker
    /// and 4pt of spacing, followed by 4pt and the position in caption2.
    @Test func aLongNameWithTheMarkerNeedsMoreThan110AndFitsIn140() {
        let caption = UIFont.preferredFont(forTextStyle: .caption1).pointSize
        let caption2 = UIFont.preferredFont(forTextStyle: .caption2).pointSize
        func width(_ s: String, _ size: CGFloat, _ weight: UIFont.Weight) -> CGFloat {
            (s as NSString).size(withAttributes: [.font: UIFont.systemFont(ofSize: size, weight: weight)]).width
        }
        func row(_ name: String, _ pos: String) -> CGFloat {
            caption2 + 4 + width(name, caption, .bold) + 4 + width(pos, caption2, .regular)
        }
        #expect(row("J. Cronenworth", "2B") > 110)
        #expect(row("J. Cronenworth", "2B") <= 140)
        #expect(row("M. Murakami", "1B") <= 140)
    }
}
