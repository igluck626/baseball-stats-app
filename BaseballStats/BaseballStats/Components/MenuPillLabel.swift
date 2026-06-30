//
//  MenuPillLabel.swift
//  BaseballStats
//
//  The app's standard menu-pill label — a glass capsule with a value and a
//  trailing chevron, used as the `label:` inside a `Menu { Picker { … } }`.
//  Shared so filter dropdowns stay visually identical across screens
//  (Leaderboards' stat picker, Award Voting's year/league pickers, …).
//
//  Presentational only: pass the already-resolved display text. Set
//  `showsChevron: false` for a single-option, non-interactive use (a static
//  label that still reads as part of the same control family).
//

import SwiftUI

struct MenuPillLabel: View {
    let text: String
    var showsChevron: Bool = true

    var body: some View {
        HStack(spacing: 4) {
            Text(text)
                .font(.subheadline.weight(.semibold))
            if showsChevron {
                Image(systemName: "chevron.down")
                    .font(.caption2.weight(.semibold))
            }
        }
        // Use the explicit primary COLOR (not the hierarchical `.primary`
        // shape style): inside a Menu, SwiftUI sets the label's base style to
        // the accent tint, so `.foregroundStyle(.primary)` would resolve to
        // accent blue. `.tint(.primary)` neutralizes the menu tint, and
        // `.foregroundColor(.primary)` pins the text/chevron to the label
        // color — so the pill is primary in any context with no per-call-site
        // override needed.
        .tint(.primary)
        .foregroundColor(.primary)
        .padding(.horizontal, 12)
        .padding(.vertical, 7)
        .glassEffect(.regular, in: Capsule())
    }
}
