//
//  TeamColors.swift
//  BaseballStats
//
//  Primary brand color per team — drives the team-tint backdrop on the
//  player profile header card. One entry per franchise, plus aliases
//  for the Lahman / Baseball Reference / historical code dialects the
//  backend's `team_code` field may use.
//
//  Source: each franchise's official primary color. Values are the
//  canonical hex codes published in MLB's brand guidelines (or
//  team.com style guides when an org's primary is the darker of two
//  brand colors).
//

import SwiftUI

enum TeamColors {
    /// Returns the primary brand color for a given team code, or nil
    /// for codes that don't map to one of the 30 franchises. The
    /// lookup is case-insensitive and tolerates the three dialects in
    /// play across the backend: Lahman ("LAN", "NYA", …), MLB Stats
    /// API / Baseball Reference ("LAD", "NYY", …), and historical
    /// codes ("FLO", "ANA", "MON").
    static func color(for teamCode: String?) -> Color? {
        guard let teamCode, !teamCode.isEmpty else { return nil }
        return hexByCode[teamCode.uppercased()].flatMap(Color.init(hex:))
    }

    /// Most-distinctive brand color per franchise, plus aliases. We
    /// pick the team's defining color rather than always reaching for
    /// their primary dark — half the league shares some shade of navy,
    /// and a uniform navy backdrop would make most player pages look
    /// identical. Where a franchise's secondary (gold for the Brewers
    /// and Pirates, orange for the Mets, powder blue for the Royals,
    /// red for the Cubs) is the iconic one, that's the choice.
    /// Aliases resolve to the same hex as their canonical 3-letter
    /// form so a franchise rebrand only needs an update in one place.
    private static let hexByCode: [String: String] = [
        // Canonical (MLB Stats API / Baseball Reference codes).
        //
        // Many teams' actual brand reds are punchy at full saturation
        // — fine on a jersey, garish as a background wash — so the
        // red clubs all share a deep crimson (#8B0000) that reads as
        // "this team's color" without screaming. Same logic for the
        // bright-gold and bright-orange clubs, which get muted
        // brown/dark-green variants.
        "ARI": "#E3D4AD",   // Sedona Sand — the desert tan, not the cardinal red
        "ATL": "#8B0000",   // Muted dark red — full Atlanta red was too punchy
        "BAL": "#DF4601",
        "BOS": "#8B0000",   // Muted dark red
        "CHC": "#CC3433",   // Cubs red, not the navy
        "CWS": "#27251F",
        "CIN": "#8B0000",   // Muted dark red
        "CLE": "#E31937",   // Guardians red, not the navy
        "COL": "#8B4FBE",   // Rockies purple (lighter than #33006F for readability)
        "DET": "#0C2340",
        "HOU": "#8B3A00",   // Dark orange/brown — full Astros orange was too bright
        "KCR": "#74B4FA",   // Royals powder blue, not the navy
        "LAA": "#8B0000",   // Muted dark red
        "LAD": "#005A9C",
        "MIA": "#00A3E0",
        "MIL": "#1A4A1A",   // Dark green — Brewers gold was too bright as a bg
        "MIN": "#D31145",   // Twins red, not the navy
        "NYM": "#FF5910",   // Mets orange, not the navy
        "NYY": "#003087",
        "OAK": "#003831",
        "PHI": "#8B0000",   // Muted dark red
        "PIT": "#4A3728",   // Dark brown — Pirates gold was too bright as a bg
        "SDP": "#4A3000",   // Dark gold/brown — Padres yellow was too bright
        "SFG": "#FD5A1E",
        "SEA": "#005C5C",   // Mariners teal (Northwest Green), not the navy
        "STL": "#8B0000",   // Muted dark red
        "TBR": "#8FBCE6",   // Rays light blue, not the navy
        "TEX": "#003278",
        "TOR": "#E8291C",   // Blue Jays red — distinctive vs. the half-dozen blue clubs
        "WSN": "#8B0000",   // Muted dark red

        // Lahman dialect aliases
        "LAN": "#005A9C",   // Dodgers
        "NYA": "#003087",   // Yankees
        "NYN": "#FF5910",   // Mets
        "CHA": "#27251F",   // White Sox
        "CHN": "#CC3433",   // Cubs
        "KCA": "#74B4FA",   // Royals
        "SDN": "#4A3000",   // Padres
        "SFN": "#FD5A1E",   // Giants
        "SLN": "#8B0000",   // Cardinals
        "TBA": "#8FBCE6",   // Rays
        "WAS": "#8B0000",   // Nationals

        // Historical / legacy aliases
        "ANA": "#8B0000",   // Angels (1997–2004 branding)
        "FLO": "#00A3E0",   // Marlins, pre-2012 rename
        "MON": "#8B0000",   // Expos → Nationals lineage
        "ATH": "#003831",   // Athletics shorthand
    ]
}

// MARK: - Hex-string Color init

extension Color {
    /// Build a Color from a `#RRGGBB` or `#RRGGBBAA` hex string. Returns
    /// nil for malformed input. Case-insensitive; leading "#" optional.
    /// Centralized here because no other surface in the app needs hex
    /// parsing yet — keeping it next to TeamColors avoids leaking a
    /// generic extension into module-wide scope.
    ///
    /// Marked nonisolated so it stays callable from `TeamColors.color`
    /// (a pure-data static func). The project's
    /// `-default-isolation=MainActor` would otherwise pin every
    /// extension method to the main actor.
    nonisolated init?(hex: String) {
        var s = hex.trimmingCharacters(in: .whitespaces)
        if s.hasPrefix("#") { s.removeFirst() }
        guard s.count == 6 || s.count == 8 else { return nil }
        var value: UInt64 = 0
        guard Scanner(string: s).scanHexInt64(&value) else { return nil }
        let r, g, b, a: Double
        if s.count == 6 {
            r = Double((value >> 16) & 0xFF) / 255.0
            g = Double((value >>  8) & 0xFF) / 255.0
            b = Double( value        & 0xFF) / 255.0
            a = 1.0
        } else {
            r = Double((value >> 24) & 0xFF) / 255.0
            g = Double((value >> 16) & 0xFF) / 255.0
            b = Double((value >>  8) & 0xFF) / 255.0
            a = Double( value        & 0xFF) / 255.0
        }
        self.init(.sRGB, red: r, green: g, blue: b, opacity: a)
    }
}
