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

// MARK: - Current-batter marker
//
// The live box score marks the batter at the plate with a small glyph in the
// team's colour, on a row washed with the same colour. Brand colours are tuned
// for one surface: measured against the row the marker actually sits on, 27 of
// the 30 fell below 3:1 in one mode or the other — the navies, dark greens, the
// shared crimson and the White Sox's near-black in dark mode (DET 1.03:1), the
// pale blues, Sedona sand and the oranges in light mode (ARI 1.38:1).
//
// The rule keeps the team's own hue and moves only as far as it must: a colour
// that already reaches 3:1 (WCAG 2's minimum for graphics) against its row AS
// DRAWN — the wash rounded to whole channels — with a 0.05 cushion is used as it
// is; one that doesn't is darkened in light mode, or brightened — and then, if a
// saturated blue still can't reach it at full brightness, eased toward white —
// in dark mode, a step at a time, stopping at the first that does. Only a colour
// no step can rescue falls back to the system accent; none of the 30 does.
//
// The cushion is measured, not taste: aiming at exactly 3.00 against the
// unrounded row, the White Sox's dark-mode marker rendered at 2.99:1 — the screen
// draws the row's wash in whole channels. One level of the row moves the ratio by
// about 0.03, so 3.05 holds through a level either way.
extension TeamColors {
    /// An sRGB colour on 0...255 channels: what the contrast arithmetic and the
    /// screen both see.
    struct RGB: Equatable {
        var r: Double, g: Double, b: Double

        init(r: Double, g: Double, b: Double) { (self.r, self.g, self.b) = (r, g, b) }

        init?(hex: String) {
            var s = hex.trimmingCharacters(in: .whitespaces)
            if s.hasPrefix("#") { s.removeFirst() }
            guard s.count == 6, let v = UInt32(s, radix: 16) else { return nil }
            self.init(r: Double(v >> 16 & 0xFF), g: Double(v >> 8 & 0xFF), b: Double(v & 0xFF))
        }

        var color: Color { Color(.sRGB, red: r / 255, green: g / 255, blue: b / 255) }

        /// To whole channels, as the screen draws it.
        var rounded: RGB { RGB(r: r.rounded(), g: g.rounded(), b: b.rounded()) }

        /// WCAG 2 relative luminance.
        var luminance: Double {
            func lin(_ c: Double) -> Double {
                let c = c / 255
                return c <= 0.04045 ? c / 12.92 : pow((c + 0.055) / 1.055, 2.4)
            }
            return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
        }

        /// This colour at `opacity` over `base`.
        func over(_ base: RGB, opacity: Double) -> RGB {
            RGB(r: opacity * r + (1 - opacity) * base.r,
                g: opacity * g + (1 - opacity) * base.g,
                b: opacity * b + (1 - opacity) * base.b)
        }

        var hsv: (h: Double, s: Double, v: Double) {
            let (r, g, b) = (r / 255, g / 255, b / 255)
            let mx = max(r, g, b), mn = min(r, g, b), d = mx - mn
            var h = 0.0
            if d > 0 {
                if mx == r { h = (g - b) / d } else if mx == g { h = 2 + (b - r) / d } else { h = 4 + (r - g) / d }
                h /= 6
                if h < 0 { h += 1 }
            }
            return (h, mx == 0 ? 0 : d / mx, mx)
        }

        /// From hue, saturation and value on 0...1, rounded to whole channels.
        init(h: Double, s: Double, v: Double) {
            let i = Int(h * 6) % 6, f = h * 6 - Double(Int(h * 6))
            let p = v * (1 - s), q = v * (1 - s * f), t = v * (1 - s * (1 - f))
            let (r, g, b): (Double, Double, Double) = [
                (v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q),
            ][i]
            self.init(r: (r * 255).rounded(), g: (g * 255).rounded(), b: (b * 255).rounded())
        }
    }

    /// WCAG 2 contrast ratio, 1...21.
    static func contrast(_ a: RGB, _ b: RGB) -> Double {
        let (hi, lo) = (max(a.luminance, b.luminance), min(a.luminance, b.luminance))
        return (hi + 0.05) / (lo + 0.05)
    }

    static let markerMinimumContrast = 3.0
    /// What the rule aims for: the minimum plus the cushion described above.
    static let markerTarget = 3.05
    /// How much of the marker's colour washes its row (the box score's batting row).
    static let markerWash = 0.15
    /// The box score's glass team card as rendered — sampled from the 2026-10-10
    /// simulator screenshots, where a 15% wash over these values reproduced the
    /// marked rows' pixels exactly (CIN light 237,216,216; CHW dark 33,33,32).
    static let markerCard = (light: RGB(r: 254, g: 254, b: 254), dark: RGB(r: 32, g: 32, b: 32))

    /// The row behind a marker of colour `c`, as drawn: `c` washed over the card,
    /// in whole channels.
    static func markerRow(_ c: RGB, dark: Bool) -> RGB {
        c.over(dark ? markerCard.dark : markerCard.light, opacity: markerWash).rounded
    }

    /// The marker's contrast against its own washed row, as drawn.
    static func markerContrast(_ c: RGB, dark: Bool) -> Double {
        contrast(c, markerRow(c, dark: dark))
    }

    /// The team's colour moved just far enough to reach `markerTarget` against its
    /// row, or nil if no step can.
    static func markerRGB(from base: RGB, dark: Bool) -> RGB? {
        if markerContrast(base, dark: dark) >= markerTarget { return base }
        let (h, s, v) = base.hsv
        var candidates: [RGB] = []
        if dark {
            for i in 1...100 {
                let nv = min(1, v + 0.01 * Double(i))
                candidates.append(RGB(h: h, s: s, v: nv))
                if nv >= 1 { break }
            }
            for i in 1...100 { candidates.append(RGB(h: h, s: max(0, s - 0.01 * Double(i)), v: 1)) }
        } else {
            for i in 1...100 { candidates.append(RGB(h: h, s: s, v: max(0, v - 0.01 * Double(i)))) }
        }
        return candidates.first { markerContrast($0, dark: dark) >= markerTarget }
    }

    /// The current-batter marker's colour for a club, or nil for a code that
    /// doesn't resolve (the caller falls back to the system accent).
    static func marker(for teamCode: String?, dark: Bool) -> Color? {
        guard let teamCode, let hex = hexByCode[teamCode.uppercased()], let base = RGB(hex: hex) else { return nil }
        return markerRGB(from: base, dark: dark)?.color
    }

    /// The 30 franchises' canonical codes, for tests that must cover every club.
    static var canonicalCodes: [String] {
        ["ARI", "ATL", "BAL", "BOS", "CHC", "CWS", "CIN", "CLE", "COL", "DET", "HOU", "KCR", "LAA", "LAD", "MIA",
         "MIL", "MIN", "NYM", "NYY", "OAK", "PHI", "PIT", "SDP", "SFG", "SEA", "STL", "TBR", "TEX", "TOR", "WSN"]
    }

    static func hex(for teamCode: String) -> String? { hexByCode[teamCode.uppercased()] }
}
