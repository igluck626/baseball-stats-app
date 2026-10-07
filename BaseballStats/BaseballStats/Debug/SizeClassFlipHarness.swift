//
//  SizeClassFlipHarness.swift
//  BaseballStats
//
//  DEBUG only — this whole file, and its one call site in BaseballStatsApp,
//  compile out of Release builds. Lets a UI test flip the window's horizontal
//  size class on command, to check that per-window state survives the flip.
//  See `SceneStateFlipUITests`.
//

#if DEBUG
import SwiftUI

extension View {
    /// Under the `-SizeClassFlipHarness` launch argument, adds a button that
    /// flips this view's horizontal size class between compact and regular.
    /// Without it — every normal debug launch — the view is returned as it is.
    @ViewBuilder
    func sizeClassFlipHarness() -> some View {
        if ProcessInfo.processInfo.arguments.contains("-SizeClassFlipHarness") {
            modifier(SizeClassFlipHarness())
        } else {
            self
        }
    }
}

private struct SizeClassFlipHarness: ViewModifier {
    @State private var regular = false

    func body(content: Content) -> some View {
        content
            // A changed VALUE, not a branch: what's under test is whether the
            // app's own views keep their state when the size class changes.
            .environment(\.horizontalSizeClass, regular ? .regular : .compact)
            .overlay(alignment: .bottomLeading) {
                Button(regular ? "regular" : "compact") { regular.toggle() }
                    .font(.caption.monospaced())
                    .padding(6)
                    .background(.yellow)
                    .padding(.leading, 8)
                    .padding(.bottom, 120)
                    .accessibilityIdentifier("harness.flipSizeClass")
            }
    }
}
#endif
