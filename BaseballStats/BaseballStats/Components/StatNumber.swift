//
//  StatNumber.swift
//  BaseballStats
//
//  A stat number of 1,000 or more reads with a thousands comma everywhere
//  (Isaac, 2026-10-27): "1,140" strikeouts, "3,000.1" innings. The comma is
//  pinned, not the locale's separator, as PlayerCompareView already does.
//
//  ⚠️ NEVER FOR A YEAR, AN ID OR ANYTHING PARSED BACK. "2,026" is wrong, an id
//  in a URL or a key must stay bare, and `Double("1,234.2")` is nil — the IP
//  parsers (BoxScoreView.ipStringToDecimal, PlayerViewModel.ipBaseballToDecimal)
//  read unformatted strings only.
//

import Foundation

enum StatNumber {
    private static let formatter: NumberFormatter = {
        let f = NumberFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.numberStyle = .decimal
        f.usesGroupingSeparator = true
        f.groupingSeparator = ","
        f.groupingSize = 3
        f.maximumFractionDigits = 0
        return f
    }()

    /// 1140 → "1,140"; −1140 → "-1,140"; 999 → "999".
    static func grouped(_ n: Int) -> String {
        formatter.string(from: NSNumber(value: n)) ?? String(n)
    }

    /// Innings already in baseball notation, as the server sends them:
    /// "3000.1" → "3,000.1". Anything that isn't a number comes back as it was.
    static func groupedIP(_ s: String) -> String {
        let parts = s.split(separator: ".", maxSplits: 1, omittingEmptySubsequences: false)
        guard let whole = Int(parts[0]) else { return s }
        return grouped(whole) + (parts.count > 1 ? "." + parts[1] : "")
    }
}
