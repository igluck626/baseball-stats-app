//
//  ScoresGrid.swift
//  BaseballStats
//
//  How many game cards the Scores list sets side by side, from the list's
//  measured width — never the device, orientation or size class.
//

import CoreGraphics

enum ScoresGrid {
    /// A card is never narrower than this — an iPhone's card is 370pt — or
    /// wider than `maxCard`, which keeps a score near its team.
    static let minCard: CGFloat = 340
    static let maxCard: CGFloat = 460
    static let gap: CGFloat = 12
    /// The list's horizontal padding, each side.
    static let padding: CGFloat = 16
    static let maxColumns = 3

    /// One column under 600pt, and wherever two `minCard` cards don't fit
    /// (today's list, centred at the reading width); otherwise as many as fit,
    /// up to `maxColumns`.
    static func columns(width: CGFloat) -> Int {
        guard width >= 600 else { return 1 }
        let fit = Int((width - 2 * padding + gap) / (minCard + gap))
        return max(1, min(maxColumns, fit))
    }

    /// The widest the list's content gets with `n` columns: the reading width
    /// for one, `n` cards at `maxCard` plus their gaps and padding for more.
    /// The date bar above the list takes the same width.
    static func contentCap(columns n: Int) -> CGFloat {
        guard n > 1 else { return ReadableContentWidth.standard }
        return CGFloat(n) * maxCard + CGFloat(n - 1) * gap + 2 * padding
    }
}

extension ScoresGrid {
    /// One entry of the Scores list: a section header, a game, or an empty cell
    /// that finishes a row so the next header starts on its own line.
    enum Row<Item: Identifiable>: Identifiable {
        case header(String)
        case item(Item)
        case spacer(section: String, index: Int)

        enum ID: Hashable {
            case header(String)
            case item(Item.ID)
            case spacer(String, Int)
        }

        var id: ID {
            switch self {
            case .header(let title): return .header(title)
            case .item(let item): return .item(item.id)
            case .spacer(let section, let index): return .spacer(section, index)
            }
        }
    }

    /// The whole slate as ONE sequence, for ONE ForEach: each non-empty section's
    /// header, then its items. With `columns` > 1, empty cells fill out the
    /// header's row and the section's last row, so every header starts a row.
    ///
    /// One ForEach is what keeps a game's identity when it changes section: a
    /// view's identity is its place in a particular ForEach plus its id, so a
    /// game moving from a Live ForEach to a Completed one became a new view —
    /// even inside one container, and the list did that before the grid. Here
    /// it keeps its id in the same ForEach, so its state survives and the move
    /// animates.
    static func rows<Item: Identifiable>(sections: [(title: String, items: [Item])],
                                         columns n: Int) -> [Row<Item>] {
        var rows: [Row<Item>] = []
        for section in sections where !section.items.isEmpty {
            rows.append(.header(section.title))
            var spacer = 0
            func pad(_ count: Int) {
                for _ in 0..<count { rows.append(.spacer(section: section.title, index: spacer)); spacer += 1 }
            }
            pad(n - 1)
            rows += section.items.map { .item($0) }
            pad((n - section.items.count % n) % n)
        }
        return rows
    }
}
