//
//  BoxScoreLayout.swift
//  BaseballStats
//
//  The box score's column widths, in one place, as plain arithmetic: what each
//  table needs at the current text size, and how it widens to fill a card with
//  room to spare. No view measures its own text; the inputs are the card's width
//  and the Dynamic Type size, so every number here is a unit test.
//

import SwiftUI
import UIKit

enum BoxScoreLayout {
    /// Below this content width the box score is laid out exactly as it always
    /// was on a phone held upright: fixed columns, nothing stretched.
    static let compactBelow: CGFloat = 600

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
