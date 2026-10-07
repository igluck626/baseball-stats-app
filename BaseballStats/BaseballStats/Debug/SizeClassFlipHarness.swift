//
//  SizeClassFlipHarness.swift
//  BaseballStats
//
//  DEBUG only — this whole file, and its two call sites (BaseballStatsApp,
//  and the Ask cover in ContentView), compile out of Release builds. Lets a UI
//  test flip the window's horizontal size class on command, to check that
//  per-window state survives the flip. See `SceneStateFlipUITests`.
//

#if DEBUG
import Combine
import SwiftUI

extension View {
    /// Under the `-SizeClassFlipHarness` launch argument, adds a button that
    /// flips this view's horizontal size class between compact and regular.
    /// Without it — every normal debug launch — the view is returned as it is.
    ///
    /// Applied at the root AND inside the Ask cover, which the root's overlay
    /// can't reach. Both share one state, so either button flips the window;
    /// `id` tells them apart (the root's stays in the tree behind the cover).
    @ViewBuilder
    func sizeClassFlipHarness(id: String = "harness.flipSizeClass") -> some View {
        if SizeClassFlipState.enabled {
            modifier(SizeClassFlipHarness(id: id))
        } else {
            self
        }
    }
}

/// One flip state for the whole window, so the root and a cover agree.
@MainActor
private final class SizeClassFlipState: ObservableObject {
    static let shared = SizeClassFlipState()
    nonisolated static var enabled: Bool {
        ProcessInfo.processInfo.arguments.contains("-SizeClassFlipHarness")
    }
    @Published var regular = false
}

private struct SizeClassFlipHarness: ViewModifier {
    let id: String
    @ObservedObject private var state = SizeClassFlipState.shared

    func body(content: Content) -> some View {
        content
            // A changed VALUE, not a branch: what's under test is whether the
            // app's own views keep their state when the size class changes.
            .environment(\.horizontalSizeClass, state.regular ? .regular : .compact)
            // Mid-height on the leading edge: clear of the nav and tab bars, and
            // above the keyboard while an Ask draft is being typed.
            .overlay(alignment: .leading) {
                Button(state.regular ? "regular" : "compact") { state.regular.toggle() }
                    .font(.caption.monospaced())
                    .padding(6)
                    .background(.yellow)
                    .padding(.leading, 8)
                    .accessibilityIdentifier(id)
            }
    }
}
#endif
