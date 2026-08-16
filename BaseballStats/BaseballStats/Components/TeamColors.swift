//
//  TeamColors.swift
//  BaseballStats
//
//  One color per team — drives the team tint on the player profile header,
//  the news chips, the postseason bracket and the score swatches. One entry
//  per franchise, plus aliases for the Lahman / Baseball Reference /
//  historical code dialects the backend's `team_code` field may use.
//

import SwiftUI
import UIKit   // UIColor HSB manipulation for the dark-mode brightening helpers

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

    /// The club's color as used on a small solid mark — a bracket chip or a
    /// score swatch — brightened in dark mode so a dark navy stays visible
    /// against a dark surface, and neutral grey for a code we can't resolve.
    ///
    /// This is the single definition. `PostseasonBracketView` carried three
    /// byte-identical private copies of it, one per view; three copies of a
    /// rule is how the rule drifts.
    static func chip(for teamCode: String?, dark: Bool) -> Color {
        guard let base = color(for: teamCode) else { return Color(.systemGray3) }
        return dark ? base.brightenedForDarkText() : base
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

// MARK: - Dark-mode team-color brightening
//
// Team brand colors are tuned for white backgrounds; many (A's forest
// green, navy clubs) go nearly invisible on a dark surface. These lift
// brightness for dark mode. Call ONLY in dark mode — light mode should use
// the raw color. Counterparts to SearchView's `darkened` HSB helper.
extension Color {
    /// For dark-mode WASHES: boost every color's brightness so it registers
    /// against a dark card, ceiling-capped so already-bright colors (Rockies
    /// purple, Giants orange) don't blow out. A slight saturation cap keeps
    /// lifted colors from going neon.
    func brightenedForDark(
        boost: CGFloat = 0.22, ceiling: CGFloat = 0.92, satCap: CGFloat = 0.85,
    ) -> Color {
        var h: CGFloat = 0, s: CGFloat = 0, b: CGFloat = 0, a: CGFloat = 0
        UIColor(self).getHue(&h, saturation: &s, brightness: &b, alpha: &a)
        return Color(
            hue: Double(h),
            saturation: Double(min(s, satCap)),
            brightness: Double(min(b + boost, ceiling)),
            opacity: Double(a),
        )
    }

    /// For dark-mode team-colored TEXT / accents: raise brightness to a high
    /// floor so the color reads comfortably (text needs more contrast than a
    /// wash). Already-bright colors above the floor are untouched.
    func brightenedForDarkText(
        minBrightness: CGFloat = 0.82, satCap: CGFloat = 0.80,
    ) -> Color {
        var h: CGFloat = 0, s: CGFloat = 0, b: CGFloat = 0, a: CGFloat = 0
        UIColor(self).getHue(&h, saturation: &s, brightness: &b, alpha: &a)
        return Color(
            hue: Double(h),
            saturation: Double(min(s, satCap)),
            brightness: Double(max(b, minBrightness)),
            opacity: Double(a),
        )
    }
}
