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
    /// BDL's outcome sentence per plate appearance, so a tapped row can
    /// open the same detail sheet the plays list opens. Empty is fine —
    /// the sheet then leads with the outcome noun alone.
    var sentences: [String: String] = [:]
    /// The play stream's pitch rows per plate appearance, where the two
    /// feeds agree on count. Supplies the RICHER pitch vocabulary —
    /// "Called Strike" rather than the PA feed's flat "Strike" — so a
    /// sheet opened from here reads exactly as one opened from the
    /// plays list. Absent for a PA falls back to the PA's own pitches.
    var pitchRows: [String: [BDLPlay]] = [:]

    enum Grouping: String, CaseIterable, Identifiable {
        case overall = "Overall"
        case byTeam  = "By Team"
        var id: String { rawValue }
    }

    @State private var grouping: Grouping = .overall
    @State private var selectedPlay: PlayDetail?
    /// Only consulted at the accessibility sizes — see `visible`.
    @State private var expanded = false
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
        .sheet(item: $selectedPlay) { PlayDetailSheet(detail: $0) }
    }

    /// The detail sheet behind one leaders row.
    ///
    /// Built from the plate appearance the entry carries, so this needs
    /// no second fetch and no access to the plays list's own grouping.
    /// The marked pitch is the one the row is ABOUT — for a hardest-hit
    /// row that is the ball put in play, for a fastest-pitch row the
    /// pitch itself.
    private func detail(for e: GameLeaders.Entry) -> PlayDetail {
        let rows = e.pa.pitches ?? []
        let key = GameLeaders.paKey(e.pa)
        // Prefer the play stream's pitch rows, mapped through the SAME
        // two functions the plays list uses — that is what guarantees
        // the two routes into this sheet say the same thing, rather
        // than two mappings that merely look alike today.
        let pitches: [PlayDetail.Pitch]
        if let stream = pitchRows[key], stream.count == rows.count {
            pitches = stream.map {
                PlayDetail.Pitch(
                    call:   PlayDetailSheet.call($0),
                    detail: PlayDetailSheet.pitchDescription($0),
                )
            }
        } else {
            pitches = rows.map(PlayDetailSheet.pitch(from:))
        }
        return PlayDetail(
            id:       e.id,
            title:    e.pa.result ?? "Play",
            sentence: sentences[key] ?? "",
            contact:  rows.first { $0.exitVelocity != nil },
            pitches:  pitches,
            highlightIndex: e.pitchIndex,
        )
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
            let shown = visible(entries)
            // A wrapped entry occupies three lines at the accessibility
            // sizes, so a 6pt gap between entries is the same gap as
            // between an entry's own wrapped lines and the rows run
            // together into one block of text. Widen the separation
            // there so an entry still reads as one item.
            VStack(alignment: .leading, spacing: typeSize.isAccessibilitySize ? 16 : 6) {
                Text(title)
                    .font(.caption2.weight(.semibold))
                    .foregroundStyle(.tertiary)
                ForEach(Array(shown.enumerated()), id: \.element.id) { i, e in
                    // ⚠️ NO chevron, no tint, no disclosure. The app
                    // signals tappability nowhere — the survey found it
                    // unsignalled app-wide, and it works because it is
                    // consistent. A single decorated list here would
                    // read as the exception rather than as the rule
                    // this list follows.
                    Button { selectedPlay = detail(for: e) } label: {
                        Text(Self.line(
                            e, unit: unit,
                            detail: displayDetail(e),
                            previousDetail: i > 0 ? displayDetail(shown[i - 1]) : nil,
                        ))
                            .font(.subheadline)
                            .foregroundStyle(.primary)
                            .lineLimit(typeSize.isAccessibilitySize ? nil : 2)
                            .fixedSize(horizontal: false, vertical: true)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
                if shown.count < entries.count {
                    Button("Show all \(entries.count)") { expanded = true }
                        .font(.subheadline.weight(.semibold))
                }
            }
        }
    }

    /// How many rows to draw.
    ///
    /// Ten at every ordinary size, where the whole card is a little
    /// over half a screen. At the accessibility sizes each entry wraps
    /// to three lines, which makes twenty entries plus headers around
    /// EIGHT screens sitting between the box-score tables and the plays
    /// list — so it starts collapsed and expands on request.
    ///
    /// ⚠️ Collapsed, not truncated. The precedent is the plays list's
    /// pitch button, which MOVES below the sentence at these sizes
    /// rather than disappearing: layout adapts, content does not. A
    /// board that simply showed five rows at large text would give the
    /// reader who needs large text less of the game than everyone else,
    /// which is a content difference dressed as a layout one. An
    /// expander keeps all ten reachable and only changes how far you
    /// scroll to reach the next card.
    private func visible(_ entries: [GameLeaders.Entry]) -> [GameLeaders.Entry] {
        guard typeSize.isAccessibilitySize, !expanded else { return entries }
        return Array(entries.prefix(3))
    }

    /// "Muncy · 108.2 mph · Double". The outcome is what makes the
    /// number mean something — 108 into a double play reads differently
    /// from 108 over the wall — so it is part of the line rather than
    /// an optional trailing decoration.
    /// ⚠️ The trailing detail is dropped when it repeats the row
    /// DIRECTLY above — consecutive suppression, not first-occurrence.
    /// Seven of this game's ten fastest pitches are Halvorsen's, every
    /// one a 4-Seam Fastball, and printing that seven times says
    /// nothing after the first. Suppressing every later occurrence
    /// instead would be wrong: where a slider interrupts a run of
    /// fastballs, the fastball returning is news, and a reader scanning
    /// from the middle of the list needs the label to have reappeared.
    static func line(
        _ e: GameLeaders.Entry, unit: String, detail: String?, previousDetail: String?,
    ) -> String {
        var parts = [e.name, String(format: "%.1f \(unit)", e.value)]
        if let d = detail, !d.isEmpty {
            // ⚠️ Suppression applies to PITCH TYPES only. A run of one
            // pitcher's fastballs is one fact repeated, and printing it
            // seven times says nothing after the first. Two batters'
            // singles are two different facts that happen to share a
            // word — dropping the second leaves the reader unable to
            // tell what that ball became, which is exactly the thing
            // the outcome was added to say.
            if e.kind == .pitch, d == previousDetail {} else { parts.append(d) }
        }
        return parts.joined(separator: " \u{00B7} ")
    }

    /// The trailing detail as it should read.
    ///
    /// ⚠️ For a PITCH, the play stream's name wins over the plate
    /// appearance's. The two feeds name the same pitch differently —
    /// "Four-seam FB" against "4-Seam Fastball" — and the detail sheet
    /// this row opens is built from the stream. Left alone, a row would
    /// say one and its own sheet the other, one tap apart. Preferring
    /// one source for display is the whole point of the exercise; the
    /// PA feed remains the fallback where the two disagree on pitch
    /// count and the stream cannot be trusted to line up.
    ///
    /// A HIT's detail is the plate-appearance outcome ("Double"), which
    /// the stream has no equivalent of, so it is never overridden.
    private func displayDetail(_ e: GameLeaders.Entry) -> String? {
        guard e.kind == .pitch,
              let stream = pitchRows[GameLeaders.paKey(e.pa)],
              e.pitchIndex < stream.count,
              let type = stream[e.pitchIndex].pitchType, !type.isEmpty
        else { return e.detail }
        return type
    }
}
