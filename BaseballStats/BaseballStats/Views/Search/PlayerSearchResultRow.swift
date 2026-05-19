//
//  PlayerSearchResultRow.swift
//  BaseballStats
//
//  One row in the search results list. The row itself is non-interactive
//  — the parent view attaches the tap action so navigation can be added
//  later without changing this file.
//

import SwiftUI

struct PlayerSearchResultRow: View {
    let player: PlayerSearchResult

    var body: some View {
        HStack(spacing: 14) {
            headshot

            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(player.name)
                        .font(.title3.weight(.semibold))
                        .lineLimit(1)

                    if player.is_hof == true {
                        hofBadge
                    }
                }

                if let positionLine {
                    Text(positionLine)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }

                if let careerYears {
                    Text(careerYears)
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                }
            }

            Spacer(minLength: 8)

            Image(systemName: "chevron.right")
                .font(.footnote.weight(.semibold))
                .foregroundStyle(.tertiary)
        }
        .padding(.vertical, 6)
    }

    // MARK: - Subviews

    private var headshot: some View {
        AsyncImage(url: player.largeHeadshotURL) { phase in
            switch phase {
            case .success(let image):
                image
                    .resizable()
                    .scaledToFill()
            case .empty, .failure:
                placeholderSilhouette
            @unknown default:
                placeholderSilhouette
            }
        }
        .frame(width: 60, height: 60)
        .background(Circle().fill(.ultraThinMaterial))
        .clipShape(Circle())
        .overlay(
            Circle().strokeBorder(.quaternary, lineWidth: 0.5)
        )
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
            .background(
                Capsule().fill(PlayerSearchResultRow.baseballRed.gradient)
            )
            .accessibilityLabel("Hall of Fame")
    }

    /// Saturated red used for the HOF badge — meant to evoke baseball-stitch
    /// red rather than the brighter system red.
    private static let baseballRed = Color(red: 0.8, green: 0.1, blue: 0.1)

    // MARK: - Derived display strings

    /// "OF · Los Angeles Angels" when both are present. Falls back to
    /// whichever single piece is available, or nil if neither is. We use
    /// `teamCode` (server-normalized) rather than `currentTeam` (raw) so
    /// the lookup is reliable regardless of which loader wrote the row.
    /// Resolution lives in Components/TeamNames.swift and is shared
    /// with the player profile views.
    private var positionLine: String? {
        let pos = player.position?.nonEmpty
        let team = player.teamCode?.nonEmpty.flatMap(teamFullName(for:))
        switch (pos, team) {
        case let (p?, t?): return "\(p) · \(t)"
        case let (p?, nil): return p
        case let (nil, t?): return t
        default: return nil
        }
    }

    /// Years active. "2010–2024" if both sides known; "Debut 2010" if the
    /// player is still active and we don't have a final season yet.
    private var careerYears: String? {
        switch (player.mlb_debut, player.mlb_last_season) {
        case let (debut?, last?):
            return debut == last ? "\(debut)" : "\(debut)–\(last)"
        case let (debut?, nil):
            return "Debut \(debut)"
        default:
            return nil
        }
    }
}

private extension String {
    /// Returns nil when the string is empty; otherwise self. Useful for
    /// turning empty strings from the API into proper nils so SwiftUI
    /// can hide the placeholder line.
    var nonEmpty: String? { isEmpty ? nil : self }
}

#Preview {
    List {
        PlayerSearchResultRow(player: .init(
            player_id: 592450,
            name: "Aaron Judge",
            bbref_id: "judgeaa01",
            mlb_debut: 2016,
            mlb_last_season: 2025,
            currentTeam: "New York",
            teamCode: "NYA",
            position: "RF",
            bats: "R",
            throwingArm: "R",
            height: 79,
            weight: 282,
            birth_year: 1992, birth_month: 4, birth_day: 26,
            birth_city: "Linden", birth_state: "CA", birth_country: "USA",
            debut: "2016-08-13",
            final_game: nil,
            birthdate: "1992-04-26",
            headshot_url: nil,
            is_hof: false,
            hof_year: nil,
            is_pitcher: nil,
            bdl_id: nil
        ))
        PlayerSearchResultRow(player: .init(
            player_id: 110849,
            name: "Babe Ruth",
            bbref_id: "ruthba01",
            mlb_debut: 1914,
            mlb_last_season: 1935,
            currentTeam: "NYA",
            teamCode: "NYA",
            position: "RF",
            bats: "L",
            throwingArm: "L",
            height: 74,
            weight: 215,
            birth_year: 1895, birth_month: 2, birth_day: 6,
            birth_city: "Baltimore", birth_state: "MD", birth_country: "USA",
            debut: "1914-07-11",
            final_game: "1935-05-30",
            birthdate: "1895-02-06",
            headshot_url: nil,
            is_hof: true,
            hof_year: 1936,
            is_pitcher: nil,
            bdl_id: nil
        ))
    }
    .listStyle(.insetGrouped)
}
