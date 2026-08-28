//
//  LiveComponents.swift
//  BaseballStats
//
//  Shared building blocks for the live-game surfaces in the Scores
//  tab: visual primitives (`BaseRunnerView`, `LiveBadge`), the team-logo
//  cache + view, and the `PlaysView` play-by-play list. These consume the
//  live snapshots the shared `LiveGameStore` publishes; none owns a poll
//  loop of its own (Phase 2 moved all live polling into the store).
//

import Combine
import SwiftUI

// MARK: - Base runner diamond

/// Three filled/outlined squares arranged in a baseball diamond:
/// second at top, third at left, first at right (home plate is the
/// implicit bottom-center anchor). Each square is rotated 45° to
/// read as a diamond. Fill = runner on that base.
struct BaseRunnerView: View {
    let first: Bool
    let second: Bool
    let third: Bool

    /// Side length of the bounding square. Bases are sized
    /// proportionally so the view scales cleanly between the live
    /// game card (compact) and the box score header (larger).
    var size: CGFloat = 32

    var body: some View {
        // Drive the diamond off a slightly inset dimension: a base offset by
        // `d * 0.32` whose rotated square extends ~`d * 0.17` past that offset
        // would reach ~0.52*size and spill the frame. Insetting to 0.86*size
        // keeps the far corners at ~0.45*size — fully inside the `size` box
        // (nothing clipped, nothing spilling into the row) while callers keep
        // using `size` as the footprint.
        let d = size * 0.86
        return ZStack {
            base(filled: second, side: d).offset(y: -d * 0.32)
            base(filled: third,  side: d).offset(x: -d * 0.32)
            base(filled: first,  side: d).offset(x: d * 0.32)
        }
        .frame(width: size, height: size)
    }

    private func base(filled: Bool, side: CGFloat) -> some View {
        Rectangle()
            .fill(filled ? Color.accentColor : Color.clear)
            .overlay(
                Rectangle()
                    .stroke(Color.primary.opacity(0.6), lineWidth: 1)
            )
            .frame(width: side * 0.28, height: side * 0.28)
            .rotationEffect(.degrees(45))
    }
}

// MARK: - Team logo

/// A club's identity mark: its abbreviation set in a tinted circle.
///
/// This is what stands in for the team logo from 2026-08-15 — the app is not
/// licensed to display MLB's marks, so `TeamLogoCache` (an in-memory image
/// store with its own download tasks) and every CDN request went with them.
/// Nothing here touches the network, so there is no placeholder state and no
/// failure state: the badge is correct the instant it is laid out.
///
/// Takes a plain string rather than a `TeamInfo` so the Standings and Compare
/// screens — which hold a Lahman code, not a resolved team — can render the
/// same mark. `TeamLogoView` wraps it for the `TeamInfo` callers.
struct TeamBadge: View {
    let abbreviation: String
    var size: CGFloat = 28

    var body: some View {
        Circle()
            .fill(Color(.secondarySystemFill))
            .frame(width: size, height: size)
            .overlay(
                Text(text)
                    .font(.system(size: max(8, size * 0.32), weight: weight))
                    .foregroundStyle(label)
                    .lineLimit(1)
                    .minimumScaleFactor(0.7)
                    // Keep 3-letter codes off the curve at every size.
                    .padding(.horizontal, size * 0.08)
            )
            .accessibilityLabel(text == "—" ? "Team" : text)
    }

    /// The two big marks — the 52/56pt tiles and the 88pt home header — are
    /// the club's identity on the screen, not an annotation, so they take the
    /// primary label at bold. A large low-contrast disc reads as an image
    /// that failed to load; a solid one reads as a deliberate mark.
    ///
    /// The small marks stay secondary/semibold. At 22pt beside a standings
    /// row the letters are a quiet identifier next to the name that already
    /// says the team — promoting them there would make every row shout.
    private var isLarge: Bool { size >= 52 }
    private var weight: Font.Weight { isLarge ? .bold : .semibold }
    private var label: Color { isLarge ? .primary : .secondary }

    /// An em dash for an unknown or empty club, so the circle never renders
    /// blank — a code the abbreviation table doesn't know still yields a mark.
    private var text: String {
        let trimmed = abbreviation.trimmingCharacters(in: .whitespaces)
        return trimmed.isEmpty ? "—" : trimmed.uppercased()
    }
}

/// A club's color as a slim vertical bar, set immediately before its letters.
///
/// This is the mark on the score surfaces: where a row already prints the
/// abbreviation, a circle repeating those same letters said nothing twice, so
/// the color carries the identity and the text carries the name. Copied in
/// shape from the postseason bracket, which has read this way for a while.
///
/// The height is a parameter because the bar should match the line it leads,
/// not a fixed idea of a row: the bracket itself already uses 18 in its main
/// tree and 16 in its two compact lists.
struct TeamColorSwatch: View {
    let lahmanCode: String?
    var height: CGFloat = 18

    @Environment(\.colorScheme) private var colorScheme

    init(code: String?, height: CGFloat = 18) {
        self.lahmanCode = code
        self.height = height
    }

    /// For callers holding a `TeamInfo` and nothing else — resolves the club
    /// from the MLBAM id it carries.
    init(team: TeamInfo, height: CGFloat = 18) {
        self.lahmanCode = MLBTeamCatalog.lahmanCode(forMLBAMId: team.id)
        self.height = height
    }

    var body: some View {
        RoundedRectangle(cornerRadius: 2.5, style: .continuous)
            .fill(TeamColors.chip(for: lahmanCode, dark: colorScheme == .dark))
            .frame(width: 5, height: height)
            .accessibilityHidden(true)   // the abbreviation beside it is the label
    }
}

/// Team mark used across the Scores-tab cards and the team sheets. Signature
/// is unchanged from the logo era on purpose — all sixteen call sites keep
/// their frames and their layout.
struct TeamLogoView: View {
    let team: TeamInfo
    var size: CGFloat = 28

    var body: some View {
        TeamBadge(abbreviation: team.abbreviation ?? String(team.name.prefix(3)),
                  size: size)
    }
}

// MARK: - LIVE badge

/// Small red "LIVE" capsule with a pulsing dot. The pulse runs
/// while the view is on screen — `.onAppear` flips the animatable
/// state once and the `repeatForever` modifier carries it from
/// there.
struct LiveBadge: View {
    @State private var pulse = false

    var body: some View {
        HStack(spacing: 5) {
            Circle()
                .fill(Color.white)
                .frame(width: 6, height: 6)
                .opacity(pulse ? 0.4 : 1.0)
                .animation(
                    .easeInOut(duration: 0.9).repeatForever(autoreverses: true),
                    value: pulse
                )
            Text("LIVE")
                .font(.caption2.weight(.heavy))
                .foregroundStyle(.white)
                .kerning(0.5)
        }
        .padding(.horizontal, 7)
        .padding(.vertical, 3)
        .background(Capsule().fill(Color.red))
        .onAppear { pulse = true }
        .accessibilityLabel("Live")
    }
}

// MARK: - Plays

/// Expandable plays section for the box score. Shows a `Plays ▾`
/// header row that toggles between collapsed (just the header) and
/// expanded (segmented Scoring/All picker + the play list).
///
/// All plays-related state lives here — mode pick, set of expanded
/// half-innings in All-mode, the "user has manually toggled" guard.
/// The parent passes in the raw `BDLPlay` stream and the two team
/// abbreviations for the score-line formatting.
///
/// `autoExpandOnScoring` (true for live games): when a new scoring
/// play arrives we auto-expand AND flip to Scoring mode — once. The
/// user-toggle guard prevents the section from re-popping open
/// after they've collapsed it.
struct PlaysView: View {
    let plays: [BDLPlay]
    let awayAbbr: String
    let homeAbbr: String
    let autoExpandOnScoring: Bool
    /// When true, render the header + expanded body inline without
    /// the standalone glass-card wrapper. The caller is responsible
    /// for providing the container (live situation / linescore card).
    var isEmbedded: Bool = false
    /// The game's final score, from the BOX SCORE rather than from these
    /// plays. Used only to reconcile the derived scoring list — see
    /// `reconciliation`. nil for a game still in progress, where there is no
    /// final to check against and the list is simply shown as it stands.
    var finalAwayScore: Int? = nil
    var finalHomeScore: Int? = nil

    @State private var isExpanded = false
    @State private var playsMode: PlaysMode = .scoring
    @State private var expandedHalfInnings: Set<String> = []
    /// Set of `AtBat.id` keys whose individual pitch list is
    /// currently expanded inside the All-mode view. Empty by
    /// default — every at-bat starts collapsed to just the
    /// outcome headline.
    @State private var expandedAtBats: Set<String> = []
    /// `true` once the user has tapped the header, so subsequent
    /// scoring-play arrivals don't fight whatever state they chose.
    @State private var hasUserToggled = false
    /// The half-inning key we last auto-expanded. Initial fetch
    /// auto-expands the latest half-inning; subsequent ticks only
    /// auto-expand when the latest half-inning key *changes* (i.e.
    /// the game advances to Top of the next inning, or to Bottom
    /// after the half flips), preserving the user's manual collapse
    /// of any inning that doesn't change.
    @State private var lastAutoExpandedHalfInningKey: String?
    /// Tracks the previous scoring-play count so we only react to
    /// the LATCH from N → N+1, not to every plays update.
    @State private var prevScoringCount = 0

    enum PlaysMode: String, Hashable, Identifiable, CaseIterable {
        case scoring = "Scoring"
        case all     = "All"
        var id: String { rawValue }
    }

    var body: some View {
        let content = VStack(alignment: .leading, spacing: 8) {
            Button {
                hasUserToggled = true
                withAnimation(.easeInOut(duration: 0.2)) {
                    isExpanded.toggle()
                }
            } label: {
                HStack {
                    Text("Plays")
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.primary)
                    Spacer()
                    Image(systemName: isExpanded ? "chevron.up" : "chevron.down")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                }
                .padding(.vertical, 4)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if isExpanded {
                Divider().opacity(0.4)
                Picker("", selection: $playsMode) {
                    ForEach(PlaysMode.allCases) { mode in
                        Text(mode.rawValue).tag(mode)
                    }
                }
                .pickerStyle(.segmented)

                if plays.isEmpty {
                    Text("Play-by-play not yet available")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, alignment: .leading)
                } else {
                    switch playsMode {
                    case .scoring: scoringPlaysList
                    case .all:     allPlaysList
                    }
                }
            }
        }
        return Group {
            if isEmbedded {
                content
            } else {
                content
                    .padding(.horizontal, 14)
                    .padding(.vertical, 12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 16))
                    .shadow(color: .black.opacity(0.06), radius: 8, x: 0, y: 3)
            }
        }
        .onChange(of: plays) { _, new in
            let newScoringCount = new.filter(\.scoringPlay).count
            defer { prevScoringCount = newScoringCount }
            // Auto-expand the latest half-inning. Triggers on every
            // transition to a new half-inning so the action stays
            // on-screen without scrolling; same-inning updates do
            // not touch the expansion set (so a user collapse of
            // the current inning stays collapsed). Prior auto-
            // expansions are left in place rather than reset — if
            // the user opened earlier innings, we don't fight them.
            if let last = new.last, let type = last.inningType {
                let latestKey = Self.halfInningKey(inningType: type, inning: last.inning)
                if latestKey != lastAutoExpandedHalfInningKey {
                    expandedHalfInnings.insert(latestKey)
                    lastAutoExpandedHalfInningKey = latestKey
                }
            }
            // Auto-expand on scoring-play arrival for live games,
            // unless the user has already toggled the section.
            if autoExpandOnScoring,
               !hasUserToggled,
               newScoringCount > prevScoringCount {
                withAnimation(.easeInOut(duration: 0.2)) {
                    isExpanded = true
                    playsMode  = .scoring
                }
            }
        }
    }

    // MARK: Scoring mode

    private var scoringPlaysList: some View {
        let rows = derivedScoringRows()
        return Group {
            if rows.isEmpty {
                Text("No scoring plays yet")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            } else {
                LazyVStack(alignment: .leading, spacing: 8) {
                    ForEach(rows) { row in
                        scoringPlayRow(row)
                    }
                    // SAY IT RATHER THAN SWALLOW IT. A list one run short of
                    // the game's own score is worse than one that admits the
                    // gap: the reader can see three runs listed under a 4-0
                    // final and would otherwise conclude the app had lost one.
                    // Shown only when the two genuinely disagree.
                    if let r = reconciliation, !r.matches {
                        Text("Showing \(r.shown) of \(r.expected) runs — "
                             + "the provider's play list is incomplete for this game.")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.top, 2)
                    }
                }
            }
        }
    }

    private func scoringPlayRow(_ row: ScoringRow) -> some View {
        let arrow = (row.inningType ?? "").hasPrefix("Top") ? "▲" : "▼"
        let ord = Self.ordinalInning(row.inning)
        // Pick the scoring team's abbreviation. Both-sides scoring
        // is rare (a single play that scores for both teams) but
        // possible on extremely weird sequences — fall back to a
        // generic label if neither side's delta resolves.
        let scoringAbbr: String? = {
            if row.scoredHome { return homeAbbr }
            if row.scoredAway { return awayAbbr }
            return nil
        }()
        return VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .top, spacing: 8) {
                Text("\(arrow)\(ord)")
                    .font(.caption.weight(.bold))
                    .foregroundStyle(.red)
                    .frame(width: 38, alignment: .leading)
                if let abbr = scoringAbbr {
                    Text(abbr)
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(.white)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(
                            Capsule().fill(
                                row.scoredHome
                                    ? Color.blue.opacity(0.8)
                                    : Color.orange.opacity(0.85)
                            )
                        )
                }
                // A run with no sentence within reach still gets a row: the
                // run is a fact even where the words are missing, and an
                // omitted row would put the list out against the final score.
                Text(row.text ?? "Run scored")
                    .font(.caption)
                    .foregroundStyle(.primary)
                    .lineLimit(3)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Text(scoreLineText(awayScore: row.awayScore, homeScore: row.homeScore))
                .font(.caption2)
                .foregroundStyle(.secondary)
                .padding(.leading, 46)
                .monospacedDigit()
        }
    }

    /// The scoring plays, DERIVED FROM THE SCORE rather than read off a flag.
    ///
    /// ⚠️ WHY NOT `scoringPlay`. The provider sets that flag, and it means a
    /// different thing in every era: on a 2026 game it marks three plays, each
    /// carrying a proper sentence; on a 2021 game it marks thirty-six,
    /// most of them individual pitches ("Pitch 1 : Strike 1 Looking"); on a
    /// 2005 game it marks nine rows that have no text at all. A flag whose
    /// meaning changes with the season is not a signal, and trusting it put
    /// pitch-by-pitch noise, a duplicated sentence and a backwards-running
    /// score on the tab a reader lands on first.
    ///
    /// A RUN CROSSING THE PLATE IS ERA-INDEPENDENT. Every row of every era
    /// carries the running score, so the rows where it rises are the scoring
    /// plays by definition — and a list built that way cannot double-count or
    /// run backwards, because it is monotonic by construction.
    ///
    /// TWO WRINKLES, both measured rather than assumed:
    ///
    /// 1. The score is peeked and reverted. A row mid-at-bat can report a
    ///    lower score than one already seen (five such rows in a 2026 game,
    ///    four in 2021, one in 2005), so the walk tracks a running MAX and
    ///    only records an increase against it.
    /// 2. THE ROW WHERE THE SCORE MOVES IS NEVER THE ROW THAT DESCRIBES IT.
    ///    In 2026 the increase lands on "Melton pitches to DeLuca" and the
    ///    sentence is two rows later; in 2021 on "Pitch 7 : Ball In Play" with
    ///    the sentence next; in 2005 on a blank row with the sentence three
    ///    later. So the row is found by the score and the words are taken from
    ///    the rows that follow it.
    private func derivedScoringRows() -> [ScoringRow] { Self.scoringRows(from: plays) }

    /// Pure, and deliberately so: the reconciliation test drives THIS with
    /// real play lists from several games in each era, which is the only way
    /// the denylist's staleness can be caught before a reader meets it.
    static func scoringRows(from plays: [BDLPlay]) -> [ScoringRow] {
        guard !plays.isEmpty else { return [] }
        var rows: [ScoringRow] = []
        var maxHome = 0
        var maxAway = 0
        for (i, play) in plays.enumerated() {
            let newHome = max(maxHome, play.homeScore)
            let newAway = max(maxAway, play.awayScore)
            guard newHome > maxHome || newAway > maxAway else { continue }
            let scoredHome = newHome > maxHome
            let scoredAway = newAway > maxAway
            maxHome = newHome
            maxAway = newAway
            rows.append(ScoringRow(
                id:         play.order,
                inning:     play.inning,
                inningType: play.inningType,
                text:       Self.descriptionFollowing(plays, from: i),
                awayScore:  newAway,
                homeScore:  newHome,
                scoredHome: scoredHome,
                scoredAway: scoredAway,
            ))
        }
        return rows
    }

    /// The first sentence at or after `index` that describes a play rather
    /// than narrating the count.
    ///
    /// ⚠️ THIS IS A DENYLIST AND DENYLISTS GO STALE. If the provider invents a
    /// new noise format, it will be mistaken for a description and a scoring
    /// row will read "Pitch 3 : Ball 2". That is exactly why the list is
    /// RECONCILED against the final score — see `reconciliation`. The
    /// arithmetic is the guard; this is only the heuristic it protects.
    private static func descriptionFollowing(_ plays: [BDLPlay], from index: Int) -> String? {
        let window = min(plays.count, index + 8)
        for j in index..<window {
            guard let raw = plays[j].text?.trimmingCharacters(in: .whitespacesAndNewlines),
                  !raw.isEmpty else { continue }
            if isNarration(raw) { continue }
            return raw
        }
        return nil
    }

    /// True for a row that narrates rather than describes: a pitch count, an
    /// at-bat announcement, an inning header, a lineup change.
    static func isNarration(_ text: String) -> Bool {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if t.isEmpty { return true }
        let lower = t.lowercased()
        if lower.hasPrefix("pitch ") { return true }          // "Pitch 1 : Ball 1"
        if lower.contains(" pitches to ") { return true }     // "X pitches to Y"
        if lower == "lineup change" { return true }
        if lower.hasPrefix("top of the") || lower.hasPrefix("bottom of the")
            || lower.hasPrefix("end of the") || lower.hasPrefix("middle of the") { return true }
        if lower.hasSuffix(" batting") { return true }         // "C Figgins batting"
        return false
    }

    /// Whether the derived list accounts for every run the game finished with.
    ///
    /// THE ARITHMETIC IS THE REAL GUARD, and it catches both directions at
    /// once: a sentence the denylist wrongly discarded leaves the list short,
    /// and a noise row promoted to a scoring play leaves it long. Neither
    /// depends on the denylist being complete, which is the point — a list
    /// that does not add up to the game's own score says so instead of
    /// quietly being one run out.
    ///
    /// Compared against the BOX SCORE's final, not the plays feed's own last
    /// row, so it is a genuine cross-source check rather than a tautology.
    private var reconciliation: (matches: Bool, shown: Int, expected: Int)? {
        guard let a = finalAwayScore, let h = finalHomeScore else { return nil }
        return Self.reconcile(plays: plays, finalAway: a, finalHome: h)
    }

    /// Runs accounted for by the derived list, against the box score's final.
    static func reconcile(plays: [BDLPlay], finalAway: Int,
                          finalHome: Int) -> (matches: Bool, shown: Int, expected: Int) {
        let rows = scoringRows(from: plays)
        let shown = (rows.last?.awayScore ?? 0) + (rows.last?.homeScore ?? 0)
        return (shown == finalAway + finalHome, shown, finalAway + finalHome)
    }

    struct ScoringRow: Identifiable, Hashable {
        let id: Int          // BDLPlay.order — unique within a game
        let inning: Int
        let inningType: String?
        /// Taken from a LATER row than the one that moved the score; nil when
        /// no describable row follows within the window.
        let text: String?
        let awayScore: Int
        let homeScore: Int
        let scoredHome: Bool
        let scoredAway: Bool
    }

    // MARK: All mode (PA-grouped)

    private var allPlaysList: some View {
        // Reverse the half-inning groupings so the most-recent
        // half-inning lands at the top of the list. `groupedHalfInnings`
        // preserves BDL's chronological order (oldest first); reading
        // a long live game from the top would otherwise require
        // scrolling all the way down to see what just happened.
        let groups = Array(Self.groupedHalfInnings(plays).reversed())
        return LazyVStack(alignment: .leading, spacing: 0) {
            ForEach(groups) { half in
                halfInningSection(half: half)
            }
        }
    }

    private func halfInningSection(half: HalfInning) -> some View {
        let isHalfExpanded = expandedHalfInnings.contains(half.id)
        let arrow = half.inningType.hasPrefix("Top") ? "▲" : "▼"
        let ord = Self.ordinalInning(half.inning)
        let atBatCount = half.atBats.count
        return VStack(alignment: .leading, spacing: 4) {
            Button {
                withAnimation(.easeInOut(duration: 0.18)) {
                    if isHalfExpanded { expandedHalfInnings.remove(half.id) }
                    else              { expandedHalfInnings.insert(half.id) }
                }
            } label: {
                HStack(spacing: 8) {
                    Text("\(arrow) \(ord)")
                        .font(.caption.weight(.bold))
                        .foregroundStyle(.red)
                    Text("(\(atBatCount) AB\(atBatCount == 1 ? "" : "s"))")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                    Spacer()
                    Image(systemName: isHalfExpanded ? "chevron.down" : "chevron.right")
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(.secondary)
                }
                .padding(.vertical, 6)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if isHalfExpanded {
                ForEach(half.atBats) { ab in
                    atBatRow(ab)
                }
            }
            Divider().opacity(0.3)
        }
    }

    private func atBatRow(_ ab: AtBat) -> some View {
        let isPAExpanded = expandedAtBats.contains(ab.id)
        // Earlier pitches are the intermediate ball/strike/foul
        // calls; the LAST play in the PA is the outcome and gets
        // promoted to the headline. The expand-pitches button only
        // shows when there's something to expand beyond the
        // outcome row.
        let pitchCount = max(0, ab.plays.count - 1)
        let resultText = ab.plays.last?.text ?? ab.batterText
        return VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .top, spacing: 8) {
                VStack(alignment: .leading, spacing: 2) {
                    if let batter = ab.batterText {
                        Text(batter)
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                    Text(resultText ?? "—")
                        .font(.caption)
                        .foregroundStyle(.primary)
                        .lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
                if pitchCount > 0 {
                    Button {
                        withAnimation(.easeInOut(duration: 0.18)) {
                            if isPAExpanded { expandedAtBats.remove(ab.id) }
                            else            { expandedAtBats.insert(ab.id) }
                        }
                    } label: {
                        HStack(spacing: 2) {
                            Image(systemName: isPAExpanded ? "chevron.down" : "chevron.right")
                                .font(.caption2.weight(.semibold))
                            Text("\(pitchCount) pitch\(pitchCount == 1 ? "" : "es")")
                                .font(.caption2)
                        }
                        .foregroundStyle(.secondary)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.leading, 12)
            if isPAExpanded, pitchCount > 0 {
                // Show the leading intermediate pitches (everything
                // up to but not including the final outcome).
                ForEach(ab.plays.dropLast(), id: \.order) { pitch in
                    Text(pitch.text ?? "")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.leading, 28)
                }
            }
        }
        .padding(.vertical, 2)
    }

    // MARK: Helpers

    private func scoreLineText(awayScore: Int, homeScore: Int) -> String {
        "\(awayAbbr) \(awayScore), \(homeAbbr) \(homeScore)"
    }

    private struct HalfInning: Identifiable, Hashable {
        let id: String
        let inning: Int
        let inningType: String
        let atBats: [AtBat]
    }

    private struct AtBat: Identifiable, Hashable {
        /// Stable key combining the half-inning id with the order
        /// of the at-bat's first play, so SwiftUI's diffing keeps
        /// state aligned across live-poll updates.
        let id: String
        /// Parsed batter name from the "Start Batter/Pitcher" marker
        /// ("Quintana pitches to McCutchen" → "McCutchen"). nil if
        /// the marker text didn't follow the expected shape or if
        /// the half-inning began without one.
        let batterText: String?
        /// Every displayed play in this PA, in order. The LAST one
        /// is treated as the at-bat result; earlier ones are the
        /// intermediate pitches surfaced by the expand button.
        let plays: [BDLPlay]
    }

    private static func groupedHalfInnings(_ plays: [BDLPlay]) -> [HalfInning] {
        // First pass: bucket by (normalized inningType, inning),
        // preserving BDL's chronological order. Store the
        // NORMALIZED `Top` / `Bottom` so the half-inning header's
        // arrow lookup (`hasPrefix("Top")`) doesn't get fooled by
        // BDL casing variants or transition markers.
        var keys: [String] = []
        var buckets: [String: (inning: Int, type: String, plays: [BDLPlay])] = [:]
        for p in plays {
            let rawType = p.inningType ?? "?"
            let key = halfInningKey(inningType: rawType, inning: p.inning)
            let normalized = normalizedInningType(rawType)
            if buckets[key] == nil {
                keys.append(key)
                buckets[key] = (p.inning, normalized, [p])
            } else {
                buckets[key]?.plays.append(p)
            }
        }
        // Second pass: split each bucket into at-bats by walking
        // the play list and using "Start Batter/Pitcher" markers
        // as PA boundaries. Inning-transition plays and
        // start/end markers are dropped from the display set.
        return keys.compactMap { key in
            guard let b = buckets[key] else { return nil }
            let atBats = atBats(in: b.plays, halfInningKey: key)
            return HalfInning(
                id:         key,
                inning:     b.inning,
                inningType: b.type,
                atBats:     atBats,
            )
        }
    }

    private static func atBats(in plays: [BDLPlay], halfInningKey: String) -> [AtBat] {
        var result: [AtBat] = []
        var currentBatter: String? = nil
        var currentPlays: [BDLPlay] = []
        var firstOrder: Int? = nil

        func flush() {
            guard !currentPlays.isEmpty || currentBatter != nil else { return }
            let orderKey = firstOrder.map(String.init) ?? "x\(result.count)"
            result.append(AtBat(
                id:         "\(halfInningKey)/\(orderKey)",
                batterText: currentBatter,
                plays:      currentPlays,
            ))
            currentBatter = nil
            currentPlays = []
            firstOrder = nil
        }

        for p in plays {
            if p.type == "Start Batter/Pitcher" {
                flush()
                currentBatter = parseBatter(from: p.text)
                firstOrder = p.order
                continue
            }
            if !shouldDisplay(p) { continue }
            currentPlays.append(p)
            if firstOrder == nil { firstOrder = p.order }
        }
        flush()
        return result
    }

    /// True iff the play should appear in the All-mode display.
    /// Filters out inning markers and Start/End Batter/Pitcher
    /// transitions, plus the textual variants BDL sometimes ships
    /// without the structured type ("Middle of the 7th").
    private static func shouldDisplay(_ p: BDLPlay) -> Bool {
        let excludedTypes: Set<String> = [
            "Start Inning", "End Inning",
            "Start Batter/Pitcher", "End Batter/Pitcher",
        ]
        if let t = p.type, excludedTypes.contains(t) { return false }
        if let text = p.text {
            if text.hasPrefix("Middle of") { return false }
            if text.hasPrefix("End of")    { return false }
            if text.hasPrefix("Start of")  { return false }
        }
        return true
    }

    /// "Quintana pitches to McCutchen" → "McCutchen".
    /// Falls back to the raw text if the " to " separator isn't
    /// found.
    private static func parseBatter(from text: String?) -> String? {
        guard let text = text else { return nil }
        if let range = text.range(of: " to ") {
            return String(text[range.upperBound...])
        }
        return text
    }

    /// Normalize the `inningType` to a stable `"Top"` / `"Bottom"`
    /// label before keying. Without this, BDL's casing variants
    /// (`"Top"` vs `"TOP"` vs `"top"`) and mid-inning marker
    /// types (`"Mid-Top"`, `"End-Bottom"`, `""`) build separate
    /// buckets for what's really the same half-inning, producing
    /// three "▼ 1st" headers in the UI.
    fileprivate static func halfInningKey(inningType: String, inning: Int) -> String {
        "\(normalizedInningType(inningType))-\(inning)"
    }

    private static func normalizedInningType(_ raw: String) -> String {
        let lower = raw.lowercased()
        if lower.contains("top") { return "Top" }
        if lower.contains("bot") { return "Bottom" }
        // Mid-inning transition markers ("Middle of 7th") and
        // unknowns fall through to "Top" — they're filtered out of
        // the display by `shouldDisplay` anyway, so the bucket
        // membership only matters for grouping.
        return "Top"
    }

    private static func ordinalInning(_ n: Int) -> String {
        switch n {
        case 1:  return "1st"
        case 2:  return "2nd"
        case 3:  return "3rd"
        default: return "\(n)th"
        }
    }
}
