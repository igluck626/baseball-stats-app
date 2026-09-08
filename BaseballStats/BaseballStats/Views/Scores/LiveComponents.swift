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
    /// Plate appearances for the same game, joined onto the at-bats
    /// to supply the batted-ball metrics — see `attachContactMetrics`.
    /// Empty is fine: every at-bat then renders without a metric line,
    /// which is what a pre-2015 game gets anyway.
    var plateAppearances: [BDLPlateAppearance] = []
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
    /// The at-bat whose detail sheet is open, or nil. Replaces the
    /// old in-line pitch expansion: the pitch list, the batted-ball
    /// metrics and the batter's name all live in the sheet now, so the
    /// row itself stays one sentence long.
    @State private var selectedPlay: PlayDetail?
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
    /// Drives the at-bat row's layout. At the accessibility sizes the
    /// trailing out count is moved BELOW the sentence rather than
    /// beside it — see `battedRow`.
    @Environment(\.dynamicTypeSize) private var typeSize

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
        .sheet(item: $selectedPlay) { detail in
            PlayDetailSheet(detail: detail)
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
        let groups = Array(
            Self.attachContactMetrics(
                Self.groupedHalfInnings(plays),
                plateAppearances: plateAppearances,
            ).reversed(),
        )
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
                let marks = Self.outMarks(half.atBats)
                ForEach(Array(zip(half.atBats, marks)), id: \.0.id) { ab, mark in
                    atBatRow(ab, outsMark: mark)
                }
            }
            Divider().opacity(0.3)
        }
    }

    /// Build the sheet's payload for one at-bat.
    ///
    /// The heading is the outcome NOUN rather than the sentence —
    /// "Sacrifice Fly", "Home Run", "Strikeout" — so the sheet opens
    /// with what happened before it says how. The sentence follows in
    /// full, which is where the tail a two-line row may have clipped
    /// is recovered.
    private static func detail(for ab: AtBat, sentence: String) -> PlayDetail {
        PlayDetail(
            id:       ab.id,
            title:    outcomeTitle(ab),
            sentence: sentence,
            // nil for any plate appearance that put no ball in play, so
            // a strikeout's sheet omits the metrics block entirely
            // rather than showing it with dashes — the same rule the
            // row follows.
            contact:  ab.contact,
            pitches:  pitchRows(ab),
        )
    }

    /// The outcome as a noun.
    ///
    /// A ball in play names itself: BDL types that row "Home Run",
    /// "Sacrifice Fly", "Ground Out", and its text is "Pitch N : Ball
    /// In Play", which is what identifies it. A plate appearance that
    /// ended without contact has no such row, so the last pitch's call
    /// names it instead — a third strike means a strikeout, ball four
    /// a walk.
    private static func outcomeTitle(_ ab: AtBat) -> String {
        let pitches = pitchRows(ab)
        if let bip = pitches.last(where: { ($0.text ?? "").contains("Ball In Play") }),
           let type = bip.type, !type.isEmpty {
            return type
        }
        switch pitches.last?.type ?? "" {
        case let t where t.hasPrefix("Automatic Ball"): return "Intentional Walk"
        case let t where t.hasPrefix("Ball"):           return "Walk"
        case let t where t.hasPrefix("Strike"):         return "Strikeout"
        case "Hit By Pitch":                            return "Hit By Pitch"
        default:                                        return "Play"
        }
    }

    /// The pitches of the plate appearance, in order.
    ///
    /// Matched on the "Pitch N :" text rather than on the presence of
    /// a pitch type: an intentional walk's automatic balls carry no
    /// type, and a steal or substitution interleaved into the at-bat
    /// carries no "Pitch" prefix. Type alone would drop the first and
    /// admit the second.
    private static func pitchRows(_ ab: AtBat) -> [BDLPlay] {
        ab.plays.filter { ($0.text ?? "").hasPrefix("Pitch ") }
    }

    /// The number to print in each at-bat's trailing "N Out", or nil
    /// where the plate appearance made no out.
    ///
    /// `AtBat.outs` is a RUNNING TOTAL after the PA, so it repeats
    /// across at-bats that didn't retire anyone — Top 7 of game
    /// 5059936 reads `1,1,2,2,3`. Printing it on every row is clutter:
    /// a mark appears only where the count actually advanced, giving
    /// `1,·,2,·,3`.
    ///
    /// A double play advances the count by two and prints the new
    /// total (Bottom 7's `Rojas grounded into double play` prints
    /// "2 Out"), which is the count a reader wants: how many are out
    /// now.
    private static func outMarks(_ atBats: [AtBat]) -> [Int?] {
        var running = 0
        return atBats.map { ab in
            guard let outs = ab.outs, outs > running else { return nil }
            running = outs
            return outs
        }
    }

    /// One at-bat, in the shape of MLB's play list: the outcome
    /// sentence, the out count trailing it, and the score beneath only
    /// where the score changed. Everything else — the batter's name,
    /// the batted-ball metrics, the pitch list — is behind a tap, in
    /// `PlayDetailSheet`. The row's whole job is to be readable at a
    /// glance while an inning scrolls past.
    private func atBatRow(_ ab: AtBat, outsMark: Int?) -> some View {
        // Announcements — relief changes, defensive changes, pinch-hit
        // notices — reach here as pseudo-at-bats with no batter. They
        // are real events a reader following the inning wants, so they
        // stay in the list, but they are not at-bats: quieter, flush
        // to the margin the at-bats indent from, no out count and no
        // tap. See `announcementRow`.
        Group {
            if ab.batterId == nil {
                announcementRow(ab)
            } else {
                battedRow(ab, outsMark: outsMark)
            }
        }
    }

    private func battedRow(_ ab: AtBat, outsMark: Int?) -> some View {
        let sentence = ab.resultText ?? ab.plays.last?.text ?? ab.batterText ?? "—"
        return Button {
            selectedPlay = Self.detail(for: ab, sentence: sentence)
        } label: {
            VStack(alignment: .leading, spacing: 3) {
                // The out count is a trailing sibling of the sentence,
                // which is exactly the arrangement that broke the old
                // pitch button at the accessibility sizes — it claimed
                // half the row and truncated the sentence to nothing.
                // So above those sizes it moves below the sentence,
                // where it has the full width.
                if typeSize.isAccessibilitySize {
                    sentenceText(sentence)
                    if let mark = outsMark { outsText(mark) }
                } else {
                    HStack(alignment: .firstTextBaseline, spacing: 10) {
                        sentenceText(sentence)
                        Spacer(minLength: 0)
                        if let mark = outsMark {
                            // `layoutPriority` so the sentence yields
                            // to it rather than the other way round —
                            // "1 Out" truncated to "1 O…" would be
                            // worse than one fewer word of sentence.
                            outsText(mark).layoutPriority(1)
                        }
                    }
                }
                // Only where the score actually moved, and in the
                // format the rest of the app already uses.
                if ab.runs != nil, let away = ab.awayScore, let home = ab.homeScore {
                    Text(scoreLineText(awayScore: away, homeScore: home))
                        .font(.footnote.weight(.semibold))
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(.leading, 12)
            .padding(.vertical, 7)
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func sentenceText(_ sentence: String) -> some View {
        Text(sentence)
            .font(.subheadline)
            .foregroundStyle(.primary)
            .multilineTextAlignment(.leading)
            // Two lines holds all but the longest sentence at the
            // default sizes, and the tail of one that overflows is
            // carried in full by the sheet. At the accessibility sizes
            // the same sentence needs five or six lines, and clipping
            // it there would clip the only thing the row says.
            .lineLimit(typeSize.isAccessibilitySize ? nil : 2)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func outsText(_ mark: Int) -> some View {
        Text("\(mark) Out")
            .font(.subheadline)
            .foregroundStyle(.secondary)
            .lineLimit(1)
    }

    /// A relief change, defensive change or pinch-hit notice. Flush to
    /// the margin the at-bats indent from, in a smaller secondary face,
    /// with no out count and no tap target — so it reads as a note
    /// between at-bats rather than as an at-bat that failed to render.
    private func announcementRow(_ ab: AtBat) -> some View {
        Text(ab.resultText ?? ab.plays.last?.text ?? "—")
            .font(.footnote)
            .foregroundStyle(.secondary)
            .italic()
            .lineLimit(typeSize.isAccessibilitySize ? nil : 2)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.vertical, 5)
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
        /// BDL id of the batter, off the "Start Batter/Pitcher"
        /// marker. This is the JOIN KEY to the plate-appearance feed
        /// — see `attachContactMetrics`. nil for the pseudo-at-bats
        /// built from rows that arrive before any marker (relief
        /// announcements, defensive changes), which correctly join
        /// to nothing.
        let batterId: Int?
        /// Every displayed play in this PA, in order. The LAST one
        /// is treated as the at-bat result; earlier ones are the
        /// intermediate pitches surfaced by the expand button.
        let plays: [BDLPlay]
        /// Text of this at-bat's own `Play Result` row.
        ///
        /// ⚠️ NOT `plays.last`, which the headline used to read. The
        /// play stream interleaves: on game 5059936 the row
        /// "Smith hit for Feduccia" arrives AFTER Tucker's
        /// `End Batter/Pitcher` and BEFORE Smith's
        /// `Start Batter/Pitcher`, so it lands at the end of Tucker's
        /// group and headlined Tucker's at-bat with another player's
        /// substitution. Same shape put "Pitch 5 : Ball In Play" on
        /// Hernández's intentional walk. Reading the batter's own
        /// result row instead keeps the headline consistent with the
        /// outs and metrics beside it, which are derived from that
        /// same row.
        let resultText: String?
        /// Total outs AFTER this plate appearance, or nil when the
        /// at-bat carries no `Play Result` row of its own.
        ///
        /// ⚠️ Source is the `Play Result` row on `/plays`, and ONLY a
        /// row whose `batterId` is non-nil. The play stream also
        /// carries `Play Result` rows for steals, substitutions and
        /// relief announcements ("Dreyer relieved Halvorsen",
        /// "De La Cruz stole second."); all 15 of them in game
        /// 5059936 have `batterId == nil` AND `outs == 0`, so that
        /// zero is a placeholder, not a count. Reading it as a count
        /// walks the running total backwards mid-inning.
        ///
        /// ⚠️ NOT `/plate_appearances`' own `outs` field, which needs
        /// no join and is wrong — see the note on
        /// `BDLPlateAppearance`.
        let outs: Int?
        /// Runs driven in by this at-bat, from `scoreValue` on the
        /// same `Play Result` row, and the score after it.
        ///
        /// ⚠️ Gated on `scoringPlay`, NOT on a score delta between
        /// rows. Differencing looks cleaner and fails: the
        /// `Start Batter/Pitcher` rows carry a different score than
        /// the surrounding rows, so on game 5059936 a delta finds 16
        /// "changes" — some NEGATIVE — against the 6 real scoring
        /// events `scoringPlay` identifies, which sum to the correct
        /// 9 runs and the correct 3-6 final.
        let runs: Int?
        let awayScore: Int?
        let homeScore: Int?
        /// Batted-ball metrics from the joined plate appearance, or
        /// nil when this at-bat put no ball in play (strikeout, walk)
        /// or joined to no PA at all. Rendered as an absent line
        /// rather than as zeros.
        let contact: BDLPitchDetail?
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
        var currentBatterId: Int? = nil
        var currentPlays: [BDLPlay] = []
        var firstOrder: Int? = nil

        func flush() {
            guard !currentPlays.isEmpty || currentBatter != nil else { return }
            let orderKey = firstOrder.map(String.init) ?? "x\(result.count)"
            // This at-bat's OWN result row. Scanning from the back
            // finds the outcome; requiring a non-nil `batterId`
            // skips the interleaved steal / substitution rows, whose
            // `outs` is a placeholder zero. A pseudo-at-bat built
            // only from those rows finds none, and renders with no
            // gutter mark and no run line.
            let resultRow = currentPlays.last { $0.type == "Play Result" && $0.batterId != nil }
            result.append(AtBat(
                id:         "\(halfInningKey)/\(orderKey)",
                batterText: currentBatter,
                batterId:   currentBatterId,
                plays:      currentPlays,
                resultText: resultRow?.text,
                outs:       resultRow?.outs,
                runs:       (resultRow?.scoringPlay ?? false) ? (resultRow?.scoreValue ?? 1) : nil,
                awayScore:  (resultRow?.scoringPlay ?? false) ? resultRow?.awayScore : nil,
                homeScore:  (resultRow?.scoringPlay ?? false) ? resultRow?.homeScore : nil,
                contact:    nil,   // filled in by `attachContactMetrics`
            ))
            currentBatter = nil
            currentBatterId = nil
            currentPlays = []
            firstOrder = nil
        }

        for p in plays {
            if p.type == "Start Batter/Pitcher" {
                flush()
                currentBatter = parseBatter(from: p.text)
                currentBatterId = p.batterId
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

    /// Join the plate-appearance feed onto the grouped at-bats so each
    /// one carries its batted-ball metrics.
    ///
    /// ⚠️ The key is INNING + HALF + BATTER, consumed in sequence —
    /// never a list index. The two feeds do not line up positionally:
    /// `/plays` carries rows `/plate_appearances` has no notion of
    /// (steals, pinch-hit announcements, defensive changes, relief
    /// announcements), and each one an index join steps over shifts
    /// every metric after it onto the wrong batter. Measured on game
    /// 5059936: an index join hung 95.5 mph on "De La Cruz stole
    /// second" and pushed the rest of the inning one row late, so an
    /// intentional walk rendered an exit velocity.
    ///
    /// A queue per key rather than a single value, because a batter
    /// can bat twice in one half-inning when the order turns over;
    /// the Nth at-bat by a batter takes the Nth PA by that batter.
    /// Anything that fails to match keeps `contact == nil` and renders
    /// with no metric line — absent, not zero.
    private static func attachContactMetrics(
        _ halves: [HalfInning], plateAppearances: [BDLPlateAppearance],
    ) -> [HalfInning] {
        guard !plateAppearances.isEmpty else { return halves }

        struct Key: Hashable {
            let inning: Int
            let half: String
            let batterId: Int
        }
        // `paNumber` is not dense — it restarts and skips — so it is
        // used purely as a sort key here, never as an index.
        var queues: [Key: [BDLPitchDetail?]] = [:]
        for pa in plateAppearances.sorted(by: {
            ($0.inning, $0.paNumber) < ($1.inning, $1.paNumber)
        }) {
            guard let bid = pa.batterId else { continue }
            let key = Key(
                inning:   pa.inning,
                half:     normalizedInningType(pa.halfInning ?? ""),
                batterId: bid,
            )
            // The contact metrics sit on the LAST pitch of the PA —
            // the one that was put in play. Earlier pitches carry a
            // type and a speed but no exit velocity.
            queues[key, default: []].append(pa.pitches?.last)
        }

        var cursor: [Key: Int] = [:]
        return halves.map { half in
            HalfInning(
                id:         half.id,
                inning:     half.inning,
                inningType: half.inningType,
                atBats:     half.atBats.map { ab in
                    guard let bid = ab.batterId else { return ab }
                    let key = Key(
                        inning: half.inning, half: half.inningType, batterId: bid,
                    )
                    let i = cursor[key, default: 0]
                    guard let queue = queues[key], i < queue.count else { return ab }
                    cursor[key] = i + 1
                    // A PA with no ball in play yields nil here, which
                    // is the same rendering as no PA at all.
                    guard let detail = queue[i], detail.exitVelocity != nil else { return ab }
                    return AtBat(
                        id:         ab.id,
                        batterText: ab.batterText,
                        batterId:   ab.batterId,
                        plays:      ab.plays,
                        resultText: ab.resultText,
                        outs:       ab.outs,
                        runs:       ab.runs,
                        awayScore:  ab.awayScore,
                        homeScore:  ab.homeScore,
                        contact:    detail,
                    )
                },
            )
        }
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
