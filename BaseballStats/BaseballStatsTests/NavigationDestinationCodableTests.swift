//
//  NavigationDestinationCodableTests.swift
//
//  Every type a stack can push must be Codable, or that stack's saved path
//  silently stops saving: `NavigationPath.codable` is nil as soon as one value
//  on it isn't, and the scene then reopens at the stack's root with nothing to
//  say why (see `SceneRestoration.encode`).
//
//  A source scan, so a NEW destination is caught without anyone remembering to
//  list it: every `navigationDestination(for: T.self)` in the app's sources,
//  and for each T a declaration or extension that names `Codable`.
//

import Foundation
import Testing

struct NavigationDestinationCodableTests {
    /// The app's sources: `BaseballStatsTests/<this file>` -> `../BaseballStats`.
    static let sourceRoot = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent()
        .appendingPathComponent("BaseballStats")

    static func swiftSources() throws -> [String] {
        let files = FileManager.default.enumerator(at: sourceRoot, includingPropertiesForKeys: nil)
        var out: [String] = []
        while let url = files?.nextObject() as? URL {
            if url.pathExtension == "swift" { out.append(try String(contentsOf: url, encoding: .utf8)) }
        }
        return out
    }

    /// Code only: comments describe destinations without declaring them.
    static func stripComments(_ s: String) -> String {
        s.replacingOccurrences(of: #"/\*[\s\S]*?\*/"#, with: "", options: .regularExpression)
         .replacingOccurrences(of: #"//[^\n]*"#, with: "", options: .regularExpression)
    }

    static func matches(_ pattern: String, in s: String) -> [[String]] {
        let re = try! NSRegularExpression(pattern: pattern)
        return re.matches(in: s, range: NSRange(s.startIndex..., in: s)).map { m in
            (0..<m.numberOfRanges).map { i in
                Range(m.range(at: i), in: s).map { String(s[$0]) } ?? ""
            }
        }
    }

    @Test func everyNavigationDestinationTypeIsCodable() throws {
        let sources = try Self.swiftSources().map(Self.stripComments)
        #expect(sources.count > 50, "found \(sources.count) sources under \(Self.sourceRoot.path)")
        let all = sources.joined(separator: "\n")

        let types = Set(Self.matches(#"navigationDestination\(\s*for:\s*([A-Za-z_][A-Za-z0-9_.]*)\.self"#, in: all)
            .map { $0[1].components(separatedBy: ".").last! })
        // Today: Game, PlayerSearchResult, TeamNewsDestination,
        // AwardVotingBrowserDestination, PostseasonBracketDestination.
        #expect(types.count >= 5, "destinations found: \(types.sorted())")

        for type in types.sorted() {
            let name = NSRegularExpression.escapedPattern(for: type)
            let declared = !Self.matches(
                #"(?:struct|class|enum|actor)\s+"# + name + #"\b[^{]*:[^{]*\bCodable\b"#, in: all).isEmpty
            let extended = !Self.matches(
                #"extension\s+"# + name + #"\s*:[^{]*\bCodable\b"#, in: all).isEmpty
            #expect(declared || extended,
                    "\(type) is pushed with navigationDestination(for:) but isn't Codable — its stack's path can't be saved")
        }
    }
}
