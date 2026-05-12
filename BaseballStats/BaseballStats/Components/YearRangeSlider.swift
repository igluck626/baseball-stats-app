//
//  YearRangeSlider.swift
//  BaseballStats
//
//  Two-handle range slider snapping to whole integers — used by the
//  Leaderboards screen to pick a year window for All-Time / Career
//  modes. SwiftUI has no native range slider, so this is a thin
//  custom implementation: track + highlighted segment + two draggable
//  handles in a ZStack, with the gestures resolved against a named
//  coordinate space so the math doesn't care about handle-local
//  drag positions.
//

import SwiftUI

struct YearRangeSlider: View {
    @Binding var lowerValue: Int
    @Binding var upperValue: Int
    /// Inclusive year bounds — the slider clamps to this range and
    /// uses the span to map x-position → integer year.
    let bounds: ClosedRange<Int>

    private let trackHeight: CGFloat   = 4
    private let handleDiameter: CGFloat = 26
    /// Named coordinate space pinned on the ZStack so the gesture's
    /// `location.x` is in slider-local coordinates regardless of which
    /// handle (or where on it) the user touched.
    private let coordSpace = "year-range-slider"

    var body: some View {
        GeometryReader { geo in
            let width  = geo.size.width
            let span   = CGFloat(max(1, bounds.upperBound - bounds.lowerBound))
            let lowerX = CGFloat(lowerValue - bounds.lowerBound) / span * width
            let upperX = CGFloat(upperValue - bounds.lowerBound) / span * width

            ZStack(alignment: .leading) {
                // Background track.
                Capsule()
                    .fill(Color(.systemGray5))
                    .frame(height: trackHeight)

                // Highlighted segment between the two handles. Width
                // can briefly be 0 when the handles meet — `max(0, …)`
                // keeps SwiftUI from logging a negative-width warning.
                Capsule()
                    .fill(Color.accentColor.opacity(0.85))
                    .frame(width: max(0, upperX - lowerX), height: trackHeight)
                    .offset(x: lowerX)

                handle(centerX: lowerX, width: width, isLower: true)
                handle(centerX: upperX, width: width, isLower: false)
            }
            .coordinateSpace(name: coordSpace)
        }
        // Outer frame matches the handle height — the GeometryReader
        // is otherwise infinite-tall and would steal vertical space
        // from siblings in a parent VStack.
        .frame(height: handleDiameter)
    }

    /// One draggable circular handle. `centerX` is the handle's center
    /// position along the track in slider-local coordinates; the
    /// .offset positions the handle so its center lands there.
    private func handle(centerX: CGFloat, width: CGFloat, isLower: Bool) -> some View {
        Circle()
            .fill(Color.white)
            .frame(width: handleDiameter, height: handleDiameter)
            .shadow(color: .black.opacity(0.18), radius: 2, x: 0, y: 1)
            .overlay(Circle().stroke(Color(.systemGray3), lineWidth: 0.5))
            .offset(x: centerX - handleDiameter / 2)
            // Bigger hit target than the visible circle — touch
            // can land anywhere inside the diameter without missing.
            .contentShape(Circle())
            .gesture(
                DragGesture(minimumDistance: 0, coordinateSpace: .named(coordSpace))
                    .onChanged { gesture in
                        let clamped    = max(0, min(width, gesture.location.x))
                        let proportion = Double(clamped / width)
                        let spanInt    = bounds.upperBound - bounds.lowerBound
                        // .rounded() snaps to whole years — no
                        // sub-year fractional state ever escapes the
                        // gesture handler.
                        let raw = bounds.lowerBound
                                + Int((Double(spanInt) * proportion).rounded())
                        if isLower {
                            // Clamp so the lower handle can't cross
                            // the upper one. Same logic mirrored for
                            // the upper handle below.
                            lowerValue = min(raw, upperValue)
                        } else {
                            upperValue = max(raw, lowerValue)
                        }
                    }
            )
    }
}
