//
//  YearRangeSlider.swift
//  BaseballStats
//
//  Tappable chip showing the current "YYYY – YYYY" year window
//  collapses to one line by default. Tapping expands inline to a
//  single compact row: from-year label, two side-by-side native
//  Sliders, to-year label. The left slider adjusts the lower bound,
//  the right slider adjusts the upper bound, and each binding
//  clamps so the handles can't cross.
//
//  We deliberately use two stock SwiftUI Sliders (UIKit-backed) over
//  a custom-gesture range slider — earlier custom attempts had hit-
//  test and state-tracking issues that left handles unusable. The
//  per-slider visual fills don't quite read as a single "range
//  between thumbs", but the controls are bulletproof and the chip
//  above shows the live numeric window the entire time.
//

import SwiftUI

struct YearRangeSlider: View {
    @Binding var lowerValue: Int
    @Binding var upperValue: Int
    /// Inclusive year bounds — the outer floor / ceiling for both
    /// sliders. Per-slider ranges narrow inside this to keep the two
    /// handles from crossing.
    let bounds: ClosedRange<Int>

    /// Local expansion state — preserved across re-renders so a
    /// parent VM publish doesn't reset the user's tap-to-open.
    @State private var isExpanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            chip
            if isExpanded {
                slidersRow
                    .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
        .animation(.easeInOut(duration: 0.2), value: isExpanded)
    }

    // MARK: - Chip (collapsed summary)

    /// Single-line tappable summary. Reads "Year range  1871 – 2026"
    /// with a chevron on the right indicating expansion state. Whole
    /// row is the tap target so a hurried tap on the chevron, the
    /// label, or the numbers all toggle the same way.
    private var chip: some View {
        Button {
            isExpanded.toggle()
        } label: {
            HStack(spacing: 8) {
                Text("Year range")
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(.secondary)
                Spacer()
                // `Text(verbatim:)` sidesteps the LocalizedStringKey
                // path that SwiftUI's `Text("\(year)")` uses, which
                // applies the user's locale and adds thousand
                // separators ("1,871" instead of "1871").
                Text(verbatim: "\(lowerValue) – \(upperValue)")
                    .font(.subheadline.weight(.semibold))
                    .monospacedDigit()
                    .foregroundStyle(.primary)
                Image(systemName: "chevron.down")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                    // Rotate instead of swapping symbols so SwiftUI
                    // animates the indicator continuously rather than
                    // cross-fading two glyphs.
                    .rotationEffect(.degrees(isExpanded ? 180 : 0))
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityHint(isExpanded ? "Tap to collapse" : "Tap to expand")
    }

    // MARK: - Expanded controls (single row)

    /// One HStack: from-year label, lower-handle Slider, upper-handle
    /// Slider, to-year label. The two sliders share the middle space
    /// 50/50 and each clamps against the other's value so the handles
    /// can never cross.
    private var slidersRow: some View {
        HStack(spacing: 10) {
            Text(verbatim: String(lowerValue))
                .font(.caption.weight(.semibold))
                .monospacedDigit()
                .foregroundStyle(.secondary)
                .frame(width: 44, alignment: .leading)

            Slider(value: lowerBinding, in: lowerRange, step: 1)
                .tint(.accentColor)

            Slider(value: upperBinding, in: upperRange, step: 1)
                .tint(.accentColor)

            Text(verbatim: String(upperValue))
                .font(.caption.weight(.semibold))
                .monospacedDigit()
                .foregroundStyle(.secondary)
                .frame(width: 44, alignment: .trailing)
        }
    }

    // MARK: - Bindings + per-slider ranges

    /// Double bridge for the lower slider. Setter snaps to int and
    /// clamps to `upperValue - 1` so the lower handle can never equal
    /// or exceed the upper. Also clamps to `bounds.lowerBound`
    /// defensively (Slider already honors `in:`, but a manual write
    /// through this binding could bypass it).
    private var lowerBinding: Binding<Double> {
        Binding(
            get: { Double(lowerValue) },
            set: { newValue in
                let snapped = Int(newValue.rounded())
                lowerValue = max(bounds.lowerBound,
                                 min(snapped, upperValue - 1))
            }
        )
    }

    /// Double bridge for the upper slider — symmetric to lowerBinding.
    private var upperBinding: Binding<Double> {
        Binding(
            get: { Double(upperValue) },
            set: { newValue in
                let snapped = Int(newValue.rounded())
                upperValue = min(bounds.upperBound,
                                 max(snapped, lowerValue + 1))
            }
        )
    }

    /// Lower slider's allowed range: from the global floor up to one
    /// year below the current upper value. Per-slider tight ranges
    /// (rather than `bounds` on both) make each thumb's position
    /// proportional to its own slice of the timeline, so dragging
    /// near the right edge of the lower slider feels "almost touching
    /// the upper value" rather than "halfway through the full bounds".
    /// Guards against a degenerate inverted range when the two
    /// values are adjacent — SwiftUI tolerates a zero-width
    /// ClosedRange and just renders the slider inert.
    private var lowerRange: ClosedRange<Double> {
        let lo = Double(bounds.lowerBound)
        let hi = Double(max(bounds.lowerBound, upperValue - 1))
        return lo...max(lo, hi)
    }

    private var upperRange: ClosedRange<Double> {
        let hi = Double(bounds.upperBound)
        let lo = Double(min(bounds.upperBound, lowerValue + 1))
        return min(lo, hi)...hi
    }
}
