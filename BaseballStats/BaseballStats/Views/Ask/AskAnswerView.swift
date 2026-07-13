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
            } else {
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
        !(response.leaders?.isEmpty ?? true)
            || response.rates != nil
            || !(response.splits?.isEmpty ?? true)
            || !(response.sample?.isEmpty ?? true)
    }

    // MARK: - Normal answer

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

    /// Whichever structured payload the answer carries, stacked. In practice
    /// only one is populated per answer, but this is defensive.
    @ViewBuilder
    private var detailContent: some View {
        VStack(alignment: .leading, spacing: 14) {
            if let leaders = response.leaders, !leaders.isEmpty {
                leadersTable(leaders)
            }
            if let rates = response.rates {
                ratesLine(rates)
            }
            if let splits = response.splits, !splits.isEmpty {
                splitsTable(splits)
            }
            if let sample = response.sample, !sample.isEmpty {
                sampleList(sample)
            }
        }
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
        guard let pa = rates.PA else { return nil }
        if let h = rates.H, let ab = rates.AB {
            return "\(h)-for-\(ab) · \(pa) PA"
        }
        return "\(pa) PA"
    }

    // MARK: - Splits

    private func splitsTable(_ splits: [AskSplit]) -> some View {
        VStack(spacing: 0) {
            HStack {
                Text("SPLIT").frame(maxWidth: .infinity, alignment: .leading)
                Text("AVG").frame(width: 52, alignment: .trailing)
                Text("OBP").frame(width: 52, alignment: .trailing)
                Text("SLG").frame(width: 52, alignment: .trailing)
            }
            .font(.caption2.weight(.semibold))
            .foregroundStyle(.secondary)
            .padding(.bottom, 6)

            ForEach(splits) { split in
                HStack {
                    Text(split.split_value ?? "—")
                        .font(.subheadline.weight(.medium))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .lineLimit(1)
                    Text(LeaderboardRow.formatted(split.AVG, as: .threeDecimal))
                        .frame(width: 52, alignment: .trailing)
                    Text(LeaderboardRow.formatted(split.OBP, as: .threeDecimal))
                        .frame(width: 52, alignment: .trailing)
                    Text(LeaderboardRow.formatted(split.SLG, as: .threeDecimal))
                        .frame(width: 52, alignment: .trailing)
                }
                .font(.subheadline)
                .monospacedDigit()
                .padding(.vertical, 6)

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
