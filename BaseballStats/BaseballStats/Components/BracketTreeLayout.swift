//
//  BracketTreeLayout.swift
//  BaseballStats
//
//  The bracket's geometry and canvas, shared by Playoff History (Lahman's
//  finished series) and the live bracket on the Standings tab (slots that may
//  still be empty). Each supplies its own box; the placement, connectors,
//  headers, league labels and bands are this one layout.
//
//  AL half on top, NL half below, both flowing left → right (WC → DS → CS)
//  into a vertically centred World Series. Positions are COMPUTED, not
//  measured, so the `Canvas` connectors line up exactly with the boxes.
//

import SwiftUI

/// Box and spacing sizes. `history` is Playoff History's, unchanged since
/// Phase 2a; the live bracket uses a taller box for its status line and
/// scales with Dynamic Type.
struct BracketMetrics {
    var boxW: CGFloat
    var boxH: CGFloat
    var colGap: CGFloat = 50
    var dsGap: CGFloat = 30          // vertical gap between a league's two DS boxes
    var leagueGap: CGFloat = 84      // vertical gap between the AL and NL halves
    var topInset: CGFloat = 92       // room for column headers + AL league label
    var sideInset: CGFloat = 22
    var bandPadX: CGFloat = 14
    var bandPadY: CGFloat = 16

    static let history = BracketMetrics(boxW: 132, boxH: 58)
    static let live = BracketMetrics(boxW: 144, boxH: 84)

    /// Every length times `k` — the live bracket's Dynamic Type scale.
    func scaled(_ k: CGFloat) -> BracketMetrics {
        BracketMetrics(boxW: boxW * k, boxH: boxH * k, colGap: colGap * k, dsGap: dsGap * k,
                       leagueGap: leagueGap * k, topInset: topInset * k, sideInset: sideInset * k,
                       bandPadX: bandPadX * k, bandPadY: bandPadY * k)
    }
}

/// One league's half: its Division Series boxes (each with the Wild Card box
/// that fed it, if there is one to draw) and its LCS.
struct BracketHalf<Cell> {
    var division: [(ds: Cell, wc: Cell?)]
    var championship: Cell
}

enum BracketLeague: String {
    case al, nl

    var tint: Color {
        switch self {
        case .al: return Color(.systemRed)
        case .nl: return Color(.systemBlue)
        }
    }
}

struct BracketTreeLayout<Cell> {
    struct Placed: Identifiable {
        let id: String
        let cell: Cell
        let center: CGPoint
        let isFinal: Bool
    }
    /// An elbow connector from a child box's right edge to a parent's left edge.
    struct Line: Identifiable {
        let id: Int
        let from: CGPoint
        let to: CGPoint
    }
    struct Label: Identifiable {
        let id: String
        let text: String
        let center: CGPoint
        let emphasized: Bool
        let tint: Color?
    }
    struct Band: Identifiable {
        let id: String
        let rect: CGRect
        let league: BracketLeague
    }

    let placed: [Placed]
    let lines: [Line]
    let headers: [Label]
    let leagueLabels: [Label]
    let bands: [Band]
    let size: CGSize
    let metrics: BracketMetrics

    /// Lay out a bracket. Columns that a year doesn't have are simply absent:
    /// the LCS era has no WC or DS column and draws as Championship → WS.
    static func make(al: BracketHalf<Cell>, nl: BracketHalf<Cell>, worldSeries: Cell,
                     hasWC: Bool, hasDS: Bool, metrics m: BracketMetrics,
                     id: (Cell) -> String) -> BracketTreeLayout {
        let colStride = m.boxW + m.colGap
        var colIndex = 0
        func nextCol() -> Int { defer { colIndex += 1 }; return colIndex }
        let wcCol  = hasWC ? nextCol() : 0
        let dsCol  = hasDS ? nextCol() : 0
        let lcsCol = nextCol()
        let wsCol  = nextCol()
        func colX(_ i: Int) -> CGFloat { m.sideInset + m.boxW / 2 + CGFloat(i) * colStride }
        let wcX = colX(wcCol), dsX = colX(dsCol), lcsX = colX(lcsCol), wsX = colX(wsCol)

        let dsStride = m.boxH + m.dsGap
        // Vertical layout anchors on the LCS box: with DS the two DS boxes
        // flank it; without, it stands alone.
        let halfHeight = hasDS ? (dsStride + m.boxH) : m.boxH
        let alLcsY = m.topInset + halfHeight / 2
        let alHalfBottom = m.topInset + halfHeight
        let nlHalfTop = alHalfBottom + m.leagueGap
        let nlLcsY = nlHalfTop + halfHeight / 2
        let nlHalfBottom = nlHalfTop + halfHeight
        let wsY = (alLcsY + nlLcsY) / 2

        var placed: [Placed] = []
        var lines: [Line] = []
        func right(_ c: CGPoint) -> CGPoint { CGPoint(x: c.x + m.boxW / 2, y: c.y) }
        func left(_ c: CGPoint) -> CGPoint { CGPoint(x: c.x - m.boxW / 2, y: c.y) }
        func connect(_ child: CGPoint, _ parent: CGPoint) {
            lines.append(Line(id: lines.count, from: right(child), to: left(parent)))
        }

        func placeHalf(_ half: BracketHalf<Cell>, lcsY: CGFloat) {
            let lcsCenter = CGPoint(x: lcsX, y: lcsY)
            let count = half.division.count
            for (i, pair) in half.division.enumerated() {
                let dy = lcsY + (CGFloat(i) - CGFloat(count - 1) / 2) * dsStride
                let dsCenter = CGPoint(x: dsX, y: dy)
                placed.append(Placed(id: id(pair.ds), cell: pair.ds, center: dsCenter, isFinal: false))
                if hasWC, let wc = pair.wc {
                    let wcCenter = CGPoint(x: wcX, y: dy)
                    placed.append(Placed(id: id(wc), cell: wc, center: wcCenter, isFinal: false))
                    connect(wcCenter, dsCenter)
                }
                connect(dsCenter, lcsCenter)
            }
            placed.append(Placed(id: id(half.championship), cell: half.championship,
                                 center: lcsCenter, isFinal: false))
            connect(lcsCenter, CGPoint(x: wsX, y: wsY))
        }
        placeHalf(al, lcsY: alLcsY)
        placeHalf(nl, lcsY: nlLcsY)
        placed.append(Placed(id: id(worldSeries), cell: worldSeries,
                             center: CGPoint(x: wsX, y: wsY), isFinal: true))

        var headers: [Label] = []
        let headerY: CGFloat = 20 * (m.topInset / 92)
        if hasWC {
            headers.append(Label(id: "h-wc", text: "Wild Card", center: CGPoint(x: wcX, y: headerY), emphasized: false, tint: nil))
        }
        if hasDS {
            headers.append(Label(id: "h-ds", text: "Division Series", center: CGPoint(x: dsX, y: headerY), emphasized: false, tint: nil))
        }
        headers.append(Label(id: "h-cs", text: "Championship", center: CGPoint(x: lcsX, y: headerY), emphasized: false, tint: nil))
        headers.append(Label(id: "h-ws", text: "World Series", center: CGPoint(x: wsX, y: headerY), emphasized: true, tint: nil))

        // League labels, centred over each half's columns (through the LCS).
        let halfMinX = m.sideInset - m.bandPadX
        let halfMaxX = lcsX + m.boxW / 2 + m.bandPadX
        let halfMidX = (halfMinX + halfMaxX) / 2
        let labelLift = 18 * (m.topInset / 92)
        let leagueLabels: [Label] = [
            Label(id: "l-al", text: "American League", center: CGPoint(x: halfMidX, y: m.topInset - labelLift),
                  emphasized: true, tint: BracketLeague.al.tint),
            Label(id: "l-nl", text: "National League", center: CGPoint(x: halfMidX, y: nlHalfTop - labelLift),
                  emphasized: true, tint: BracketLeague.nl.tint),
        ]

        // Tinted half bands (group each league; the World Series stays neutral).
        let bandLift = m.bandPadY + 24 * (m.topInset / 92)
        let alBandTop = m.topInset - bandLift
        let nlBandTop = nlHalfTop - bandLift
        let bands: [Band] = [
            Band(id: "b-al", rect: CGRect(x: halfMinX, y: alBandTop, width: halfMaxX - halfMinX,
                                          height: alHalfBottom + m.bandPadY - alBandTop), league: .al),
            Band(id: "b-nl", rect: CGRect(x: halfMinX, y: nlBandTop, width: halfMaxX - halfMinX,
                                          height: nlHalfBottom + m.bandPadY - nlBandTop), league: .nl),
        ]

        let size = CGSize(width: wsX + m.boxW / 2 + m.sideInset,
                          height: nlHalfBottom + m.bandPadY + 24 * (m.topInset / 92))
        return BracketTreeLayout(placed: placed, lines: lines, headers: headers,
                                 leagueLabels: leagueLabels, bands: bands, size: size, metrics: m)
    }
}

/// Draws a `BracketTreeLayout`: league bands, elbow connectors, headers,
/// league labels, one `box` per placed cell and the trophy over the final —
/// in one absolute coordinate space inside a two-axis ScrollView.
struct BracketTreeCanvas<Cell, Box: View>: View {
    let layout: BracketTreeLayout<Cell>
    /// Shrink the whole bracket to the window's width when that keeps its
    /// smallest text at `minimumTextSize` or more, otherwise draw it at full
    /// size and scroll (see `fitScale`), with a button at each edge that has
    /// more beyond it. Off for Playoff History, which keeps its chevron.
    var fitsWidth = false
    @ViewBuilder let box: (BracketTreeLayout<Cell>.Placed) -> Box
    @Environment(\.colorScheme) private var colorScheme

    /// The scroll view's own measurements: how wide the window is, how wide
    /// the bracket is, and how far it has been scrolled sideways.
    @State private var scroll = HorizontalScroll()
    @State private var position = ScrollPosition()
    /// The vertical offset, for the edge buttons' scroll — kept out of `scroll`
    /// so a vertical drag doesn't re-render the canvas.
    @State private var vertical = VerticalOffset()
    /// The smallest text in the bracket — headers, status lines, seeds — at the
    /// reader's Dynamic Type size.
    @ScaledMetric(relativeTo: .caption2) private var smallestText: CGFloat = 11
    @Environment(\.dynamicTypeSize) private var typeSize

    /// The smallest text is never drawn under this many points to make the
    /// bracket fit; a window too narrow for that scrolls instead.
    static var minimumTextSize: CGFloat { 9 }
    static var padding: CGFloat { 20 }

    struct HorizontalScroll: Equatable {
        var container: CGFloat = 0
        var content: CGFloat = 0
        var offset: CGFloat = 0
        /// The visible height, inside the safe area.
        var height: CGFloat = 0
        /// The bracket is wider than the window, so part of it is off-screen.
        var overflows: Bool { content > container + 1 }
        /// Some of it is still to the right.
        var moreToTheRight: Bool { overflows && offset < content - container - 4 }
        /// Some of it is to the left.
        var moreToTheLeft: Bool { overflows && offset > 4 }
    }

    final class VerticalOffset { var y: CGFloat = 0 }

    /// How much to shrink the bracket: 1 when it fits as drawn, when the reader
    /// has chosen a text size above the default (a larger bracket to scroll is
    /// what they asked for; shrinking it would hand back the size they turned
    /// up — to about half at AX1), or when shrinking it to fit would take its
    /// smallest text under `minimumTextSize` — a phone held upright keeps
    /// today's full-size bracket and scrolls — and otherwise exactly enough to
    /// fit the width. From the scroll view's measured width, never the device.
    static func fitScale(container: CGFloat, natural: CGFloat, smallestText: CGFloat,
                         typeSize: DynamicTypeSize = .large) -> CGFloat {
        guard container > 0, natural > 0, typeSize <= .large else { return 1 }
        let k = (container - 2 * padding) / natural
        return k < 1 && k * smallestText >= minimumTextSize ? k : 1
    }

    var body: some View {
        let k = fitsWidth
            ? Self.fitScale(container: scroll.container, natural: layout.size.width, smallestText: smallestText,
                            typeSize: typeSize)
            : 1
        ScrollView([.horizontal, .vertical]) {
            ZStack(alignment: .topLeading) {
                ForEach(layout.bands) { band in
                    RoundedRectangle(cornerRadius: 18, style: .continuous)
                        .fill(band.league.tint.opacity(colorScheme == .dark ? 0.16 : 0.07))
                        .frame(width: band.rect.width, height: band.rect.height)
                        .position(x: band.rect.midX, y: band.rect.midY)
                }

                // Elbow connectors — drawn under the boxes, with real weight.
                Canvas { ctx, _ in
                    for line in layout.lines {
                        let midX = (line.from.x + line.to.x) / 2
                        var path = Path()
                        path.move(to: line.from)
                        path.addLine(to: CGPoint(x: midX, y: line.from.y))
                        path.addLine(to: CGPoint(x: midX, y: line.to.y))
                        path.addLine(to: line.to)
                        ctx.stroke(path, with: .color(Color(.secondaryLabel)), lineWidth: 2)
                    }
                }
                .frame(width: layout.size.width, height: layout.size.height)

                ForEach(layout.headers) { label in
                    Text(label.text.uppercased())
                        .font(.caption2.weight(label.emphasized ? .bold : .semibold))
                        .foregroundStyle(label.emphasized ? Color.accentColor : Color.secondary)
                        .position(label.center)
                }

                ForEach(layout.leagueLabels) { label in
                    Text(label.text)
                        .font(.subheadline.weight(.heavy))
                        .foregroundStyle((label.tint ?? .secondary).opacity(0.9))
                        .position(label.center)
                }

                ForEach(layout.placed) { item in
                    box(item)
                        .position(item.center)
                }

                // Championship trophy — centred ABOVE the World Series box so it
                // marks the final without overlapping the team rows or scores.
                if let ws = layout.placed.first(where: { $0.isFinal }) {
                    Image(systemName: "trophy.fill")
                        .font(.callout.weight(.semibold))
                        .foregroundStyle(.tint)
                        .position(x: ws.center.x, y: ws.center.y - layout.metrics.boxH / 2 - 13)
                }
            }
            .frame(width: layout.size.width, height: layout.size.height, alignment: .topLeading)
            .scaleEffect(k, anchor: .topLeading)
            .frame(width: layout.size.width * k, height: layout.size.height * k, alignment: .topLeading)
            .padding(Self.padding)
            // CENTRED WHEN IT FITS. A bracket narrower than the window used to
            // sit against the left edge with the right half of an iPad empty.
            // When it is wider, minWidth is the smaller number and changes nothing.
            // The Standings bracket is centred top to bottom as well: on a tall
            // window it sat at the top with half the screen empty below it.
            .frame(minWidth: scroll.container, minHeight: fitsWidth ? scroll.height : nil,
                   alignment: fitsWidth ? .center : .top)
        }
        .scrollPosition($position)
        .onScrollGeometryChange(for: HorizontalScroll.self) { geo in
            HorizontalScroll(container: geo.containerSize.width,
                             content: geo.contentSize.width,
                             offset: geo.contentOffset.x,
                             height: geo.containerSize.height - geo.contentInsets.top - geo.contentInsets.bottom)
        } action: { _, new in
            scroll = new
        }
        .onScrollGeometryChange(for: CGFloat.self) { $0.contentOffset.y } action: { _, y in
            vertical.y = y
        }
        // WHEN IT DOES NOT FIT, SAY SO — AND OFFER THE WAY. A two-axis canvas cut
        // off at the right edge gave no sign that the Championship and World
        // Series were there, and a small decorative chevron was easy to miss.
        // Each edge with more bracket beyond it carries a button that scrolls
        // there; the canvas still scrolls under the fade beside it.
        .scrollIndicators(scroll.overflows ? .visible : .automatic, axes: .horizontal)
        .overlay(alignment: .trailing) {
            if scroll.moreToTheRight {
                if fitsWidth {
                    ScrollEdgeButton(edge: .trailing) { scrollTo(x: scroll.content - scroll.container) }
                        .transition(.opacity)
                } else {
                    MoreToTheRightHint()
                        .transition(.opacity)
                }
            }
        }
        .overlay(alignment: .leading) {
            if fitsWidth && scroll.moreToTheLeft {
                ScrollEdgeButton(edge: .leading) { scrollTo(x: 0) }
                    .transition(.opacity)
            }
        }
        .animation(.easeOut(duration: 0.2), value: scroll.moreToTheRight)
        .animation(.easeOut(duration: 0.2), value: scroll.moreToTheLeft)
    }

    private func scrollTo(x: CGFloat) {
        withAnimation(.easeInOut(duration: 0.35)) {
            position.scrollTo(point: CGPoint(x: max(0, x), y: vertical.y))
        }
    }
}

/// A fade and a round arrow button at an edge of a canvas that scrolls
/// sideways: the arrow scrolls to that end, and only the arrow takes touches,
/// so the canvas still scrolls under the fade.
private struct ScrollEdgeButton: View {
    let edge: HorizontalEdge
    let action: () -> Void

    var body: some View {
        let trailing = edge == .trailing
        ZStack(alignment: trailing ? .trailing : .leading) {
            LinearGradient(colors: [Color(.systemBackground).opacity(0), Color(.systemBackground).opacity(0.85)],
                           startPoint: trailing ? .leading : .trailing, endPoint: trailing ? .trailing : .leading)
                .frame(width: 56)
                .allowsHitTesting(false)
            Button(action: action) {
                Image(systemName: trailing ? "chevron.right" : "chevron.left")
                    .font(.body.weight(.bold))
                    .frame(width: 44, height: 44)
                    .contentShape(Circle())
            }
            .buttonStyle(.plain)
            .foregroundStyle(.primary)
            .glassEffect(.regular.interactive(), in: Circle())
            .padding(trailing ? .trailing : .leading, 8)
            .accessibilityLabel(trailing ? "Scroll to the end of the bracket" : "Scroll to the start of the bracket")
        }
        .frame(maxHeight: .infinity)
    }
}

/// A fade and a chevron at the trailing edge of a canvas that scrolls sideways
/// (Playoff History). Decoration only: it takes no touches, so the canvas
/// scrolls beneath it.
private struct MoreToTheRightHint: View {
    var body: some View {
        ZStack(alignment: .trailing) {
            LinearGradient(colors: [Color(.systemBackground).opacity(0), Color(.systemBackground).opacity(0.85)],
                           startPoint: .leading, endPoint: .trailing)
                .frame(width: 44)
            Image(systemName: "chevron.compact.right")
                .font(.title2.weight(.semibold))
                .foregroundStyle(.secondary)
                .padding(.trailing, 6)
        }
        .frame(maxHeight: .infinity)
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }
}
