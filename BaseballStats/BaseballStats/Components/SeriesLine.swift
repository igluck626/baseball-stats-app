//
//  SeriesLine.swift
//  BaseballStats
//
//  One caption line of postseason series state under a game — "AL Wild Card ·
//  Game 2 · BOS leads 1-0", "BOS wins 2-1". Renders NOTHING for a regular-
//  season game, an unknown game, or when the series endpoint failed, so a
//  card with no line lays out exactly as it did before.
//
//  A single Text, never an HStack of Texts: see the Dynamic Type note on
//  metric rows — separate Texts break apart at accessibility sizes.
//

import SwiftUI

struct SeriesLine: View {
    let game: Game
    var alignment: Alignment = .leading
    /// The Home strip's 110pt card: the series state only ("BOS leads 1-0").
    var compact = false
    @ObservedObject var store: SeriesStore = .shared
    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        // The first load happens at app start / foreground (`ContentView`);
        // an EMPTY view could not trigger it — its `.task` never runs. This
        // one only keeps a line that is showing fresh.
        if !game.isRegularSeason {
            content
                .task(id: game.seasonYear) {
                    if let season = game.seasonYear { await store.load(season: season) }
                }
        }
    }

    @ViewBuilder private var content: some View {
        // The Home strip's card is a FIXED 110x100pt frame, and at accessibility
        // sizes its own rows already overflow it. A series line there only
        // pushes the score out of the frame, so the compact form steps aside
        // at AX sizes and the card lays out exactly as it did without it.
        if compact, !typeSize.isAccessibilitySize, let line = store.compactLine(for: game) {
            Text(line)
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .minimumScaleFactor(0.8)
        } else if !compact, let line = store.line(for: game) {
            Text(line)
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: alignment)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}
