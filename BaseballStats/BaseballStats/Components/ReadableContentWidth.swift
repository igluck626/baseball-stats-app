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
    /// anywhere scrolls.
    ///
    /// ⚠️ A NO-OP ON A PHONE, BY CONSTRUCTION. Every phone is narrower than the
    /// cap, so the inner frame never constrains anything, and the outer frame
    /// only re-states the width the ScrollView already proposes. Phone layouts
    /// are pixel-identical with and without it; that was checked by screenshot.
    func readableContentWidth(_ maxWidth: CGFloat = ReadableContentWidth.standard) -> some View {
        frame(maxWidth: maxWidth)
            .frame(maxWidth: .infinity)
    }
}

enum ReadableContentWidth {
    /// About the width of a large phone in landscape: wide enough for the
    /// box-score and game-log tables, narrow enough to read across.
    static let standard: CGFloat = 700
}
