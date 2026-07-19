//
//  AskAnswerView.swift
//  BaseballStats
//
//  Renders one `AskResponse` inside an exchange card. Prose-first: the
//  phrased sentence leads, and any structured data lives behind a
//  "Show details" expander so the default read is a plain-English answer.
//
//  Two deliberate styling choices:
//   • A decline / out-of-scope answer ("we don't have that") is a GOOD
//     answer, so it gets a NEUTRAL info treatment (small info icon,
//     secondary text) — never a warning triangle or a red/orange color.
//   • The coverage / count-data caveat is a subtle, informative footnote,
//     not an alarm.
//

import SwiftUI

struct AskAnswerView: View {
    let response: AskResponse
    /// Tapping a same-name candidate re-asks pinned to that player.
    let onSelectCandidate: (AskCandidate) -> Void
    /// Tapping a leaderboard row opens that player's profile.
    let onSelectPlayer: (Int) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if response.ambiguous == true, let candidates = candidates, !candidates.isEmpty {
                ambiguousSection(candidates)
            } else if isNeutralInfo {
                neutralInfoSection
            } else if let splits = response.splits, !splits.isEmpty {
                // DATA-FIRST: a split IS the table. No prose above it, no
                // expander — the numbers are the answer.
                splitsSection(splits)
            } else if let leaders = response.leaders, !leaders.isEmpty {
                // DATA-FIRST: a leaderboard IS the ranked list.
                leadersSection(leaders)
            } else if let rates = response.rates {
                // DATA-FIRST: a stat line IS the answer — shown up front, with
                // the extra component counts behind a disclosure. No prose
                // reciting AVG/OBP/SLG/OPS the row already shows.
                ratesSection(rates)
            } else if let m = response.milestone, m.reached == true {
                // DATA-FIRST: the milestone game IS the answer (a not-reached or
                // unpinpointable milestone arrives as prose in `answer`).
                milestoneSection(m)
            } else if let streak = response.streak, (streak.length ?? 0) >= 1 {
                streakSection(streak)
            } else if let span = response.span, span.total != nil {
                spanSection(span)
            } else {
                // A single count: the phrased sentence IS the answer, with any
                // sample plays behind a disclosure.
                answerSection
            }
        }
    }

    // MARK: - Case detection

    private var candidates: [AskCandidate]? { response.player_resolved?.candidates }

    /// A decline or an out-of-scope note — both are honest "no answer" results
    /// that should read calmly, not as failures.
    private var isNeutralInfo: Bool {
        response.declined == true || response.out_of_scope == true
    }

    private var hasDetails: Bool {
        !(response.sample?.isEmpty ?? true)
    }

    // MARK: - Single answer (count): prose + optional sample disclosure

    private var answerSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            if let answer = response.answer, !answer.isEmpty {
                // Rendered as markdown (defense in depth): if the model ever
                // emits **bold** or a list despite the plain-text system
                // prompt, it renders correctly instead of leaking literal
                // ** / # into the UI. Plain prose is unaffected.
                Text(.init(answer))
                    .font(.body)
                    .foregroundStyle(.primary)
                    .fixedSize(horizontal: false, vertical: true)
            } else if let count = response.count {
                // Fallback: the rate line couldn't be computed — show the bare
                // count so the number the user asked for is never lost.
                Text(count.formatted(.number))
                    .font(.title2.weight(.bold))
                    .monospacedDigit()
            }

            if hasDetails {
                DisclosureGroup {
                    detailContent
                        .padding(.top, 6)
                } label: {
                    Text("More")
                        .font(.subheadline.weight(.semibold))
                }
                .tint(.secondary)
            }

            coverageFootnote
        }
    }

    /// Detail for a single count: the sample plays. (Rates, leaders, and
    /// splits are data-first and never routed through here.)
    @ViewBuilder
    private var detailContent: some View {
        if let sample = response.sample, !sample.isEmpty {
            sampleList(sample)
        }
    }

    // MARK: - Data-first sections

    /// A rate line as a horizontal stat table with the asked-for stat
    /// highlighted. Extra components and (for a small situational count) the
    /// sample plays sit behind "More". No prose.
    private func ratesSection(_ rates: AskRates) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            StatTable(columns: rateColumns(rates, highlight: response.highlighted_stat))
            if ratesHasMore(rates) {
                DisclosureGroup {
                    VStack(alignment: .leading, spacing: 12) {
                        if let components = rateComponents(rates) {
                            Text(components)
                                .font(.caption)
                                .monospacedDigit()
                                .foregroundStyle(.secondary)
                        }
                        if let sample = response.sample, !sample.isEmpty {
                            sampleList(sample)
                        }
                    }
                    .padding(.top, 4)
                } label: {
                    Text("More")
                        .font(.subheadline.weight(.semibold))
                }
                .tint(.secondary)
            }
            coverageFootnote
        }
    }

    private func ratesHasMore(_ r: AskRates) -> Bool {
        rateComponents(r) != nil || !(response.sample?.isEmpty ?? true)
    }

    /// The component counts not in the headline table: 2B/3B/BB/HBP/SF/SO.
    private func rateComponents(_ r: AskRates) -> String? {
        var parts: [String] = []
        if let x = r.PA { parts.append("\(x.formatted(.number)) PA") }
        if let x = r.doubles { parts.append("\(x) 2B") }
        if let x = r.triples { parts.append("\(x) 3B") }
        if let x = r.BB { parts.append("\(x) BB") }
        if let x = r.HBP { parts.append("\(x) HBP") }
        if let x = r.SF { parts.append("\(x) SF") }
        if let x = r.SO { parts.append("\(x) SO") }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    private func splitsSection(_ splits: [AskSplit]) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            splitsTable(splits)
            coverageFootnote
        }
    }

    private func leadersSection(_ leaders: [AskLeader]) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            if let qualifier = leaderQualifier {
                Text(qualifier)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            leadersTable(leaders)
            coverageFootnote
        }
    }

    /// Rate leaderboards carry `stat` + `min_pa` (previously stated in prose);
    /// surface them as a caption since there's no prose now. Count boards carry
    /// neither, so this stays nil for them.
    private var leaderQualifier: String? {
        // Streak/span leaderboards carry an explicit title ("Longest hitting
        // streak"); rate boards fall back to stat + min-PA.
        if let title = response.leaderboard_title, !title.isEmpty { return title }
        guard let stat = response.stat else { return nil }
        if let minPA = response.min_pa {
            return "\(stat) · min. \(minPA) PA in the situation"
        }
        return stat
    }

    // MARK: - Leaders

    private func leadersTable(_ leaders: [AskLeader]) -> some View {
        VStack(spacing: 0) {
            ForEach(Array(leaders.enumerated()), id: \.element.id) { index, leader in
                Button {
                    if let id = leader.mlbam_id { onSelectPlayer(id) }
                } label: {
                    HStack(spacing: 10) {
                        Text("\(leader.rank ?? index + 1)")
                            .font(.callout.weight(.semibold))
                            .monospacedDigit()
                            .foregroundStyle(.secondary)
                            .frame(width: 22, alignment: .trailing)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(leader.player_name ?? "—")
                                .font(.subheadline.weight(.medium))
                                .foregroundStyle(.primary)
                                .lineLimit(1)
                                .minimumScaleFactor(0.8)
                            if let sub = leader.subtitle, !sub.isEmpty {
                                Text(sub)
                                    .font(.caption2)
                                    .foregroundStyle(.secondary)
                                    .lineLimit(1)
                            }
                        }
                        Spacer(minLength: 8)
                        Text(leaderValue(leader))
                            .font(.subheadline.weight(.semibold))
                            .monospacedDigit()
                        if leader.mlbam_id != nil {
                            Image(systemName: "chevron.right")
                                .font(.caption2.weight(.semibold))
                                .foregroundStyle(.tertiary)
                        }
                    }
                    .padding(.vertical, 7)
                }
                .buttonStyle(.plain)
                .disabled(leader.mlbam_id == nil)

                if index < leaders.count - 1 { Divider() }
            }
        }
    }

    /// A streak/span board shows the length/total (`value`); a count board shows
    /// the count; a rate board shows the qualifying stat.
    private func leaderValue(_ leader: AskLeader) -> String {
        if let v = leader.value {
            return v.formatted(.number)
        }
        if let count = leader.count {
            return count.formatted(.number)
        }
        let value: Double?
        switch response.stat {
        case "AVG": value = leader.AVG
        case "OBP": value = leader.OBP
        case "SLG": value = leader.SLG
        default:    value = leader.OPS
        }
        return LeaderboardRow.formatted(value, as: .threeDecimal)
    }

    // MARK: - Stat table (shared by rate lines, split rows, situational counts)

    // The eight columns kept on screen. PA moved to "More" — with 4-digit PAs
    // the nine-wide table got tight — but it's prepended back (like any "More"
    // stat) when it's the one the question asked about.
    private static let coreStatKeys: Set<String> =
        ["AB", "H", "HR", "RBI", "AVG", "OBP", "SLG", "OPS"]

    /// Columns for a rate line: the core eight, with the asked-for stat
    /// highlighted. If that stat isn't one of the eight (PA/SO/BB/2B/3B/HBP),
    /// it's prepended so it stays visible and emphasized.
    private func rateColumns(_ r: AskRates, highlight: String?) -> [StatTable.Column] {
        func col(_ label: String, _ value: String) -> StatTable.Column {
            StatTable.Column(label: label, value: value, highlighted: label == highlight)
        }
        var cols: [StatTable.Column] = []
        if let h = highlight, !Self.coreStatKeys.contains(h), let v = moreValue(r, h) {
            cols.append(StatTable.Column(label: h, value: v, highlighted: true))
        }
        cols += [
            col("AB", intVal(r.AB)),  col("H", intVal(r.H)),
            col("HR", intVal(r.HR)),  col("RBI", intVal(r.RBI)),
            col("AVG", rateVal(r.AVG)), col("OBP", rateVal(r.OBP)),
            col("SLG", rateVal(r.SLG)), col("OPS", rateVal(r.OPS)),
        ]
        return cols
    }

    /// Columns for one split row — the core eight (PA sits on the split label).
    private func splitColumns(_ s: AskSplit) -> [StatTable.Column] {
        [.init(label: "AB", value: intVal(s.AB)),
         .init(label: "H", value: intVal(s.H)), .init(label: "HR", value: intVal(s.HR)),
         .init(label: "RBI", value: intVal(s.RBI)), .init(label: "AVG", value: rateVal(s.AVG)),
         .init(label: "OBP", value: rateVal(s.OBP)), .init(label: "SLG", value: rateVal(s.SLG)),
         .init(label: "OPS", value: rateVal(s.OPS))]
    }

    private func intVal(_ n: Int?) -> String { n.map { $0.formatted(.number) } ?? "—" }
    private func rateVal(_ d: Double?) -> String { LeaderboardRow.formatted(d, as: .threeDecimal) }
    private func moreValue(_ r: AskRates, _ key: String) -> String? {
        switch key {
        case "PA":  return r.PA.map { $0.formatted(.number) }
        case "2B":  return r.doubles.map(String.init)
        case "3B":  return r.triples.map(String.init)
        case "BB":  return r.BB.map(String.init)
        case "HBP": return r.HBP.map(String.init)
        case "SF":  return r.SF.map(String.init)
        case "SO":  return r.SO.map(String.init)
        default:    return nil
        }
    }

    // MARK: - Milestone / Streak / Span (game-unit, from the daily record)

    /// A milestone game: "715th home run" + the date and opponent (no pitcher —
    /// game logs carry none).
    @ViewBuilder
    private func milestoneSection(_ m: AskMilestone) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            if let n = m.n, let ev = m.event {
                Text("\(Self.ordinal(n)) \(ev)")
                    .font(.title3.weight(.bold))
            }
            HStack(spacing: 6) {
                if let d = m.date_pretty {
                    Text(d).font(.body.weight(.semibold))
                }
                if let opp = m.opponent, !opp.isEmpty {
                    let verb = (m.home_away == "H") ? "vs" : "@"
                    Text("\(verb) \(teamAbbreviation(for: opp))")
                        .font(.body).foregroundStyle(.secondary)
                }
            }
            coverageFootnote
        }
    }

    /// A streak: "30-game hitting streak" + the date range + the batting line
    /// over the streak's games.
    @ViewBuilder
    private func streakSection(_ s: AskStreak) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            if let len = s.length, let label = s.type_label {
                Text("\(len)-game \(label)").font(.title3.weight(.bold))
            }
            if let a = s.start_pretty, let b = s.end_pretty {
                Text("\(a) – \(b)").font(.subheadline).foregroundStyle(.secondary)
            }
            // The streak LENGTH is the answer and lives in the header; the line
            // below is context (no G — that would recite the length).
            if let line = s.line { StatTable(columns: lineColumns(line)) }
            if s.restricted == true {
                Text("Over this player's seasons with complete game-by-game coverage.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            coverageFootnote
        }
    }

    /// A span: a context line naming the window + stat (NOT the value — same rule
    /// as the count/rate cards, which never recite the number above a table that
    /// already shows it), then the window dates + season-count, then the line
    /// with the span's stat HIGHLIGHTED (the value shown once, in accent).
    @ViewBuilder
    private func spanSection(_ sp: AskSpan) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            if let ev = sp.event, let w = sp.window {
                Text("Most \(ev) in any \(w) games")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(.secondary)
            }
            HStack(spacing: 6) {
                if let a = sp.start_pretty, let b = sp.end_pretty {
                    Text("\(a) – \(b)").font(.subheadline).foregroundStyle(.secondary)
                }
                if sp.cross_season == true, let ss = sp.start_season, let es = sp.end_season {
                    Text("· spanning \(es - ss + 1) seasons")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            if sp.line != nil { StatTable(columns: spanColumns(sp)) }
            coverageFootnote
        }
    }

    /// The batting line over a streak's or span's games. No G column — the game
    /// count (streak length / span window) is in the header, so showing it here
    /// would recite it. `highlight` (a span's stat, e.g. "HR") emphasizes that
    /// column — the asked-for value, shown once, in accent — like the rate cards.
    private func lineColumns(_ l: AskStatLine, highlight: String? = nil,
                             extraValue: String? = nil) -> [StatTable.Column] {
        func col(_ label: String, _ value: String) -> StatTable.Column {
            .init(label: label, value: value, highlighted: label == highlight)
        }
        let core = ["AB", "H", "HR", "RBI", "AVG", "OBP", "SLG", "OPS"]
        var cols: [StatTable.Column] = []
        // a highlighted stat that isn't a core column gets its own leading column
        if let h = highlight, !core.contains(h) {
            cols.append(.init(label: h, value: extraValue ?? "—", highlighted: true))
        }
        cols += [col("AB", intVal(l.AB)), col("H", intVal(l.H)), col("HR", intVal(l.HR)),
                 col("RBI", intVal(l.RBI)), col("AVG", rateVal(l.AVG)),
                 col("OBP", rateVal(l.OBP)), col("SLG", rateVal(l.SLG)),
                 col("OPS", rateVal(l.OPS))]
        return cols
    }

    /// Span columns with the event's stat highlighted. Core stats (HR/H/RBI)
    /// highlight in place; others (2B/3B/BB/SO from the line, or 1B/TB/HBP from
    /// the span total) get a leading highlighted column so the value still shows.
    private func spanColumns(_ sp: AskSpan) -> [StatTable.Column] {
        guard let l = sp.line else { return [] }
        let hi = spanStatLabel(sp.event)
        let extra: String? = {
            switch hi {
            case "2B": return intVal(l.doubles)
            case "3B": return intVal(l.triples)
            case "BB": return intVal(l.BB)
            case "SO": return intVal(l.SO)
            case "1B", "TB", "HBP": return sp.total.map { $0.formatted(.number) }
            default:   return nil
            }
        }()
        return lineColumns(l, highlight: hi, extraValue: extra)
    }

    /// A span's event label ("home runs") -> the stat-table column to highlight.
    private func spanStatLabel(_ event: String?) -> String? {
        switch event {
        case "home runs": return "HR"
        case "hits": return "H"
        case "RBIs": return "RBI"
        case "doubles": return "2B"
        case "triples": return "3B"
        case "walks": return "BB"
        case "strikeouts": return "SO"
        case "singles": return "1B"
        case "total bases": return "TB"
        case "times hit by a pitch": return "HBP"
        default: return nil
        }
    }

    /// "715" -> "715th" (11/12/13 take "th").
    static func ordinal(_ n: Int) -> String {
        let ones = n % 10, tens = (n / 10) % 10
        let suffix = tens == 1 ? "th"
            : ones == 1 ? "st" : ones == 2 ? "nd" : ones == 3 ? "rd" : "th"
        return "\(n)\(suffix)"
    }

    // MARK: - Splits

    /// Each split is a labeled block over the SAME stat table used for a single
    /// rate line, so "vs LHP / vs RHP" reads as two rows of one component. The
    /// label sits on its own line above the table so nine columns keep the full
    /// phone width.
    private func splitsTable(_ splits: [AskSplit]) -> some View {
        VStack(spacing: 0) {
            ForEach(splits) { split in
                VStack(alignment: .leading, spacing: 6) {
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Text(split.split_value ?? "—")
                            .font(.subheadline.weight(.semibold))
                            .lineLimit(1)
                            .minimumScaleFactor(0.8)
                        if let pa = split.PA {
                            Text("· \(pa.formatted(.number)) PA")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                    StatTable(columns: splitColumns(split))
                }
                .padding(.vertical, 8)

                if split.id != splits.last?.id { Divider() }
            }
        }
    }

    // MARK: - Sample plays

    private func sampleList(_ sample: [AskPlay]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Sample plays")
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.secondary)
            // The backend's `description` is raw Retrosheet EVENT_TX (e.g.
            // "HR/F9D+.3-H(UR);2-H(UR);…") — internal notation that's
            // meaningless to a user. We show only the readable when / who /
            // count line instead of surfacing (or half-parsing) the notation.
            ForEach(sample) { play in
                if let meta = sampleMeta(play) {
                    Text(meta)
                        .font(.subheadline)
                        .foregroundStyle(.primary)
                }
            }
        }
    }

    private func sampleMeta(_ play: AskPlay) -> String? {
        var parts: [String] = []
        if let date = play.game_date { parts.append(date) }
        if let opp = play.opponent { parts.append("vs \(opp)") }
        if let inning = play.inning { parts.append("inn \(inning)") }
        if let count = play.count { parts.append(count) }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    // MARK: - Neutral "no answer" (decline / out of scope)

    private var neutralInfoSection: some View {
        Label {
            Text(.init(response.answer ?? response.reason
                ?? "I don't have data to answer that one."))
                .font(.body)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        } icon: {
            Image(systemName: "info.circle")
                .foregroundStyle(.secondary)
        }
    }

    // MARK: - Ambiguous name

    private func ambiguousSection(_ candidates: [AskCandidate]) -> some View {
        // If the bio details don't tell the candidates apart, say so instead of
        // showing rows that look identical.
        let indistinct = Set(candidates.map { $0.detail ?? "" }).count < candidates.count
        return VStack(alignment: .leading, spacing: 10) {
            Label {
                Text(.init(response.answer ?? "There are a few players by that name — which did you mean?"))
                    .font(.body)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } icon: {
                Image(systemName: "person.2")
                    .foregroundStyle(.secondary)
            }

            VStack(spacing: 0) {
                ForEach(candidates) { candidate in
                    Button {
                        onSelectCandidate(candidate)
                    } label: {
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(candidate.name ?? "Unknown player")
                                    .font(.subheadline.weight(.medium))
                                    .foregroundStyle(.primary)
                                if let detail = candidate.detail {
                                    Text(detail)
                                        .font(.caption)
                                        .foregroundStyle(.secondary)
                                }
                            }
                            Spacer()
                            Image(systemName: "chevron.right")
                                .font(.caption2.weight(.semibold))
                                .foregroundStyle(.tertiary)
                        }
                        .padding(.vertical, 9)
                    }
                    .buttonStyle(.plain)

                    if candidate.id != candidates.last?.id { Divider() }
                }
            }

            if indistinct {
                Text("Two players share this name and we don't have enough detail to tell them apart here.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    // MARK: - Coverage footnote (subtle, informative)

    @ViewBuilder
    private var coverageFootnote: some View {
        if let note = coverageNote {
            Label {
                Text(note)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            } icon: {
                Image(systemName: "info.circle")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .padding(.top, 2)
        }
    }

    /// Combine the two possible caveats (incomplete play-by-play, missing
    /// pitch-count data) into one footnote. Skipped when coverage is clean.
    private var coverageNote: String? {
        var notes: [String] = []
        if response.game_coverage?.complete == false,
           let note = response.game_coverage?.note, !note.isEmpty {
            notes.append(note)
        }
        if let countData = response.count_data,
           countData.available == false,
           let note = countData.note, !note.isEmpty {
            notes.append(note)
        }
        return notes.isEmpty ? nil : notes.joined(separator: " ")
    }
}

// MARK: - Stat table

/// One horizontal row of stats — PA AB H HR RBI AVG OBP SLG OPS — with an
/// optional column emphasized in the accent color. Reused for a single rate
/// line, each row of a split, and situational counts (where the asked-for stat
/// pops). Columns flex to fill the width; tabular figures plus a low
/// `minimumScaleFactor` keep nine short numbers on one line at phone width.
struct StatTable: View {
    struct Column: Identifiable {
        let label: String
        let value: String
        var highlighted: Bool = false
        var id: String { label }
    }
    let columns: [Column]

    var body: some View {
        HStack(alignment: .bottom, spacing: 3) {
            ForEach(columns) { c in
                VStack(spacing: 2) {
                    Text(c.label)
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(c.highlighted ? Color.accentColor : .secondary)
                    Text(c.value)
                        .font(.system(size: 13, weight: c.highlighted ? .bold : .regular))
                        .monospacedDigit()
                        .foregroundStyle(c.highlighted ? Color.accentColor : .primary)
                        .lineLimit(1)
                        .minimumScaleFactor(0.6)
                }
                .frame(maxWidth: .infinity)
            }
        }
    }
}
