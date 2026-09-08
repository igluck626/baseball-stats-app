//
//  PlayDetailSheet.swift
//  BaseballStats
//
//  The detail behind one row of the play-by-play list. The list row
//  itself is a single sentence; everything the old row carried inline
//  — the batted-ball metrics and the pitch list — lives here, plus the
//  sentence in full, since a row clips at two lines.
//
//  Reached only from an at-bat. Announcement rows (relief changes,
//  defensive changes, pinch-hit notices) have no batter, no pitches and
//  no batted ball, so they carry no tap target and never open this.
//

import SwiftUI

/// The payload for one play's detail sheet, built by `PlaysView` so
/// that view's grouping types can stay private.
struct PlayDetail: Identifiable, Hashable {
    let id: String
    /// The outcome as a noun — "Home Run", "Strikeout", "Sacrifice Fly".
    let title: String
    /// BDL's full sentence, including the runner-advance tail that the
    /// row may have clipped.
    let sentence: String
    /// Batted-ball metrics, or nil for a plate appearance that put no
    /// ball in play. nil means the block is ABSENT, not empty — a
    /// strikeout shows no metrics rather than three dashes.
    let contact: BDLPitchDetail?
    /// Every pitch of the plate appearance, in order.
    let pitches: [BDLPlay]
}

struct PlayDetailSheet: View {
    let detail: PlayDetail
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    header
                    if let c = detail.contact, !Self.metrics(c).isEmpty {
                        metricsCard(Self.metrics(c))
                    }
                    if !detail.pitches.isEmpty { pitchList }
                }
                .padding(18)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { dismiss() }
                }
            }
        }
        .presentationDetents([.medium, .large])
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(detail.title)
                .font(.title2.weight(.bold))
                .fixedSize(horizontal: false, vertical: true)
            Text(detail.sentence)
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func metricsCard(_ rows: [(String, String)]) -> some View {
        VStack(spacing: 10) {
            ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                HStack(alignment: .firstTextBaseline) {
                    Text(row.0)
                        .foregroundStyle(.secondary)
                    Spacer(minLength: 12)
                    Text(row.1)
                        .fontWeight(.semibold)
                        .monospacedDigit()
                }
                .font(.subheadline)
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity)
        .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 12))
    }

    private var pitchList: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("PITCHES")
                .font(.caption.weight(.bold))
                .tracking(0.8)
                .foregroundStyle(.secondary)
            ForEach(Array(detail.pitches.enumerated()), id: \.offset) { i, p in
                // One row per pitch: what it was called, and what it
                // was thrown at. The type and speed are joined into a
                // SINGLE Text with a space rather than an HStack of
                // two, for the same Dynamic Type reason the metric
                // rows elsewhere in the app are written that way.
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Text("\(i + 1)")
                        .font(.footnote.weight(.semibold))
                        .foregroundStyle(.tertiary)
                        .monospacedDigit()
                        .frame(width: 18, alignment: .trailing)
                    Text(Self.call(p))
                        .font(.subheadline)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 8)
                    if let pitch = Self.pitchDescription(p) {
                        Text(pitch)
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                            .multilineTextAlignment(.trailing)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        }
    }

    /// Only the metrics that are actually present. A ball in play can
    /// carry an exit velocity and no distance, and a row of "—" says
    /// nothing a missing row doesn't.
    private static func metrics(_ c: BDLPitchDetail) -> [(String, String)] {
        var rows: [(String, String)] = []
        if let v = c.exitVelocity { rows.append(("Exit Velocity", String(format: "%.1f mph", v))) }
        if let d = c.hitDistance  { rows.append(("Distance", "\(Int(d.rounded())) ft")) }
        if let a = c.launchAngle  { rows.append(("Launch Angle", "\(Int(a.rounded()))°")) }
        if let x = c.expectedBattingAverage {
            rows.append(("Expected AVG", String(format: "%.3f", x).replacingOccurrences(
                of: "0.", with: ".", options: .anchored,
            )))
        }
        if c.isBarrel ?? false { rows.append(("Barrel", "Yes")) }
        return rows
    }

    /// BDL's row type read as the call a broadcast would use.
    /// Prefix matching because BDL suffixes review outcomes onto the
    /// same types — "Ball - Confirmed", "Strike Looking - Overturned".
    static func call(_ p: BDLPlay) -> String {
        switch p.type ?? "" {
        case let t where t.hasPrefix("Strike Swinging"): return "Swinging Strike"
        case let t where t.hasPrefix("Strike Looking"):  return "Called Strike"
        case "Bunted Foul":                              return "Foul Bunt"
        case let t where t.hasPrefix("Foul"):            return "Foul"
        case let t where t.hasPrefix("Automatic Ball"):  return "Intentional Ball"
        case let t where t.hasPrefix("Ball"):            return "Ball"
        case "Hit By Pitch":                             return "Hit By Pitch"
        default:                                         return "In Play"
        }
    }

    /// "Sweeper 82 mph", or just one half when the other is missing —
    /// an intentional walk's automatic balls carry neither.
    static func pitchDescription(_ p: BDLPlay) -> String? {
        let parts = [p.pitchType, p.pitchVelocity.map { "\(Int($0.rounded())) mph" }]
            .compactMap { $0 }
            .filter { !$0.isEmpty }
        return parts.isEmpty ? nil : parts.joined(separator: " ")
    }
}
