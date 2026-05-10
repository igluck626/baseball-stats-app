//
//  LeaderboardRow.swift
//  BaseballStats
//
//  One ranked row in the leaderboards list. Mirrors the player profile
//  header's portrait headshot style (rounded rectangle, no chrome) with
//  a leading rank cell and a trailing stat value.
//

import SwiftUI

struct LeaderboardRow: View {
    let entry: LeaderboardEntry
    /// Format used for the trailing value cell. Picked by the parent based
    /// on the active stat (3-decimal AVG/OPS, 2-decimal ERA/WHIP, integer
    /// HR/RBI/SB/SO/W/SV, single-decimal WAR).
    let format: ValueFormat

    enum ValueFormat {
        case integer
        case oneDecimal
        case twoDecimal
        case threeDecimal
    }

    var body: some View {
        HStack(spacing: 10) {
            rankCell
            headshot

            // Name + team stack flexes to fill the leftover row width.
            // The name's .frame(maxWidth: .infinity) is load-bearing —
            // without it the inner HStack hugs the text's intrinsic
            // width, so SwiftUI never offers a constrained width and
            // .minimumScaleFactor never kicks in. With the explicit
            // flex, long names ("Vladimir Guerrero Jr.") shrink down
            // to 75% before any truncation happens.
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(entry.player.name)
                        .font(.title3.weight(.semibold))
                        .lineLimit(1)
                        .minimumScaleFactor(0.75)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    if entry.player.is_hof == true {
                        hofBadge
                    }
                }
                if let teamLine {
                    Text(teamLine)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .minimumScaleFactor(0.85)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)

            Text(formattedValue)
                .font(.title3.weight(.semibold))
                .monospacedDigit()
                .foregroundStyle(.primary)
                .lineLimit(1)

            Image(systemName: "chevron.right")
                .font(.footnote.weight(.semibold))
                .foregroundStyle(.tertiary)
        }
        .padding(.vertical, 6)
    }

    // MARK: - Subviews

    private var rankCell: some View {
        Text("\(entry.rank)")
            .font(.callout.weight(.semibold))
            .monospacedDigit()
            .foregroundStyle(.secondary)
            // Fixed width keeps the headshot column aligned across
            // single/double-digit ranks. Tightened from 24→22pt to
            // give the name/team stack a few more points of slack.
            .frame(width: 22, alignment: .trailing)
    }

    /// Plain portrait-rounded-rect headshot — no circular clip, no gray
    /// material backdrop. Matches the player profile header card's
    /// styling. MLB headshots ship as well-composed head-and-shoulders
    /// portraits with their own backdrop; forcing them into a circle
    /// over a frosted material was double-framing the image.
    private var headshot: some View {
        AsyncImage(url: entry.player.largeHeadshotURL) { image in
            image
                .resizable()
                .scaledToFill()
        } placeholder: {
            RoundedRectangle(cornerRadius: 6)
                .fill(Color(.secondarySystemFill))
        }
        .frame(width: 50, height: 60)
        .clipShape(RoundedRectangle(cornerRadius: 6))
    }

    /// Compact HOF capsule, identical to the one in
    /// PlayerSearchResultRow so the indicator reads the same on every
    /// list screen. Self-explanatory text — "HOF" vs an icon the user
    /// has to learn.
    private var hofBadge: some View {
        Text("HOF")
            .font(.caption2.weight(.bold))
            .foregroundStyle(.white)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(Capsule().fill(LeaderboardRow.baseballRed.gradient))
            .accessibilityLabel("Hall of Fame")
    }

    /// Same red as the search-row HOF badge, kept on a static so the
    /// shape can be referenced from `hofBadge` without re-allocating.
    private static let baseballRed = Color(red: 0.8, green: 0.1, blue: 0.1)

    // MARK: - Derived display strings

    /// Full team name from the team code, or nil when neither is known.
    private var teamLine: String? {
        guard let code = entry.player.teamCode, !code.isEmpty,
              let name = teamFullName(for: code) else { return nil }
        return name
    }

    private var formattedValue: String {
        guard let v = entry.value else { return "—" }
        switch format {
        case .integer:
            return String(Int(v.rounded()))
        case .oneDecimal:
            return String(format: "%.1f", v)
        case .twoDecimal:
            return String(format: "%.2f", v)
        case .threeDecimal:
            // ".342" — leading-zero stripped, baseball convention.
            let s = String(format: "%.3f", v)
            if s.hasPrefix("0.")  { return String(s.dropFirst()) }
            if s.hasPrefix("-0.") { return "-" + String(s.dropFirst(2)) }
            return s
        }
    }
}
