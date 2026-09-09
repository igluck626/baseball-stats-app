//
//  LivePayloadDecodeTests.swift
//  BaseballStatsTests
//
//  Decoding the live snapshot with the decoder that actually reads it.
//
//  ⚠️ THIS IS THE ASSERTION THAT WOULD HAVE CAUGHT THE OUTAGE. Adding
//  batted-ball metrics to the live payload took every live box score
//  down while finished games carried on working, because the metrics
//  were decoded as `BDLPlateAppearance` — a type belonging to the OTHER
//  client. The two decode differently:
//
//    • `BallDontLieClient` sets `.convertFromSnakeCase`, so its models
//      declare no `CodingKeys`.
//    • `APIClient` (our backend) uses a PLAIN `JSONDecoder`, so every
//      model on that path spells its own keys.
//
//  `BDLPlateAppearance.paNumber` is non-optional and the payload says
//  `pa_number`, so the plain decoder threw `keyNotFound` — and one throw
//  fails the WHOLE `LiveGameDetail`, which is why the symptom was "live
//  games don't load" rather than "metrics missing".
//
//  Every check here therefore decodes with a PLAIN decoder. Using a
//  convenience that sets a strategy would test something no code path
//  performs and would have passed straight through the bug.
//
//  Fixture: testdata/live-detail.json — a real snapshot of a game IN
//  PROGRESS (CIN @ LAD, 2026-09-09, 9th inning), captured from
//  production while the outage was being diagnosed.
//
//  ⚠️ KNOWN GAP IN THE FIXTURE. It was captured before `_exact_speeds`
//  reached production, so every `pitch_velocity` in it is a whole number
//  taken straight from the play stream. `playRowsCarryPitchIdentity`
//  therefore asserts that speeds are PRESENT, not that they carry the
//  decimal the plate-appearance feed supplies. The decimal path has no
//  real payload behind it here.
//
//  Recapture from a game in progress once that deploy is live —
//  `curl <base>/live/games/<id> > testdata/live-detail.json` — and then
//  tighten the test to require a decimal. Until that happens, treat this
//  suite as covering the DECODE, not the precision.
//

import Foundation
import Testing
@testable import BaseballStats

private func liveFixtureData() throws -> Data {
    let url = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()   // BaseballStatsTests
        .deletingLastPathComponent()   // BaseballStats
        .deletingLastPathComponent()   // repo root
        .appendingPathComponent("testdata/live-detail.json")
    return try Data(contentsOf: url)
}

/// The decoder `APIClient` uses. Deliberately bare.
private func apiClientDecoder() -> JSONDecoder { JSONDecoder() }

@Suite("Live payload decode")
struct LivePayloadDecodeTests {

    @Test func theWholeSnapshotDecodesWithApiClientsDecoder() throws {
        let detail = try apiClientDecoder().decode(
            LiveGameDetail.self, from: try liveFixtureData())
        #expect(detail.status == "in_progress", "fixture is no longer a live game")
        #expect(!detail.plays.isEmpty)
        #expect(detail.summary.away.abbreviation?.isEmpty == false)
    }

    /// The contact blocks specifically — the payload that caused it.
    @Test func contactBlocksSurviveAndConvert() throws {
        let detail = try apiClientDecoder().decode(
            LiveGameDetail.self, from: try liveFixtureData())
        let raw = try #require(detail.contactPAs, "fixture carries no contact blocks")
        #expect(raw.count > 20, "fixture thinned out; this stopped covering the payload")

        let pas = detail.contactPlateAppearances
        #expect(pas.count == raw.count)
        // The two fields that threw: both must survive the conversion.
        #expect(pas.allSatisfy { $0.paNumber > 0 }, "pa_number was lost in decode")
        #expect(pas.allSatisfy { $0.inning > 0 },   "inning was lost in decode")
        #expect(pas.contains { ($0.pitches?.first?.exitVelocity ?? 0) > 0 },
                "no exit velocity survived")
        #expect(pas.allSatisfy { ($0.halfInning == "top" || $0.halfInning == "bottom") })
    }

    /// ⚠️ The bug itself, pinned. If this ever starts passing, someone has
    /// added `CodingKeys` to `BDLPlateAppearance` — which fixes nothing
    /// here and BREAKS `BallDontLieClient`, whose strategy rewrites a key
    /// before matching it against those raw values.
    @Test func theBdlModelStillCannotBeDecodedByThePlainDecoder() throws {
        let json = Data("""
        [{"batter_id":745,"inning":1,"half_inning":"top","pa_number":2}]
        """.utf8)
        #expect(throws: DecodingError.self) {
            _ = try apiClientDecoder().decode([BDLPlateAppearance].self, from: json)
        }
        // and it decodes fine with the strategy its own client sets
        let bdl = JSONDecoder()
        bdl.keyDecodingStrategy = .convertFromSnakeCase
        let ok = try bdl.decode([BDLPlateAppearance].self, from: json)
        #expect(ok.first?.paNumber == 2)
    }

    /// Pitch identity on a play row, the other field added to this payload.
    @Test func playRowsCarryPitchIdentity() throws {
        let detail = try apiClientDecoder().decode(
            LiveGameDetail.self, from: try liveFixtureData())
        let withSpeed = detail.plays.filter { $0.pitchVelocity != nil }
        #expect(withSpeed.count > 100, "pitch speeds vanished from the payload")
        #expect(withSpeed.allSatisfy { $0.pitchType?.isEmpty == false })
        // and they survive the bridge to the plays list's own type
        let bridged = detail.playsAsBDL.filter { $0.pitchVelocity != nil }
        #expect(bridged.count == withSpeed.count)
    }

    /// Every type on this path must spell its keys, including the ones
    /// whose fields are all single words today.
    @Test func aSnapshotMissingOptionalBlocksStillDecodes() throws {
        var obj = try #require(try JSONSerialization.jsonObject(
            with: try liveFixtureData()) as? [String: Any])
        obj.removeValue(forKey: "contact_pas")
        let trimmed = try JSONSerialization.data(withJSONObject: obj)
        let detail = try apiClientDecoder().decode(LiveGameDetail.self, from: trimmed)
        #expect(detail.contactPAs == nil)
        #expect(detail.contactPlateAppearances.isEmpty)
    }
}
