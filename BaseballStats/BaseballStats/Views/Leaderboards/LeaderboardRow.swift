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
    /// Format used for the trailing value cell. Picked by the parent
    /// based on the active stat. Two flavors of one-decimal exist
    /// because WAR caps under 200 (no separator needed) while career
    /// IP runs into five figures and reads better with the locale's
    /// thousands separator.
    let format: ValueFormat

    enum ValueFormat {
        /// Counting stats — HR / H / R / RBI / SB / BB / SO / W / SV /
        /// AB / PA / 2B / 3B. Uses `.formatted(.number)` so values
        /// above 999 pick up the locale's grouping separator
        /// (e.g. "4,256" for Pete Rose's hits).
        case integer
        /// WAR — one decimal, no grouping. Career WAR max is ~165;
        /// season WAR under 13.
        case oneDecimal
        /// IP — one decimal *with* grouping so career IP renders as
        /// "7,356.0" rather than "7356.0".
        case oneDecimalGrouped
        /// ERA / WHIP / FIP — two decimals, no grouping (range 0–10).
        case twoDecimal
        /// AVG / OBP / SLG / OPS — three decimals, leading-zero
        /// stripped per Baseball Reference convention.
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

    /// Compact team code + (in all-time mode) the season year. Format:
    /// "LAA · 2004" for an all-time HR row, plain "LAA" for season /
    /// career rows. Uses the 3-letter abbreviation rather than the
    /// full name ("Los Angeles Angels") so the year can't truncate in
    /// the narrow leaderboard row — full names appear on the player
    /// profile after tap-through. Year alone (no team) is the fallback
    /// for the rare case where the team can't be resolved.
    private var teamLine: String? {
        let teamCode: String? = {
            guard let code = entry.player.teamCode, !code.isEmpty else { return nil }
            return teamAbbreviation(for: code)
        }()
        let yearText = entry.year.map(String.init)
        switch (teamCode, yearText) {
        case let (team?, year?): return "\(team) · \(year)"
        case let (team?, nil):   return team
        case let (nil, year?):   return year
        default:                 return nil
        }
    }

    private var formattedValue: String {
        guard let v = entry.value else { return "—" }
        switch format {
        case .integer:
            // .formatted(.number) picks up the locale's thousands
            // separator above 999 — "1,234" instead of "1234".
            // Round-trip through Int first so we don't accidentally
            // emit "762.0" for an integer-typed Double from the API.
            return Int(v.rounded()).formatted(.number)
        case .oneDecimal:
            return String(format: "%.1f", v)
        case .oneDecimalGrouped:
            return v.formatted(.number.precision(.fractionLength(1)))
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
