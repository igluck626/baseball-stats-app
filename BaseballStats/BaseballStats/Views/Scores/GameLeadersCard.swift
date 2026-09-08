//
//  GameLeadersCard.swift
//  BaseballStats
//
//  The game's hardest-hit balls and fastest pitches. Two groupings of
//  the same data: overall, or split by side.
//
//  ⚠️ The grouping control is NOT a team selector. The box score
//  already has one of those at the top, and it is always one team —
//  `TeamSide` is `.away | .home` with no neutral case, so it cannot
//  express "overall" and cannot drive this section. This control
//  chooses how the leaders are grouped, which is a different question
//  from which team's tables you are reading, so the two do not
//  duplicate each other. They do sit close together visually, which is
//  the real cost — see the note in the scoping report.
//

import SwiftUI

struct GameLeadersCard: View {
    let leaders: GameLeaders
    let away: GameLeaders?
    let home: GameLeaders?
    let awayAbbr: String
    let homeAbbr: String

    enum Grouping: String, CaseIterable, Identifiable {
        case overall = "Overall"
        case byTeam  = "By Team"
        var id: String { rawValue }
    }

    @State private var grouping: Grouping = .overall
    @Environment(\.dynamicTypeSize) private var typeSize

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("GAME LEADERS")
                .font(.caption.weight(.bold))
                .tracking(0.8)
                .foregroundStyle(.secondary)

            Picker("Grouping", selection: $grouping) {
                ForEach(Grouping.allCases) { g in Text(g.rawValue).tag(g) }
            }
            .pickerStyle(.segmented)

            switch grouping {
            case .overall:
                category("HARDEST HIT", leaders.hardestHit, unit: "mph")
                category("FASTEST PITCH", leaders.fastestPitches, unit: "mph")
            case .byTeam:
                // Each side re-ranked from the full set, so a side that
                // placed nowhere overall still shows its own best.
                sideBlock(awayAbbr, away)
                sideBlock(homeAbbr, home)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 16))
        .shadow(color: .black.opacity(0.06), radius: 8, x: 0, y: 3)
    }

    @ViewBuilder
    private func sideBlock(_ abbr: String, _ side: GameLeaders?) -> some View {
        if let side, !side.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                Text(abbr)
                    .font(.subheadline.weight(.bold))
                category("HARDEST HIT", side.hardestHit, unit: "mph")
                category("FASTEST PITCH", side.fastestPitches, unit: "mph")
            }
            .padding(.top, 4)
        }
    }

    @ViewBuilder
    private func category(_ title: String, _ entries: [GameLeaders.Entry], unit: String) -> some View {
        if !entries.isEmpty {
            // A wrapped entry occupies three lines at the accessibility
            // sizes, so a 6pt gap between entries is the same gap as
            // between an entry's own wrapped lines and the rows run
            // together into one block of text. Widen the separation
            // there so an entry still reads as one item.
            VStack(alignment: .leading, spacing: typeSize.isAccessibilitySize ? 16 : 6) {
                Text(title)
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.tertiary)
                ForEach(entries) { e in
                    // One Text with " · " separators rather than an
                    // HStack of three — an HStack breaks mid-token at
                    // the accessibility sizes, which the metric rows
                    // elsewhere in this app have been bitten by twice.
                    // The number is not right-aligned into a column for
                    // the same reason: a column of values fights the
                    // names for width once the type scales.
                    Text(Self.line(e, unit: unit))
                        .font(.subheadline)
                        .foregroundStyle(.primary)
                        .lineLimit(typeSize.isAccessibilitySize ? nil : 2)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
    }

    /// "Muncy · 108.2 mph · Double". The outcome is what makes the
    /// number mean something — 108 into a double play reads differently
    /// from 108 over the wall — so it is part of the line rather than
    /// an optional trailing decoration.
    static func line(_ e: GameLeaders.Entry, unit: String) -> String {
        var parts = [e.name, String(format: "%.1f \(unit)", e.value)]
        if let d = e.detail, !d.isEmpty { parts.append(d) }
        return parts.joined(separator: " \u{00B7} ")
    }
}
