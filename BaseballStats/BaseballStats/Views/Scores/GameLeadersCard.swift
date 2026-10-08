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

    /// ⚠️ THREE-WAY, replacing an Overall / By Team toggle whose
    /// by-team state stacked BOTH sides — twenty rows where overall
    /// rendered ten, which is most of why the card read long. A filter
    /// shows ten rows in every state.
    enum Scope: Hashable, Identifiable {
        case overall, away, home
        var id: Self { self }
    }

    @State private var scope: Scope = .overall
    /// The card is collapsed until asked for, at EVERY text size. The
    /// earlier collapse fired only at the accessibility sizes, where
    /// the card ran to some eight screens; it reads long at the default
    /// sizes too, and it sits between the box-score tables and the
    /// plays list, which is a bad place to be long.
    @State private var isExpanded = false
    @State private var selectedPlay: PlayDetail?
    /// Only consulted at the accessibility sizes — see `visible`.
    @State private var expanded = false
    /// Width of the right-aligned value column. Scaled so the decimal
    /// points still line up at the Dynamic Type steps below the
    /// accessibility ones, where the table is still in use.
    @ScaledMetric(relativeTo: .subheadline) private var valueColumnWidth: CGFloat = 46
    @Environment(\.dynamicTypeSize) private var typeSize

    /// The board the current scope names. Each side is ranked again
    /// from the whole game rather than filtered out of the overall
    /// board, so a side that placed nowhere still shows its own best.
    private var board: GameLeaders? {
        switch scope {
        case .overall: return leaders
        case .away:    return away
        case .home:    return home
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            if isExpanded {
                Picker("Scope", selection: $scope) {
                    Text("Overall").tag(Scope.overall)
                    Text(awayAbbr).tag(Scope.away)
                    Text(homeAbbr).tag(Scope.home)
                }
                .pickerStyle(.segmented)

                if let board, !board.isEmpty {
                    category("HARDEST HIT", board.hardestHit, unit: "mph")
                    category("FASTEST PITCH", board.fastestPitches, unit: "mph")
                } else {
                    Text("No tracked measurements for this side.")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
            } else {
                teaser
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
        // Both feeds, each supplying what it is better at — the call
        // from the stream, the speed and pitch name from the plate
        // appearance. See `PlayDetailSheet.pitches(stream:pa:)`.
        let pitches = PlayDetailSheet.pitches(stream: pitchRows[key] ?? [], pa: rows)
        return PlayDetail(
            id:       e.id,
            title:    e.pa.result ?? "Play",
            sentence: sentences[key] ?? "",
            contact:  rows.first { $0.exitVelocity != nil },
            pitches:  pitches,
            highlightIndex: e.pitchIndex,
        )
    }

    private var header: some View {
        Button {
            withAnimation(.easeInOut(duration: 0.2)) { isExpanded.toggle() }
        } label: {
            HStack {
                Text("GAME LEADERS")
                    .font(.caption.weight(.bold))
                    .tracking(0.8)
                    .foregroundStyle(.secondary)
                Spacer(minLength: 8)
                Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    /// What the collapsed card shows.
    ///
    /// ⚠️ EACH LINE IS LABELLED. Two bare numbers — "112.6 mph" and
    /// "100.9 mph" — do not say which is a batted ball and which is a
    /// pitch, and a reader who has to tap to find out has been given a
    /// puzzle rather than a summary.
    ///
    /// One Text per line with " · " separators, never an HStack, so the
    /// line wraps rather than breaking mid-token at the accessibility
    /// sizes.
    @ViewBuilder
    private var teaser: some View {
        VStack(alignment: .leading, spacing: teaserSpacing) {
            if let best = leaders.hardestHit.first {
                teaserLine("Hardest hit", best)
            }
            if let best = leaders.fastestPitches.first {
                teaserLine("Fastest pitch", best)
            }
        }
    }

    private var teaserSpacing: CGFloat { typeSize.isAccessibilitySize ? 14 : 4 }

    private func teaserLine(_ label: String, _ e: GameLeaders.Entry) -> some View {
        Text(Self.teaserText(label, e, detail: displayDetail(e), style: effectiveTeaserStyle))
            .font(.subheadline)
            .foregroundStyle(.primary)
            .lineLimit(typeSize.isAccessibilitySize ? nil : 2)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// How much of the row to carry into the collapsed state.
    enum TeaserStyle {
        /// "Hardest hit · 112.6 mph"
        case numberOnly
        /// "Hardest hit · Teoscar Hernandez · 112.6 mph"
        case named
        /// "Hardest hit · Teoscar Hernandez · 112.6 mph · Home Run"
        case full
    }
    /// The shape used at the ordinary text sizes. At the accessibility
    /// sizes it is overridden — see `effectiveTeaserStyle`.
    var teaserStyle: TeaserStyle = .named

    /// ⚠️ NAMED ordinarily, NUMBER-ONLY at the accessibility sizes.
    ///
    /// The name is what makes a folded board worth opening — "Halvorsen
    /// threw 100.9" poses the question the board answers, where a bare
    /// number only reports. But at AX5 the named line wraps to three
    /// lines, so two of them fill some 1350pt and folding the card has
    /// saved almost nothing. Dropping the name there costs a third of
    /// the height and keeps the LABEL, which is the part that carries
    /// meaning: a reader still knows which number is a batted ball and
    /// which is a pitch.
    private var effectiveTeaserStyle: TeaserStyle {
        typeSize.isAccessibilitySize ? .numberOnly : teaserStyle
    }

    static func teaserText(
        _ label: String, _ e: GameLeaders.Entry, detail: String?, style: TeaserStyle,
    ) -> String {
        let value = String(format: "%.1f mph", e.value)
        switch style {
        case .numberOnly: return "\(label) \u{00B7} \(value)"
        case .named:      return "\(label) \u{00B7} \(e.name) \u{00B7} \(value)"
        case .full:
            var parts = [label, e.name, value]
            if let d = detail, !d.isEmpty { parts.append(d) }
            return parts.joined(separator: " \u{00B7} ")
        }
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
                // ⚠️ The unit belongs in the header once the value has
                // its own column: tabularising stripped "mph" from
                // every row, leaving a column of bare numbers. At the
                // accessibility sizes the rows carry it themselves, so
                // the header would repeat it.
                Text(typeSize.isAccessibilitySize ? title : "\(title) (\(unit.uppercased()))")
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
                        row(e, unit: unit)
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

    /// ⚠️ TABULAR AT THE ORDINARY SIZES, a single wrapping line at the
    /// accessibility ones.
    ///
    /// Columns were rejected for the PLAYS list because a row there is a
    /// sentence, and a sentence in a fixed column truncates. A leaders
    /// row is three short fields — a name, a number, a word — so the
    /// number can hold its own right-aligned column and the decimal
    /// points line up down the board, which is most of what "organised"
    /// means for a list of measurements.
    ///
    /// It still cannot survive the accessibility sizes: a name that
    /// wraps to three lines beside a fixed 92pt column leaves the number
    /// stranded against a tall block of text. So above those sizes the
    /// row collapses to the same single Text the teaser uses. The
    /// COLUMN WIDTH is a `@ScaledMetric`, so it also tracks the smaller
    /// Dynamic Type steps rather than only working at the default.
    @ViewBuilder
    private func row(_ e: GameLeaders.Entry, unit: String) -> some View {
        let detail = displayDetail(e)
        if typeSize.isAccessibilitySize {
            Text(Self.line(e, unit: unit, detail: detail))
                .font(.subheadline)
                .foregroundStyle(.primary)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentShape(Rectangle())
        } else {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(e.name)
                    .font(.subheadline)
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
                Spacer(minLength: 4)
                if let d = detail, !d.isEmpty {
                    Text(d)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                Text(String(format: "%.1f", e.value))
                    .font(.subheadline.weight(.semibold))
                    .monospacedDigit()
                    .frame(width: valueColumnWidth, alignment: .trailing)
            }
            .frame(maxWidth: .infinity)
            .contentShape(Rectangle())
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
    /// ⚠️ NOTHING IS SUPPRESSED ON EITHER BOARD, and the argument that
    /// once said otherwise is kept here because it was not wrong so much
    /// as wrong FOR THIS SURFACE.
    ///
    /// It ran: seven of a game's ten fastest pitches are one man's, every
    /// one a 4-Seam Fastball, and printing that seven times says nothing
    /// after the first. True of a LIST OF WRAPPING SENTENCES, where a
    /// repeated tail is read as part of the sentence and pads a line the
    /// eye is already tracking word by word.
    ///
    /// It is false of a TABLE. A column is read down, and a blank cell in
    /// a column does not read as "same as above" — it reads as missing
    /// data. A reader cannot know that the gap under "4-Seam Fastball"
    /// means 4-Seam Fastball, and a board of measurements is exactly the
    /// place where a reader assumes a blank means the number was not
    /// recorded. Ten identical cells are repetitive; nine blank ones are
    /// wrong.
    ///
    /// So the suppression is gone rather than disabled — a helper left in
    /// place invites the next board to reuse it.
    ///
    /// The hardest-hit board never suppressed its outcomes, for a related
    /// reason already recorded: two batters' singles are two facts that
    /// happen to share a word.
    static func line(_ e: GameLeaders.Entry, unit: String, detail: String?) -> String {
        var parts = [e.name, String(format: "%.1f \(unit)", e.value)]
        if let d = detail, !d.isEmpty { parts.append(d) }
        return parts.joined(separator: " \u{00B7} ")
    }

    /// The trailing detail as it should read.
    ///
    /// ⚠️ A pitch's name comes from the PLATE APPEARANCE, which is what
    /// the detail sheet also shows — the two must agree, since a row
    /// and the sheet it opens are one tap apart. The play stream names
    /// the same pitch differently ("Four-seam FB" against "4-Seam
    /// Fastball") AND truncates its speed, so it supplies neither here.
    ///
    /// A HIT's detail is the plate-appearance outcome ("Double"), which
    /// the stream has no equivalent of.
    private func displayDetail(_ e: GameLeaders.Entry) -> String? { e.detail }
}
