//
//  ReadableContentWidth.swift
//  BaseballStats
//
//  One reading width for scrolling screens on wide windows.
//

import SwiftUI

extension View {
    /// Caps a scroll view's content at a comfortable reading width and centres
    /// it, so a 13-inch iPad shows a column of cards rather than cards stretched
    /// a thousand points wide with their numbers at the far edge.
    ///
    /// Apply it to the content INSIDE the ScrollView, not to the ScrollView: the
    /// scroll surface (and its indicator) still spans the window, so a drag
    /// anywhere scrolls. A `List` is its own scroll view and has no inside: use
    /// `readableListWidth()` on it instead.
    ///
    /// ⚠️ A NO-OP ON A PHONE HELD UPRIGHT, BY CONSTRUCTION. An upright phone is
    /// narrower than the cap, so the inner frame never constrains anything, and
    /// the outer frame only re-states the width the ScrollView already proposes;
    /// those layouts are pixel-identical with and without it, checked by
    /// screenshot. A phone on its side is wider (an iPhone 17 in landscape is
    /// 750pt), so there it caps like any wide window.
    func readableContentWidth(_ maxWidth: CGFloat = ReadableContentWidth.standard) -> some View {
        frame(maxWidth: maxWidth)
            .frame(maxWidth: .infinity)
    }

    /// `readableContentWidth` for a `List`: the list still spans the window, so
    /// it scrolls from anywhere, and horizontal content margins hold its rows
    /// within the reading width — from the list's measured width, never the
    /// device. Narrower than the cap, the margins are the system's own (nil), so
    /// an upright phone is unchanged.
    func readableListWidth(_ maxWidth: CGFloat = ReadableContentWidth.standard) -> some View {
        modifier(ReadableListWidth(maxWidth: maxWidth))
    }
}

private struct ReadableListWidth: ViewModifier {
    let maxWidth: CGFloat
    @State private var width: CGFloat = 0

    func body(content: Content) -> some View {
        content
            .contentMargins(.horizontal, ReadableContentWidth.listMargin(width: width, maxWidth: maxWidth),
                            for: .scrollContent)
            .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { width = $0 }
    }
}

enum ReadableContentWidth {
    /// About the width of a large phone in landscape: wide enough for the
    /// box-score and game-log tables, narrow enough to read across.
    static let standard: CGFloat = 700

    /// The horizontal content margin that keeps an inset-grouped list's cards
    /// inside a `maxWidth` column centred in `width`: half the spare width, plus
    /// the 16pt a phone's list leaves beside its cards, so the cards line up with
    /// content padded 16 inside `readableContentWidth`. nil — the system's own
    /// margins — when the list is no wider than the column.
    static func listMargin(width: CGFloat, maxWidth: CGFloat) -> CGFloat? {
        width > maxWidth ? (width - maxWidth) / 2 + 16 : nil
    }
}
