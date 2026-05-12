//
//  LeagueRankingsCard.swift
//  BaseballStats
//
//  Overview-tab card surfacing where the player ranks within their
//  league on a fixed set of headline stats. Renders any qualifying
//  ranking (player appears in the league's top 25 for the season) —
//  even one qualifying stat is enough to keep the card on screen.
//  The card hides only when *zero* stats qualify or when we can't
//  resolve the player's league from their team code.
//
//  Rankings are computed client-side by replaying the existing
//  /leaderboards endpoint once per stat with limit=25 and league=
//  this player's league. Cheap on the wire (4–6 small responses) and
//  it means we don't need a dedicated "player-in-leaderboards" API.
//

import Combine
import SwiftUI

// MARK: - Model

/// One ranked stat for display. `value` carries the leaderboard's
/// pre-sorted numeric value so we can show it without a second fetch
/// from the player's own season totals.
struct PlayerRanking: Identifiable, Hashable {
    let stat: String      // "WAR", "HR", "AVG", …
    let rank: Int
    let league: String    // "AL" / "NL"
    let value: Double?

    var id: String { stat }
}

// MARK: - ViewModel

@MainActor
final class LeagueRankingsViewModel: ObservableObject {
    let playerId: Int
    let isPitcher: Bool
    let season: Int
    /// Raw team code as carried by `PlayerSearchResult` — the backend's
    /// `team_code` field. The VM resolves this to a league internally
    /// instead of having the parent gate the card, so the card can log
    /// the mapping outcome on every load (helpful for diagnosing the
    /// "card never appears" failure mode where the gate silently
    /// swallowed an unmapped code).
    let teamCode: String?

    @Published var rankings: [PlayerRanking] = []
    @Published var isLoading = false
    @Published var error: String?
    /// True once the load has completed at least once. The view uses
    /// this to distinguish "still spinning, don't render anything yet"
    /// from "finished and found nothing — collapse the card."
    @Published var didLoad = false

    private let api: APIClient

    /// Headline stats per role, in display order. WAR leads both lists
    /// — the modern catch-all metric most users scan first.
    static let battingStats:  [String] = ["WAR", "HR", "AVG", "OPS", "RBI", "SB"]
    static let pitchingStats: [String] = ["WAR", "ERA", "SO", "WHIP"]

    init(playerId: Int, isPitcher: Bool, season: Int, teamCode: String?, api: APIClient = .shared) {
        self.playerId = playerId
        self.isPitcher = isPitcher
        self.season = season
        self.teamCode = teamCode
        self.api = api
    }

    /// Fan out one /leaderboards call per stat (limit=25, league=ours)
    /// in parallel; surface every stat where this player appears in
    /// the response — even one is enough for the card to show. The
    /// view collapses the card only when `rankings == []`.
    func load() async {
        isLoading = true
        error = nil

        let league = leagueForTeamCode(teamCode)
        guard let league else {
            // No league resolved → no meaningful filter we can pass to
            // the leaderboard endpoint. Collapse the card.
            rankings = []
            didLoad = true
            isLoading = false
            return
        }

        let stats = isPitcher
            ? Self.pitchingStats
            : Self.battingStats
        let playerType = isPitcher ? "pitcher" : "batter"

        let results: [PlayerRanking] = await withTaskGroup(of: PlayerRanking?.self) { group in
            for stat in stats {
                group.addTask { [api, playerId, league, season] in
                    do {
                        let response = try await api.getLeaderboard(
                            stat:       stat,
                            year:       season,
                            playerType: playerType,
                            league:     league,
                            team:       nil,
                            limit:      25
                        )
                        guard let leaders = response?.leaders else { return nil }
                        guard let hit = leaders.first(where: { $0.player.player_id == playerId })
                        else { return nil }
                        return PlayerRanking(
                            stat: stat,
                            rank: hit.rank,
                            league: league,
                            value: hit.value
                        )
                    } catch {
                        // Per-stat failures shouldn't tank the whole card —
                        // just omit that stat from the results.
                        return nil
                    }
                }
            }
            var hits: [String: PlayerRanking] = [:]
            for await maybe in group {
                if let r = maybe { hits[r.stat] = r }
            }
            // Preserve the catalog's display order.
            return stats.compactMap { hits[$0] }
        }

        rankings = results
        didLoad = true
        isLoading = false
    }
}

// MARK: - View

/// Glass card listing the player's league rankings. Renders nothing
/// until the first load completes; after that, hidden iff zero stats
/// qualified (or the team code couldn't be resolved to a league).
struct LeagueRankingsCard: View {
    @StateObject private var vm: LeagueRankingsViewModel

    init(playerId: Int, isPitcher: Bool, season: Int, teamCode: String?) {
        _vm = StateObject(wrappedValue: LeagueRankingsViewModel(
            playerId: playerId, isPitcher: isPitcher, season: season, teamCode: teamCode
        ))
    }

    var body: some View {
        // ZStack so a zero-height Color.clear sentinel anchors the
        // view even when both conditional branches resolve to empty
        // (the initial state: isLoading=false, didLoad=false,
        // rankings=[]). SwiftUI strips lifecycle modifiers like
        // .task from a body that collapses to EmptyView, so without
        // the sentinel `load()` would never fire on first mount.
        ZStack {
            Color.clear.frame(height: 0)
            if vm.isLoading && !vm.didLoad {
                loadingBody
            } else if !vm.rankings.isEmpty {
                loadedBody
            }
            // didLoad && rankings.isEmpty → only the sentinel renders,
            // closing the section gap so the Career card slides up
            // flush against Recent Games.
        }
        .task { await vm.load() }
    }

    private var loadingBody: some View {
        VStack(spacing: 10) {
            HStack {
                Text("League Rankings").font(.headline)
                Spacer()
                ProgressView().controlSize(.small)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .frame(maxWidth: .infinity)
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 20))
        .shadow(color: .black.opacity(0.06), radius: 8, x: 0, y: 2)
    }

    private var loadedBody: some View {
        VStack(spacing: 0) {
            HStack {
                Text("League Rankings").font(.headline)
                Spacer()
            }
            .padding(.horizontal, 14)
            .padding(.top, 12)
            .padding(.bottom, 8)

            VStack(spacing: 0) {
                ForEach(Array(vm.rankings.enumerated()), id: \.element.id) { idx, ranking in
                    rankingRow(ranking)
                    if idx != vm.rankings.indices.last {
                        Divider().opacity(0.4)
                    }
                }
            }
        }
        .frame(maxWidth: .infinity)
        .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 20))
        .shadow(color: .black.opacity(0.06), radius: 8, x: 0, y: 2)
    }

    private func rankingRow(_ r: PlayerRanking) -> some View {
        HStack(spacing: 12) {
            // Rank badge — small accent-tinted pill for top-3 rankings
            // (highlights the headline finishes), plain numeric for the
            // rest of the top 25.
            rankBadge(rank: r.rank)
            VStack(alignment: .leading, spacing: 1) {
                Text("\(ordinal(r.rank)) in \(r.league) · \(r.stat)")
                    .font(.subheadline)
                    .lineLimit(1)
            }
            Spacer()
            Text(formatRankingValue(stat: r.stat, value: r.value))
                .font(.subheadline.weight(.semibold))
                .monospacedDigit()
                .foregroundStyle(.primary)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .contentShape(Rectangle())
    }

    /// Top-3 ranks get the accent fill — visually parallels the
    /// gold-tinted "league leader" cells on the Career table. Ranks
    /// 4–25 render as a plain numeric chip.
    private func rankBadge(rank: Int) -> some View {
        let isTopThree = rank <= 3
        return Text("\(rank)")
            .font(.caption.weight(.bold))
            .monospacedDigit()
            .foregroundStyle(isTopThree ? Color.white : Color.primary)
            .frame(width: 26, height: 26)
            .background(
                Circle()
                    .fill(isTopThree
                          ? Color.accentColor
                          : Color(.secondarySystemFill))
            )
    }
}

// MARK: - Formatters

/// "1st" / "2nd" / "3rd" / "4th" / "11th" / "23rd" / … Standard English
/// ordinal — covers the full 1..100 range without hardcoding a table.
private func ordinal(_ n: Int) -> String {
    let mod100 = n % 100
    if (11...13).contains(mod100) { return "\(n)th" }
    switch n % 10 {
    case 1: return "\(n)st"
    case 2: return "\(n)nd"
    case 3: return "\(n)rd"
    default: return "\(n)th"
    }
}

/// Per-stat value formatting — counting stats are integers, rate stats
/// keep their conventional decimal precision and (for batting rates)
/// strip the leading zero per Baseball Reference style.
private func formatRankingValue(stat: String, value: Double?) -> String {
    guard let value else { return "—" }
    switch stat {
    case "WAR", "ERA", "WHIP":
        return String(format: "%.2f", value)
    case "AVG", "OBP", "SLG", "OPS":
        let s = String(format: "%.3f", value)
        if s.hasPrefix("0.")  { return String(s.dropFirst()) }
        if s.hasPrefix("-0.") { return "-" + String(s.dropFirst(2)) }
        return s
    default:
        // HR / RBI / SB / SO / H / R / BB / 2B / 3B / PA / AB — integer
        // counting stats. Round in case the backend ships them as
        // Double (it does for some columns to keep the type uniform).
        return String(Int(value.rounded()))
    }
}

// MARK: - League resolution

/// All-30-franchise team-code → league mapping, accepting whichever of
/// the three common code dialects PlayerSearchResult.teamCode happens
/// to be normalized to: Lahman ("LAN", "NYA", "CHN", …), MLB Stats API
/// ("LAD", "NYY", "CHC", …), and Baseball Reference ("LAD", "NYY",
/// "CHC", "TBR", …). The backend tries to normalize to Lahman, but
/// real-world payloads have shown both formats depending on which
/// loader wrote the row, so we accept everything.
///
/// Comparison is case-insensitive and tolerates an empty/nil input.
/// Returns nil for unknown codes — the rankings card hides in that
/// case since we can't form a meaningful league filter.
private let teamCodeToLeague: [String: String] = [
    // Lahman canonical codes
    "ARI": "NL", "ATL": "NL", "BAL": "AL", "BOS": "AL",
    "CHN": "NL", "CHA": "AL", "CIN": "NL", "CLE": "AL",
    "COL": "NL", "DET": "AL", "HOU": "AL", "KCA": "AL",
    "LAA": "AL", "LAN": "NL", "MIA": "NL", "MIL": "NL",
    "MIN": "AL", "NYN": "NL", "NYA": "AL", "OAK": "AL",
    "PHI": "NL", "PIT": "NL", "SDN": "NL", "SFN": "NL",
    "SEA": "AL", "SLN": "NL", "TBA": "AL", "TEX": "AL",
    "TOR": "AL", "WAS": "NL",

    // MLB Stats API + Baseball Reference 3-letter codes
    "CHC": "NL",                 // Cubs
    "CWS": "AL", "CHW": "AL",    // White Sox (both abbreviations seen)
    "KCR": "AL", "KC":  "AL",    // Royals
    "LAD": "NL",                 // Dodgers (most common modern format)
    "NYM": "NL",                 // Mets
    "NYY": "AL",                 // Yankees
    "SDP": "NL", "SD":  "NL",    // Padres
    "SFG": "NL", "SF":  "NL",    // Giants
    "STL": "NL",                 // Cardinals
    "TBR": "AL", "TB":  "AL",    // Rays
    "WSH": "NL", "WSN": "NL",    // Nationals

    // Historical / bbref legacy codes occasionally surfaced when the
    // backend's most-recent-season row is pre-rename.
    "FLO": "NL",                 // Marlins, pre-2012 rename
    "ANA": "AL",                 // Angels, 1997–2004 branding
    "MON": "NL",                 // Expos → Nationals, pre-2005
]

/// Public so PlayerProfileView and callers in other files can probe
/// the mapping (e.g. to decide whether to even render the card host).
/// Backed by `teamCodeToLeague`; centralized here so a future code
/// dialect addition only needs to update the one dictionary.
func leagueForTeamCode(_ code: String?) -> String? {
    guard let code, !code.isEmpty else { return nil }
    return teamCodeToLeague[code.uppercased()]
}
