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
            } else {
                // A single count or rate: the phrased sentence leads, the
                // detail (sample plays / full stat line) sits behind a
                // disclosure.
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
        response.rates != nil || !(response.sample?.isEmpty ?? true)
    }

    // MARK: - Single answer (count / rate): prose + disclosure

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
            }

            if hasDetails {
                DisclosureGroup {
                    detailContent
                        .padding(.top, 6)
                } label: {
                    Text("Show details")
                        .font(.subheadline.weight(.semibold))
                }
                .tint(.secondary)
            }

            coverageFootnote
        }
    }

    /// Detail for a single answer: the full stat line and/or sample plays.
    /// (Leaders and splits are data-first and never routed through here.)
    @ViewBuilder
    private var detailContent: some View {
        VStack(alignment: .leading, spacing: 14) {
            if let rates = response.rates {
                ratesLine(rates)
            }
            if let sample = response.sample, !sample.isEmpty {
                sampleList(sample)
            }
        }
    }

    // MARK: - Data-first sections

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
                        Text(leader.player_name ?? "—")
                            .font(.subheadline.weight(.medium))
                            .foregroundStyle(.primary)
                            .lineLimit(1)
                            .minimumScaleFactor(0.8)
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

    /// A count board shows the count; a rate board shows the qualifying stat.
    private func leaderValue(_ leader: AskLeader) -> String {
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

    // MARK: - Rates

    private func ratesLine(_ rates: AskRates) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 18) {
                rateCell("AVG", rates.AVG)
                rateCell("OBP", rates.OBP)
                rateCell("SLG", rates.SLG)
                rateCell("OPS", rates.OPS)
            }
            if let volume = rateVolume(rates) {
                Text(volume)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func rateCell(_ label: String, _ value: Double?) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label)
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.secondary)
            Text(LeaderboardRow.formatted(value, as: .threeDecimal))
                .font(.callout.weight(.semibold))
                .monospacedDigit()
        }
    }

    private func rateVolume(_ rates: AskRates) -> String? {
        countsLine(pa: rates.PA, ab: rates.AB, h: rates.H, hr: rates.HR, rbi: rates.RBI)
    }

    // MARK: - Splits

    /// Two rows per split rather than a 10-column table. The requested columns
    /// (PA AB H HR RBI AVG OBP SLG OPS) can't fit a phone width without a
    /// horizontal scroll that hides half of them, and comparing splits across a
    /// scroll is awkward. So each split is a compact block: the slash line +
    /// OPS on top (what you compare across splits), the counting volume beneath
    /// (context). Everything stays on screen and each split reads as a unit —
    /// the same "rate prominent, volume secondary" idiom used elsewhere.
    private func splitsTable(_ splits: [AskSplit]) -> some View {
        VStack(spacing: 0) {
            ForEach(splits) { split in
                VStack(alignment: .leading, spacing: 4) {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(split.split_value ?? "—")
                            .font(.subheadline.weight(.semibold))
                            .lineLimit(1)
                            .minimumScaleFactor(0.8)
                        Spacer(minLength: 8)
                        Text(slashLine(avg: split.AVG, obp: split.OBP, slg: split.SLG))
                            .font(.subheadline.weight(.semibold))
                            .monospacedDigit()
                        Text("\(LeaderboardRow.formatted(split.OPS, as: .threeDecimal)) OPS")
                            .font(.caption.weight(.semibold))
                            .monospacedDigit()
                            .foregroundStyle(.secondary)
                    }
                    if let counts = countsLine(pa: split.PA, ab: split.AB,
                                               h: split.H, hr: split.HR, rbi: split.RBI) {
                        Text(counts)
                            .font(.caption)
                            .monospacedDigit()
                            .foregroundStyle(.secondary)
                    }
                }
                .padding(.vertical, 8)

                if split.id != splits.last?.id { Divider() }
            }
        }
    }

    /// "AVG/OBP/SLG" as a three-decimal slash line.
    private func slashLine(avg: Double?, obp: Double?, slg: Double?) -> String {
        [avg, obp, slg]
            .map { LeaderboardRow.formatted($0, as: .threeDecimal) }
            .joined(separator: "/")
    }

    /// "1,238 PA · 337 AB · 97 H · 24 HR · 60 RBI" — grouped counts, nils
    /// skipped. Shared by the split blocks and the single-rate volume line.
    private func countsLine(pa: Int?, ab: Int?, h: Int?, hr: Int?, rbi: Int?) -> String? {
        var parts: [String] = []
        if let pa { parts.append("\(pa.formatted(.number)) PA") }
        if let ab { parts.append("\(ab.formatted(.number)) AB") }
        if let h { parts.append("\(h.formatted(.number)) H") }
        if let hr { parts.append("\(hr.formatted(.number)) HR") }
        if let rbi { parts.append("\(rbi.formatted(.number)) RBI") }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
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
        VStack(alignment: .leading, spacing: 10) {
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
                            Text(candidate.name ?? "Unknown player")
                                .font(.subheadline.weight(.medium))
                                .foregroundStyle(.primary)
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
