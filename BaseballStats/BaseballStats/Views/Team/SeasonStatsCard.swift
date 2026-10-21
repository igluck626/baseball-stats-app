//
//  SeasonStatsCard.swift
//  BaseballStats
//
//  The team page's Season Stats card: Run differential, then the club's
//  batting and pitching totals, each with its MLB rank (1–30), regular season
//  only. Everything comes ranked from GET /teams/season-stats; nothing is
//  computed here (SeasonStatsFormat only formats).
//
//  Laid out by measured width: ViewThatFits puts Batting and Pitching side by
//  side when both fit the card, else one above the other; each row is one line
//  when label, value and rank fit, else the label goes above. The rank column
//  is as wide as "T-30th" in the current text size, so every value lines up.
//

import SwiftUI

struct SeasonStatsCard: View {
    let bdlTeamId: Int
    let season: Int
    let tint: Color

    @Environment(\.colorScheme) private var colorScheme
    @State private var team: TeamSeasonStatsResponse.Team?

    var body: some View {
        // Nothing until the totals arrive; a failed fetch keeps the card away.
        if let team {
            VStack(alignment: .leading, spacing: 12) {
                HomeSectionHeader(title: "Season Stats", tint: tint) {
                    Text("MLB rank")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
                ViewThatFits(in: .horizontal) {
                    VStack(alignment: .leading, spacing: 14) {
                        rows(SeasonStatsFormat.overall, team: team)
                        HStack(alignment: .top, spacing: 20) {
                            group("Batting", keys: SeasonStatsFormat.batting, team: team)
                            group("Pitching", keys: SeasonStatsFormat.pitching, team: team)
                        }
                    }
                    VStack(alignment: .leading, spacing: 14) {
                        rows(SeasonStatsFormat.overall, team: team)
                        group("Batting", keys: SeasonStatsFormat.batting, team: team)
                        group("Pitching", keys: SeasonStatsFormat.pitching, team: team)
                    }
                }
                .padding(16)
                .background(teamWashBackground(tint: tint, cornerRadius: 18, isDark: colorScheme == .dark))
                .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous)
                    .strokeBorder(tint.opacity(0.25), lineWidth: 1))
                .shadow(color: .black.opacity(0.06), radius: 8, x: 0, y: 3)
                .padding(.horizontal, 16)
            }
        } else {
            Color.clear.frame(height: 0)
                .task(id: bdlTeamId) { await load() }
        }
    }

    private func load() async {
        guard let r = try? await APIClient.shared.getTeamSeasonStats(season: season) else { return }
        team = r.teams.first { $0.bdlId == bdlTeamId }
    }

    /// Rows with no heading: Run differential. A key the server doesn't send
    /// (an older server without RD) shows no row.
    private func rows(_ keys: [String], team: TeamSeasonStatsResponse.Team) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(keys.filter { team.stats[$0] != nil }, id: \.self) { key in
                row(key: key, stat: team.stats[key]!)
            }
        }
    }

    private func group(_ title: String, keys: [String], team: TeamSeasonStatsResponse.Team) -> some View {
        let shown = keys.filter { team.stats[$0] != nil }
        return VStack(alignment: .leading, spacing: 0) {
            Text(title.uppercased())
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
                .padding(.bottom, 6)
            ForEach(Array(shown.enumerated()), id: \.element) { i, key in
                row(key: key, stat: team.stats[key]!)
                if i < shown.count - 1 { Divider().opacity(0.3) }
            }
        }
        .frame(minWidth: 0, maxWidth: .infinity, alignment: .leading)
    }

    private func row(key: String, stat: TeamSeasonStatsResponse.Stat) -> some View {
        let value = SeasonStatsFormat.value(key, stat.value)
        return ViewThatFits(in: .horizontal) {
            HStack(spacing: 10) {
                Text(stat.label).font(.subheadline).lineLimit(1).fixedSize()
                Spacer(minLength: 8)
                valueText(value).fixedSize()
                rank(stat).fixedSize()
            }
            VStack(alignment: .leading, spacing: 4) {
                Text(stat.label).font(.subheadline)
                HStack(spacing: 10) {
                    valueText(value)
                    Spacer(minLength: 8)
                    rank(stat)
                }
            }
        }
        .padding(.vertical, 8)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(stat.label), \(value), \(SeasonStatsFormat.spokenRank(stat))")
    }

    private func valueText(_ s: String) -> some View {
        Text(s)
            .font(.subheadline.weight(.semibold))
            .monospacedDigit()
    }

    /// "2nd", "T-9th", in a column as wide as the widest rank can be.
    private func rank(_ s: TeamSeasonStatsResponse.Stat) -> some View {
        ZStack(alignment: .trailing) {
            Text("T-30th").hidden()
            Text(SeasonStatsFormat.rank(s)).foregroundStyle(.secondary)
        }
        .font(.subheadline)
        .monospacedDigit()
    }
}
