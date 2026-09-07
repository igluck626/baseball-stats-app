//
//  RosterPositionTests.swift
//  BaseballStatsTests
//
//  Which bucket a roster player lands in.
//
//  ⚠️ WHY THIS EXISTS. `RosterPositionGroup.from(_:)` used to end
//  `default: return .infield`, so any position string it didn't know was
//  filed with the infielders — among the HITTERS. Seth Halvorsen, a Dodgers
//  reliever, sat there for weeks because BDL calls him "Relief Pitcher" and
//  the bucketer only knew "RP". The wrongness was invisible precisely because
//  the default produced a plausible answer instead of no answer.
//
//  Two things are pinned here: that every string BDL actually ships is
//  understood, and that an unknown one comes back nil rather than being
//  quietly filed somewhere reasonable-looking.
//

import Testing
@testable import BaseballStats

@Test func bothOfBDLsPositionVocabulariesAreUnderstood() {
    // Measured 2026-09-07 over 1200 BDL players: 24 distinct strings, twelve
    // of them spelled out and covering about forty per cent of the table.
    // Active rosters are almost entirely abbreviated (1 of 940), which is why
    // only one man was ever visibly wrong.
    let pitchers = ["SP", "RP", "CL", "P", "Starting Pitcher", "Relief Pitcher",
                    "Closer", "Pitcher"]
    let infield  = ["1B", "2B", "3B", "SS", "IF", "First Baseman",
                    "Second Baseman", "Third Baseman", "Shortstop", "Infielder"]
    let outfield = ["LF", "CF", "RF", "OF", "Left Fielder", "Center Fielder",
                    "Right Fielder", "Outfielder"]

    for raw in pitchers {
        let g = RosterPositionGroup.from(raw)
        #expect(g == .sp || g == .rp, "\(raw) bucketed as \(String(describing: g))")
    }
    for raw in infield {
        #expect(RosterPositionGroup.from(raw) == .infield, "\(raw)")
    }
    for raw in outfield {
        #expect(RosterPositionGroup.from(raw) == .outfield, "\(raw)")
    }
    #expect(RosterPositionGroup.from("C") == .c)
    #expect(RosterPositionGroup.from("Catcher") == .c)
    #expect(RosterPositionGroup.from("DH") == .dh)
    #expect(RosterPositionGroup.from("Designated Hitter") == .dh)
}

@Test func theSpelledOutFormIsTheCaseThatShipped() {
    // The exact string that put a reliever among the hitters.
    #expect(RosterPositionGroup.from("Relief Pitcher") == .rp)
    #expect(RosterPositionGroup.from("Relief Pitcher") != .infield)
}

@Test func casingDoesNotDecideTheBucket() {
    for raw in ["relief pitcher", "RELIEF PITCHER", "Relief Pitcher"] {
        #expect(RosterPositionGroup.from(raw) == .rp, "\(raw)")
    }
}

@Test func anUnknownPositionIsNilRatherThanPlausible() {
    // ⚠️ THE POINT. Not `.infield`, not `.rp`, not a guess dressed as an
    // answer — nil, so the caller has to decide and the Roster sheet can put
    // him in a section that says out loud that we don't know.
    for raw in ["Utility", "Two-Way Player", "PH", "", "???"] {
        #expect(RosterPositionGroup.from(raw) == nil,
                "\(raw) got a bucket it should not have")
    }
}

@Test func theUnknownBucketIsNotAHittingBucket() {
    // If `.other` ever becomes a hitters-only section, the Halvorsen fault
    // comes straight back in a new costume: a pitcher listed under Hitters.
    #expect(RosterPositionGroup.other.displayName == "?")
    #expect(RosterPositionGroup.other != .infield)
    #expect(RosterPositionGroup.other != .dh)
}

// MARK: - Display and bucketing read the same table

@Test func theDisplayedAbbreviationMatchesTheBucket() {
    // The reported bug: the Roster sheet printed the raw string, so one
    // reliever read "RELIEF PITCHER" in a column of RP and SP. Display now
    // goes through the same table as bucketing, so the two cannot disagree.
    #expect(PositionAbbreviation.canonical("Relief Pitcher") == "RP")
    #expect(PositionAbbreviation.canonical("Starting Pitcher") == "SP")
    #expect(PositionAbbreviation.canonical("Shortstop") == "SS")
    #expect(PositionAbbreviation.canonical("Designated Hitter") == "DH")
    // Already short, and nil.
    #expect(PositionAbbreviation.canonical("RP") == "RP")
    #expect(PositionAbbreviation.canonical(nil) == "")
    // Unknown passes through legibly rather than becoming an em-dash — the
    // `.other` section is what flags it, not a blanked cell.
    #expect(PositionAbbreviation.canonical("Utility") == "Utility")
}

@Test func theOldEntryPointStillResolvesToTheSameTable() {
    // Two screens call `InjuryReportSheet.abbreviatePosition`; it now forwards.
    for raw in ["Relief Pitcher", "Center Fielder", "SP", "Utility"] {
        #expect(InjuryReportSheet.abbreviatePosition(raw)
                == PositionAbbreviation.canonical(raw), "\(raw)")
    }
}
