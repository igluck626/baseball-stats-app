//
//  SeasonCountdownCard.swift
//
//  The offseason countdown card (Isaac, 2026-10-31) — top of Home, and top of
//  Scores on a day with no games. Draws nothing until there is a countdown, so
//  a season in progress (or a failed fetch) leaves no gap.
//
//      2027 SEASON
//      Pitchers & catchers report
//      [101] [08] [42] [17]
//       DAYS  HRS  MIN  SEC
//      Tuesday, February 9
//
//  A countdown clock (Isaac asked for one over a plain "100 days"); at text
//  sizes too large for four tiles across, it falls back to "100 days".
//

import Combine
import SwiftUI

/// The countdown, fetched once and shared by Home and Scores. `load()` is cheap
/// to call from every `.task`: it asks again only after ten minutes.
@MainActor
final class SeasonCountdownStore: ObservableObject {
    static let shared = SeasonCountdownStore()
    @Published private(set) var countdown: SeasonCountdown?
    private var loadedAt: Date?

    func load(force: Bool = false) async {
        if !force, let t = loadedAt, Date().timeIntervalSince(t) < 600 { return }
        guard let c = try? await APIClient.shared.getSeasonCountdown() else { return }
        countdown = c
        loadedAt = Date()
    }
}

struct SeasonCountdownCard: View {
    let countdown: SeasonCountdown?
    /// The favorite team's color on Home; nil on Scores, which has no team.
    var tint: Color? = nil

    @Environment(\.colorScheme) private var colorScheme

    // ⚠️ NO VIEW AT ALL WITHOUT A COUNTDOWN, not an empty one: inside a stack
    // an empty placeholder still takes the stack's spacing, so all season Home
    // would gain a gap at the top. Hence the parent owns the fetch
    // (`SeasonCountdownStore`) — a `.task` on a view that draws nothing never runs.
    var body: some View {
        if let c = countdown, c.isActive, let season = c.season, let label = c.label {
            card(season: season, label: label, c: c)
        }
    }

    private func card(season: Int, label: String, c: SeasonCountdown) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("\(String(season)) Season")
                .font(.caption.weight(.semibold))
                .textCase(.uppercase)
                .foregroundStyle(.secondary)
            Text(label)
                .font(.headline)
            // Ticks each second while on screen; also turns the day over at
            // midnight on a screen left open.
            TimelineView(.periodic(from: .now, by: 1)) { context in
                if let r = c.remaining(context.date) {
                    ViewThatFits(in: .horizontal) {
                        clock(r)
                        countText(c, now: context.date)
                    }
                    .padding(.vertical, 6)
                } else if c.countText(context.date) != nil {
                    countText(c, now: context.date)     // "Today"
                }
            }
            Text(c.dateText)
                .font(.subheadline)
                .foregroundStyle(.secondary)
        }
        // Wrap, never truncate: at the largest text sizes the label and date
        // take two or three lines each, and a squeezed parent would cut them.
        .fixedSize(horizontal: false, vertical: true)
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(16)
        // One steady reading for VoiceOver — not a number changing every second.
        .accessibilityElement(children: .ignore)
        .accessibilityLabel([label, c.countText(), c.dateText].compactMap { $0 }.joined(separator: ", "))
        .background(background)
        .padding(.horizontal, 16)
    }

    private func clock(_ r: (days: Int, hours: Int, minutes: Int, seconds: Int)) -> some View {
        HStack(spacing: 8) {
            tile(String(r.days), "Days")
            tile(String(format: "%02d", r.hours), "Hrs")
            tile(String(format: "%02d", r.minutes), "Min")
            tile(String(format: "%02d", r.seconds), "Sec")
        }
    }

    private func tile(_ value: String, _ unit: String) -> some View {
        VStack(spacing: 2) {
            Text(value)
                .font(.title.weight(.bold))
                .monospacedDigit()
                .foregroundStyle(tint ?? .primary)
            Text(unit)
                .font(.caption2.weight(.semibold))
                .textCase(.uppercase)
                .foregroundStyle(.secondary)
        }
        .lineLimit(1)
        .fixedSize()
        .frame(minWidth: 64, maxWidth: .infinity)    // four even tiles, edge to edge
        .padding(.vertical, 8)
        .background(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .fill((tint ?? .primary).opacity(colorScheme == .dark ? 0.18 : 0.08))
        )
    }

    private func countText(_ c: SeasonCountdown, now: Date) -> some View {
        Text(c.countText(now) ?? "")
            .font(.largeTitle.weight(.bold))
            .monospacedDigit()
            .foregroundStyle(tint ?? .primary)
    }

    @ViewBuilder
    private var background: some View {
        if let tint {
            teamWashBackground(tint: tint, cornerRadius: 18, isDark: colorScheme == .dark)
                .overlay(
                    RoundedRectangle(cornerRadius: 18, style: .continuous)
                        .strokeBorder(tint.opacity(0.25), lineWidth: 1)
                )
                .shadow(color: .black.opacity(0.06), radius: 8, x: 0, y: 3)
        } else {
            RoundedRectangle(cornerRadius: 18, style: .continuous)
                .fill(.clear)
                .glassEffect(.regular, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
        }
    }
}
