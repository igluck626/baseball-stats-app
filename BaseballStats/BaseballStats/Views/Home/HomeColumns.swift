//
//  HomeColumns.swift
//  BaseballStats
//
//  Home's sections in one column, or two on a wide window: the team on the
//  left (the hero card, Roster / Injuries / History, My Players), the reading
//  on the right (Team News, Team Leaders). Decided from the measured width,
//  never the device.
//
//  `HomeColumns.layout` chooses between SwiftUI's own VStackLayout (one column,
//  exactly the VStack Home always had, so a phone is unchanged) and
//  `HomeColumnsLayout` (two), through AnyLayout — not an if/else between
//  stacks. Switching layouts by value keeps every section's identity, so a
//  resize, a rotation or a fold keeps the scroll position, the leaders'
//  Batting/Pitching choice and the favourites' edit mode.
//

import SwiftUI

enum HomeColumns {
    /// A column is never narrower than this, each section's own 16pt padding
    /// included: 368pt cards, an iPhone 17's being 370. Two of them fit the
    /// unfolded Duo (803pt).
    static let minColumn: CGFloat = 400
    /// Nor wider than this, so a 13-inch iPad's columns stay readable.
    static let maxColumn: CGFloat = 560
    static let spacing: CGFloat = 14

    enum Column { case left, right }

    static func twoColumns(width: CGFloat) -> Bool { width >= 2 * minColumn }

    /// The widest Home's content gets: the reading width in one column, two
    /// `maxColumn` columns in two.
    static func contentCap(twoColumns: Bool) -> CGFloat {
        twoColumns ? 2 * maxColumn : ReadableContentWidth.standard
    }

    /// One column: SwiftUI's VStackLayout, the stack Home always used. Two:
    /// `HomeColumnsLayout`.
    static func layout(twoColumns: Bool) -> AnyLayout {
        twoColumns ? AnyLayout(HomeColumnsLayout()) : AnyLayout(VStackLayout(spacing: spacing))
    }

    /// Where each section goes in two columns: each column takes its own
    /// sections in the order given, `spacing` apart, half the width each, a
    /// section narrower than its column centred in it.
    /// `measure(index, proposedWidth)` is a section's size at that width.
    static func frames(width: CGFloat, columns: [Column],
                       measure: (Int, CGFloat) -> CGSize) -> (frames: [CGRect], size: CGSize) {
        var frames = Array(repeating: CGRect.zero, count: columns.count)
        let colWidth = width / 2
        var bottom: [Column: CGFloat] = [.left: 0, .right: 0]
        var started: Set<Column> = []
        for (i, c) in columns.enumerated() {
            let size = measure(i, colWidth)
            let y = bottom[c, default: 0] + (started.contains(c) ? spacing : 0)
            let x = (c == .right ? colWidth : 0) + (colWidth - size.width) / 2
            frames[i] = CGRect(origin: CGPoint(x: x, y: y), size: size)
            bottom[c] = y + size.height
            started.insert(c)
        }
        return (frames, CGSize(width: width, height: max(bottom[.left] ?? 0, bottom[.right] ?? 0)))
    }
}

private struct HomeColumnKey: LayoutValueKey {
    static let defaultValue: HomeColumns.Column = .left
}

extension View {
    /// Which of Home's two columns this section takes on a wide window.
    func homeColumn(_ column: HomeColumns.Column) -> some View {
        layoutValue(key: HomeColumnKey.self, value: column)
    }
}

/// Home's two columns: News and Leaders (`.homeColumn(.right)`) on the right,
/// everything else on the left, each column in the order written.
struct HomeColumnsLayout: Layout {
    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? subviews.map { $0.sizeThatFits(.unspecified).width }.max() ?? 0
        let layout = arrange(width: width, subviews: subviews)
        return CGSize(width: proposal.width ?? layout.size.width, height: layout.size.height)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let layout = arrange(width: bounds.width, subviews: subviews)
        // Placed with exactly the proposal each was measured with — the column's
        // width, height open — so it lays out the way it measured. (A fixed
        // height shrank Team Leaders' single-line, scale-to-fit names.)
        let colWidth = bounds.width / 2
        for (subview, frame) in zip(subviews, layout.frames) {
            subview.place(at: CGPoint(x: bounds.minX + frame.minX, y: bounds.minY + frame.minY),
                          proposal: ProposedViewSize(width: colWidth, height: nil))
        }
    }

    private func arrange(width: CGFloat, subviews: Subviews) -> (frames: [CGRect], size: CGSize) {
        HomeColumns.frames(width: width, columns: subviews.map { $0[HomeColumnKey.self] }) { i, w in
            subviews[i].sizeThatFits(ProposedViewSize(width: w, height: nil))
        }
    }
}
