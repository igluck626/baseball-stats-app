//
//  TeamContactBlock.swift
//  BaseballStats
//
//  Each team's AVG, expected batting average (xBA) and balls hit 95+ mph — the
//  luck-versus-contact line at the top of the Game Leaders card (finals), and
//  in the same place on a live box score in a card of its own. Every number is
//  the backend's (`TeamContact`); this view only lays it out.
//

import SwiftUI

struct TeamContactBlock: View {
    let contact: TeamContact
    let awayAbbr: String
    let homeAbbr: String

    @Environment(\.dynamicTypeSize) private var typeSize
    @State private var showingInfo = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Text("TEAM CONTACT")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                Button { showingInfo = true } label: {
                    Image(systemName: "info.circle")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("About team contact")
                .accessibilityIdentifier("teamContact.info")
                // A popover on a regular-width screen; a sheet on a phone,
                // where a popover anchored low on the screen had no room and
                // cut its own heading off.
                .popover(isPresented: $showingInfo) {
                    TeamContactInfo()
                        .presentationCompactAdaptation(.sheet)
                        .presentationDetents([.medium, .large])
                        .presentationDragIndicator(.visible)
                }
            }
            if typeSize.isAccessibilitySize {
                // One Text per team, so the line wraps rather than breaking a
                // column apart (the house pattern for metric rows).
                VStack(alignment: .leading, spacing: 6) {
                    Text(line(awayAbbr, contact.away))
                    Text(line(homeAbbr, contact.home))
                }
                .font(.subheadline)
                .fixedSize(horizontal: false, vertical: true)
            } else {
                table
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("teamContact")
    }

    /// Natural width, leading — on a wide window it stays a compact table
    /// rather than stretching its columns across the card.
    private var table: some View {
        Grid(alignment: .trailing, horizontalSpacing: 18, verticalSpacing: 4) {
            GridRow {
                Color.clear.gridCellUnsizedAxes([.horizontal, .vertical])
                Text("AVG")
                Text("xBA")
                Text("95+ MPH")
            }
            .font(.caption2.weight(.semibold))
            .foregroundStyle(.secondary)
            row(awayAbbr, contact.away)
            row(homeAbbr, contact.home)
        }
        .fixedSize()
    }

    private func row(_ abbr: String, _ side: TeamContact.Side) -> some View {
        GridRow {
            Text(abbr)
                .font(.subheadline.weight(.semibold))
                .gridColumnAlignment(.leading)
            Text(TeamContact.rate(side.avg))
            Text(TeamContact.rate(side.xba))
            Text("\(side.hardHit)")
        }
        .font(.subheadline)
        .monospacedDigit()
    }

    private func line(_ abbr: String, _ side: TeamContact.Side) -> String {
        "\(abbr) · \(TeamContact.rate(side.avg)) AVG · \(TeamContact.rate(side.xba)) xBA · \(side.hardHit) hit 95+ mph"
    }
}

/// The ⓘ explanation, at a FIXED width: a popover measures the text's height at
/// the width it proposes, and with only a maximum width the two disagreed and
/// the last lines were cut off. (A popover's contents are compact width too, so
/// the size class can't tell it from the phone's sheet.) It scrolls only if it
/// doesn't fit — the sheet at the accessibility sizes.
private struct TeamContactInfo: View {
    var body: some View {
        ViewThatFits(in: .vertical) {
            info
            ScrollView { info }
        }
        .frame(width: 340, alignment: .leading)
    }

    private var info: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("xBA (expected batting average)")
                .font(.subheadline.weight(.semibold))
            Text("The average a team's contact would usually produce. Each ball in play is rated by how often balls hit at that speed and angle fall for hits; strikeouts count as outs. An xBA above the team's AVG means its contact deserved more hits than it got.")
            Text("95+ mph")
                .font(.subheadline.weight(.semibold))
            Text("Balls hit 95 mph or harder off the bat — hard-hit balls.")
            Text("Statcast data via balldontlie.")
                .foregroundStyle(.secondary)
        }
        .font(.footnote)
        .fixedSize(horizontal: false, vertical: true)
        .padding()
    }
}

/// The block on its own, for a live box score — where the rest of the Game
/// Leaders card stays hidden until the game is final.
struct TeamContactCard: View {
    let contact: TeamContact
    let awayAbbr: String
    let homeAbbr: String

    var body: some View {
        TeamContactBlock(contact: contact, awayAbbr: awayAbbr, homeAbbr: homeAbbr)
            .padding(.horizontal, 14)
            .padding(.vertical, 14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 16))
            .shadow(color: .black.opacity(0.06), radius: 8, x: 0, y: 3)
    }
}
