//
//  LeaderboardRow.swift
//  BaseballStats
//
//  One ranked row in the leaderboards list. Mirrors PlayerSearchResultRow's
//  chrome (60×60 circular headshot, name + team subline, trailing chevron)
//  with two leaderboard-specific additions: a leading rank number and a
//  trailing stat value.
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
        HStack(spacing: 12) {
            rankCell
            headshot

            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(entry.player.name)
                        .font(.title3.weight(.semibold))
                        .lineLimit(1)

                    if entry.player.is_hof == true {
                        hofBadge
                    }
                }

                if let teamLine {
                    Text(teamLine)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }

            Spacer(minLength: 8)

            Text(formattedValue)
                .font(.title3.weight(.semibold))
                .monospacedDigit()
                .foregroundStyle(.primary)

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
            // single/double-digit ranks.
            .frame(width: 24, alignment: .trailing)
    }

    private var headshot: some View {
        AsyncImage(url: entry.player.largeHeadshotURL) { phase in
            switch phase {
            case .success(let image):
                image.resizable().scaledToFill()
            case .empty, .failure:
                placeholderSilhouette
            @unknown default:
                placeholderSilhouette
            }
        }
        .frame(width: 60, height: 60)
        .background(Circle().fill(.ultraThinMaterial))
        .clipShape(Circle())
        .overlay(Circle().strokeBorder(.quaternary, lineWidth: 0.5))
    }

    private var placeholderSilhouette: some View {
        Image(systemName: "person.crop.circle.fill")
            .resizable()
            .scaledToFit()
            .foregroundStyle(.tertiary)
    }

    private var hofBadge: some View {
        Text("HOF")
            .font(.caption2.weight(.bold))
            .foregroundStyle(.white)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(Capsule().fill(LeaderboardRow.baseballRed.gradient))
            .accessibilityLabel("Hall of Fame")
    }

    /// Same red as the search-row HOF badge.
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
