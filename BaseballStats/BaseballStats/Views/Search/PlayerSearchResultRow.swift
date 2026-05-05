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
        AsyncImage(url: URL(string: player.headshot_url ?? "")) { phase in
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
    private var positionLine: String? {
        let pos = player.position?.nonEmpty
        let team = player.teamCode?.nonEmpty.flatMap(Self.teamFullName(for:))
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

    // MARK: - Team-code lookup

    /// Maps the team codes the backend can return to full display names.
    /// The DB stores three families of codes depending on which loader
    /// wrote the row:
    ///   • Lahman teamID — "NYA", "BOS", "SLN", "CHA"
    ///   • Baseball-Reference Tm — "NYY", "BOS", "STL", "CHW"
    ///   • _TEAM_DISPLAY city-only — "New York", "Boston", "St. Louis"
    /// City-only entries can't be disambiguated to a single franchise (NYY
    /// vs NYM both → "New York"), so we leave those untouched and accept
    /// the city-only display.
    private static let teamCodeToFullName: [String: String] = [
        // Lahman codes (modern era)
        "ARI": "Arizona Diamondbacks",
        "ATL": "Atlanta Braves",
        "BAL": "Baltimore Orioles",
        "BOS": "Boston Red Sox",
        "CHA": "Chicago White Sox",
        "CHN": "Chicago Cubs",
        "CIN": "Cincinnati Reds",
        "CLE": "Cleveland Guardians",
        "COL": "Colorado Rockies",
        "DET": "Detroit Tigers",
        "HOU": "Houston Astros",
        "KCA": "Kansas City Royals",
        "LAA": "Los Angeles Angels",
        "ANA": "Los Angeles Angels",
        "LAN": "Los Angeles Dodgers",
        "MIA": "Miami Marlins",
        "FLO": "Miami Marlins",
        "MIL": "Milwaukee Brewers",
        "MIN": "Minnesota Twins",
        "NYA": "New York Yankees",
        "NYN": "New York Mets",
        "OAK": "Oakland Athletics",
        "PHI": "Philadelphia Phillies",
        "PIT": "Pittsburgh Pirates",
        "SDN": "San Diego Padres",
        "SEA": "Seattle Mariners",
        "SFN": "San Francisco Giants",
        "SLN": "St. Louis Cardinals",
        "TBA": "Tampa Bay Rays",
        "TEX": "Texas Rangers",
        "TOR": "Toronto Blue Jays",
        "WAS": "Washington Nationals",
        "MON": "Montreal Expos",
        "BRO": "Brooklyn Dodgers",
        "WS1": "Washington Senators",

        // Baseball-Reference codes (overlaps Lahman where letters match)
        "NYY": "New York Yankees",
        "NYM": "New York Mets",
        "CHW": "Chicago White Sox",
        "CHC": "Chicago Cubs",
        "KCR": "Kansas City Royals",
        "LAD": "Los Angeles Dodgers",
        "SDP": "San Diego Padres",
        "SFG": "San Francisco Giants",
        "STL": "St. Louis Cardinals",
        "TBR": "Tampa Bay Rays",
        "WSN": "Washington Nationals",
    ]

    /// Returns the full team name for a code, or nil when the code isn't
    /// recognized and looks like a code (e.g. obscure 19th-century Lahman
    /// codes like "LS3"). Strings that don't look like codes pass through
    /// unchanged so callers can still display unrecognized city values.
    static func teamFullName(for code: String) -> String? {
        if let resolved = teamCodeToFullName[code] {
            return resolved
        }
        // Looks like a code — uppercase letters/digits, ≤3 chars — but we
        // don't recognize it. Hide rather than show a meaningless token.
        if code.count <= 3,
           code.allSatisfy({ $0.isUppercase || $0.isNumber }) {
            return nil
        }
        // Doesn't look like a code — pass through (e.g. "New York" from a
        // row stored via _TEAM_DISPLAY before team_code was added).
        return code
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
            hof_year: nil
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
            hof_year: 1936
        ))
    }
    .listStyle(.insetGrouped)
}
