//
//  BoxScoreLayout.swift
//  BaseballStats
//
//  The box score's layout as plain arithmetic: which of three layouts the
//  content width gets, what each table needs at the current text size, and how
//  it widens to fill a card with room to spare. No view measures its own text;
//  the inputs are widths and the Dynamic Type size, so every number here is a
//  unit test.
//

import SwiftUI
import UIKit

enum BoxScoreLayout {
    /// Below this content width the box score is laid out exactly as it always
    /// was on a phone held upright: fixed columns, nothing stretched.
    static let compactBelow: CGFloat = 600

    /// Three layouts, chosen by the content's measured width and the text size
    /// ONLY — never the device, idiom, orientation or size class. A Pro Max on
    /// its side and an iPad in a split view at the same width get the same box
    /// score.
    enum Mode: Equatable {
        /// Under `compactBelow`: the phone layout, unchanged.
        case compact
        /// One team at a time with the picker, in the centred reading column,
        /// tables and linescore widened to their cards.
        case medium
        /// Both teams side by side, away on the left; no picker. Only when both
        /// tables fit at their natural width at the current text size.
        case wide
    }

    /// The paddings the box score lays out with, so this arithmetic and the view
    /// agree: the scroll column's on each side, a card's on each side, and the
    /// gap between cards.
    static let columnPadding: CGFloat = 16
    static let cardPadding: CGFloat = 14
    static let cardSpacing: CGFloat = 16

    /// The reading column's cap when both teams are shown.
    static let wideColumn: CGFloat = 1200

    /// The name column a table starts from wherever the box score isn't compact
    /// (`BoxScoreView.nameColumnWidth` keeps the phone's 110).
    static let nameColumn: CGFloat = 140

    /// The content width at which both teams fit at this text size: two cards,
    /// each the natural table (`BoxTableColumns.natural`, the same widths the
    /// tables draw with) plus its padding, the gap between them, and the
    /// column's padding. nil at the accessibility sizes, which always show one
    /// team: there a table is wider than half of any screen.
    static func twoTeamWidth(_ size: DynamicTypeSize) -> CGFloat? {
        guard !size.isAccessibilitySize else { return nil }
        let table = BoxTableColumns.natural(name: nameColumn, textScale: textScale(size)).width
        return 2 * (table + 2 * cardPadding) + cardSpacing + 2 * columnPadding
    }

    static func mode(contentWidth: CGFloat, typeSize: DynamicTypeSize) -> Mode {
        if contentWidth < compactBelow { return .compact }
        if let two = twoTeamWidth(typeSize), min(contentWidth, wideColumn) >= two { return .wide }
        return .medium
    }

    /// The reading column's cap for a mode: today's 700pt, or 1,200 for two teams.
    static func columnCap(_ mode: Mode) -> CGFloat {
        mode == .wide ? wideColumn : ReadableContentWidth.standard
    }

    /// A team card's width inside its padding, from the content width alone —
    /// what `mode` assumes a table gets. (The view measures the real card.)
    static func teamCardInner(contentWidth: CGFloat, mode: Mode) -> CGFloat {
        let column = min(contentWidth, columnCap(mode)) - 2 * columnPadding
        return (mode == .wide ? (column - cardSpacing) / 2 : column) - 2 * cardPadding
    }

    /// The cards below the header, in order, the same in every mode: linescore,
    /// the ordering note, the teams (the picker and one team's tables, or both
    /// teams side by side), then Team Stats with Game Leaders (side by side when
    /// wide). Each block keeps its identity across modes, so a card's state
    /// survives a resize and the scroll position can anchor to it.
    enum Block: String, CaseIterable {
        case linescore, note, teams, statsAndLeaders
    }

    /// The card the reader is in: the last whose top edge has scrolled to or
    /// above the top of the visible area (`tops`, each card's top measured from
    /// there). nil while the first card's top is still in view — nothing to keep.
    static func topCard(_ tops: [String: CGFloat]) -> String? {
        tops.filter { $0.value <= 1 }.max { $0.value < $1.value }?.key
    }

    static func blocks(linescore: Bool, note: Bool, statsOrLeaders: Bool) -> [Block] {
        var out: [Block] = []
        if linescore { out.append(.linescore) }
        if note { out.append(.note) }
        out.append(.teams)
        if statsOrLeaders { out.append(.statsAndLeaders) }
        return out
    }

    /// How much wider the tables' text is at this Dynamic Type size than at the
    /// default: caption's point size there over its size at the default (the
    /// system's own type table — not a measurement of anything on screen). Never
    /// below 1, so the smaller sizes keep the default widths: only ever widen.
    static func textScale(_ size: DynamicTypeSize) -> CGFloat {
        func caption(_ s: DynamicTypeSize) -> CGFloat {
            let traits = UITraitCollection(preferredContentSizeCategory: UIContentSizeCategory(s))
            return UIFont.preferredFont(forTextStyle: .caption1, compatibleWith: traits).pointSize
        }
        return max(1, caption(size) / caption(.large))
    }
}

/// The batting and pitching tables' columns. The two tables share the name
/// column, so IP sits under AB.
struct BoxTableColumns: Equatable {
    struct Batting: Equatable {
        var ab: CGFloat = 24, r: CGFloat = 24, h: CGFloat = 24, rbi: CGFloat = 28
        var bb: CGFloat = 24, so: CGFloat = 24, avg: CGFloat = 34, ops: CGFloat = 34
        var total: CGFloat { ab + r + h + rbi + bb + so + avg + ops }
        func scaled(_ f: CGFloat) -> Batting {
            Batting(ab: ab * f, r: r * f, h: h * f, rbi: rbi * f, bb: bb * f, so: so * f, avg: avg * f, ops: ops * f)
        }
    }
    /// Tuned to common values (IP "10.2", ERA "12.34", PC "123") at caption.
    struct Pitching: Equatable {
        var ip: CGFloat = 34, h: CGFloat = 22, r: CGFloat = 22, er: CGFloat = 22
        var bb: CGFloat = 22, so: CGFloat = 22, era: CGFloat = 38, pc: CGFloat = 28
        var total: CGFloat { ip + h + r + er + bb + so + era + pc }
        func scaled(_ f: CGFloat) -> Pitching {
            Pitching(ip: ip * f, h: h * f, r: r * f, er: er * f, bb: bb * f, so: so * f, era: era * f, pc: pc * f)
        }
    }

    var name: CGFloat
    var batting = Batting()
    var pitching = Pitching()

    /// A stretched name column stops here; past it a name only moves further
    /// from its numbers.
    static let nameCap: CGFloat = 170
    /// A stretched stat column stops at this multiple of its natural width.
    static let statCap: CGFloat = 1.8

    /// The widest of the two tables.
    var width: CGFloat { name + max(batting.total, pitching.total) }

    /// The columns at their natural widths for this text size.
    static func natural(name: CGFloat, textScale: CGFloat = 1) -> BoxTableColumns {
        BoxTableColumns(name: name * textScale,
                        batting: Batting().scaled(textScale),
                        pitching: Pitching().scaled(textScale))
    }

    /// The natural columns widened to fill `inner`, a card's width inside its
    /// padding: the name column first, to `nameCap`, then the stat columns in
    /// proportion, to `statCap` times their natural width, each table to the
    /// card's edge. ONLY EVER WIDER: a card narrower than the natural table
    /// gets the natural table, and the horizontal scroll around it does the
    /// rest, as it always has.
    static func fitted(to inner: CGFloat, name: CGFloat, textScale: CGFloat = 1) -> BoxTableColumns {
        var cols = natural(name: name, textScale: textScale)
        let spare = inner - cols.width
        guard spare > 0 else { return cols }
        cols.name += min(spare, max(0, nameCap - cols.name))
        let room = inner - cols.name
        cols.batting = cols.batting.scaled(min(statCap, max(1, room / cols.batting.total)))
        cols.pitching = cols.pitching.scaled(min(statCap, max(1, room / cols.pitching.total)))
        return cols
    }
}

/// The linescore's columns: a team label, one cell per inning, a gap, then R H E
/// in cells the innings' width.
struct LinescoreColumns: Equatable {
    var label: CGFloat = 50
    var cell: CGFloat = 22
    var gap: CGFloat = 8

    /// A stretched inning cell stops here.
    static let cellCap: CGFloat = 44

    func width(innings: Int) -> CGFloat { label + cell * CGFloat(innings + 3) + gap }

    static func natural(textScale: CGFloat = 1) -> LinescoreColumns {
        LinescoreColumns(label: 50 * textScale, cell: 22 * textScale, gap: 8)
    }

    /// The inning and total cells widened to fill `inner`, up to `cellCap`
    /// (or their natural width, if the text is already wider). Only ever wider.
    static func fitted(to inner: CGFloat, innings: Int, textScale: CGFloat = 1) -> LinescoreColumns {
        var cols = natural(textScale: textScale)
        let even = (inner - cols.label - cols.gap) / CGFloat(innings + 3)
        cols.cell = max(cols.cell, min(even, max(cellCap, cols.cell)))
        return cols
    }
}
