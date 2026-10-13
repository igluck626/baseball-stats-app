//
//  MarkerContrastTests.swift
//  BaseballStatsTests
//
//  The live box score's current-batter marker (`TeamColors.marker`): every
//  club's marker reaches 3:1 against the row it sits on AS DRAWN — the card
//  washed 15% with the marker's own colour, in whole channels — in light and
//  dark mode, in the club's own hue, and a colour that already reads is left
//  alone.
//

import SwiftUI
import Testing
@testable import BaseballStats

@MainActor
struct MarkerContrastTests {
    typealias RGB = TeamColors.RGB

    private func base(_ code: String) -> RGB { RGB(hex: TeamColors.hex(for: code)!)! }

    @Test func theCardMatchesTheRenderedRows() {
        // The row as drawn reproduces the marked rows' pixels in the 2026-10-10
        // simulator screenshots: CIN in light mode, CHW in dark.
        #expect(TeamColors.markerRow(base("CIN"), dark: false) == RGB(r: 237, g: 216, b: 216))
        #expect(TeamColors.markerRow(base("CWS"), dark: true) == RGB(r: 33, g: 33, b: 32))
    }

    @Test func theRuleIsWhatTheSimulatorDrew() {
        // Glyph and row pixels sampled from the 2026-10-13 simulator renders of
        // the rule's markers in the box score's glass card (the build-15 contact
        // sheet): the model's colour and its row as drawn are those pixels, and
        // every one clears 3:1.
        let samples: [(code: String, dark: Bool, glyph: RGB, row: RGB)] = [
            ("DET", true, RGB(r: 40, g: 117, b: 214), RGB(r: 33, g: 45, b: 59)),
            ("CWS", true, RGB(r: 126, g: 119, b: 100), RGB(r: 46, g: 45, b: 42)),
            ("OAK", true, RGB(r: 0, g: 132, b: 116), RGB(r: 27, g: 47, b: 45)),
            ("SDP", true, RGB(r: 163, g: 106, b: 0), RGB(r: 52, g: 43, b: 27)),
            ("TEX", true, RGB(r: 0, g: 106, b: 255), RGB(r: 27, g: 43, b: 65)),
            ("ARI", false, RGB(r: 143, g: 133, b: 109), RGB(r: 237, g: 236, b: 232)),
        ]
        for s in samples {
            let m = TeamColors.markerRGB(from: base(s.code), dark: s.dark)
            #expect(m == s.glyph, "\(s.code): rule \(String(describing: m)), drawn \(s.glyph)")
            #expect(TeamColors.markerRow(s.glyph, dark: s.dark) == s.row, "\(s.code) row")
            #expect(TeamColors.contrast(s.glyph, s.row) >= 3, "\(s.code) as drawn")
        }
    }

    @Test func everyClubCovered() {
        #expect(TeamColors.canonicalCodes.count == 30)
        #expect(Set(TeamColors.canonicalCodes).count == 30)
        for code in TeamColors.canonicalCodes { #expect(TeamColors.hex(for: code) != nil, "\(code)") }
    }

    @Test(arguments: [false, true])
    func everyMarkerReadsAgainstItsRow(dark: Bool) {
        for code in TeamColors.canonicalCodes {
            let m = TeamColors.markerRGB(from: base(code), dark: dark)
            #expect(m != nil, "\(code) needs the accent fallback")
            guard let m else { continue }
            let c = TeamColors.markerContrast(m, dark: dark)
            #expect(c >= TeamColors.markerTarget, "\(code) \(dark ? "dark" : "light"): \(c)")
            // Spelled out, not through markerContrast: the glyph against its washed
            // row, rounded the way the screen draws it.
            let row = m.over(dark ? TeamColors.markerCard.dark : TeamColors.markerCard.light,
                             opacity: TeamColors.markerWash)
            let drawn = RGB(r: row.r.rounded(), g: row.g.rounded(), b: row.b.rounded())
            #expect(TeamColors.contrast(m, drawn) >= 3, "\(code) \(dark ? "dark" : "light") as drawn")
            #expect(TeamColors.marker(for: code, dark: dark) != nil)
        }
    }

    @Test(arguments: [false, true])
    func aColourThatReadsIsLeftAlone_andOneThatDoesntMovesOnlyAsFarAsItMust(dark: Bool) {
        for code in TeamColors.canonicalCodes {
            let b = base(code)
            let m = TeamColors.markerRGB(from: b, dark: dark)!
            if TeamColors.markerContrast(b, dark: dark) >= TeamColors.markerTarget {
                #expect(m == b, "\(code) already reads and must keep its exact colour")
            } else {
                #expect(TeamColors.markerContrast(m, dark: dark) < 3.2, "\(code) moved further than it needed to")
            }
        }
    }

    @Test(arguments: [false, true])
    func theMarkerKeepsTheClubsHue(dark: Bool) {
        for code in TeamColors.canonicalCodes {
            let b = base(code), m = TeamColors.markerRGB(from: b, dark: dark)!
            let dh = abs(m.hsv.h - b.hsv.h)
            #expect(min(dh, 1 - dh) < 0.02, "\(code) \(dark ? "dark" : "light"): hue \(b.hsv.h) → \(m.hsv.h)")
        }
    }

    @Test func theMeasuredFailuresAreFixed() {
        // The worst before the rule (measured 2026-10-13): dark-mode navy and
        // near-black, light-mode sand and powder blue.
        #expect(TeamColors.markerContrast(base("DET"), dark: true) < 1.1)
        #expect(TeamColors.markerContrast(base("CWS"), dark: true) < 1.1)
        #expect(TeamColors.markerContrast(base("ARI"), dark: false) < 1.5)
        #expect(TeamColors.markerContrast(base("TBR"), dark: false) < 2)
        for (code, dark) in [("DET", true), ("CWS", true), ("OAK", true), ("ARI", false), ("TBR", false)] {
            #expect(TeamColors.markerContrast(TeamColors.markerRGB(from: base(code), dark: dark)!, dark: dark) >= 3)
        }
        // The three that read in both modes keep their colour in both.
        for code in ["CLE", "TOR", "BAL"] {
            for dark in [false, true] { #expect(TeamColors.markerRGB(from: base(code), dark: dark) == base(code)) }
        }
    }

    @Test func anUnknownClubFallsBackToTheAccent() {
        #expect(TeamColors.marker(for: nil, dark: false) == nil)
        #expect(TeamColors.marker(for: "XXX", dark: true) == nil)
    }
}
