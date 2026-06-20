//
//  HeatMeterView.swift
//  BaseballStats
//
//  Compact hot/cold "heat" gauge for the player profile. Renders the
//  backend's tanh-compressed `heat_score` (~±0.32) as a marker on a
//  cold→neutral→hot gradient track, with a tier label + flame/snowflake
//  icon. Driven by `heat_score` + `heat_tier` off the player bio.
//

import SwiftUI

struct HeatMeterView: View {
    /// Signed, tanh-compressed form score (~±0.32). Positive = hot.
    let score: Double
    /// Backend tier bucket: "red_hot" | "hot" | "neutral" | "cold" | "ice_cold".
    let tier: String
    /// Window label shown as a subtitle (e.g. "Last 15 games" / "Recent form").
    var window: String = "Recent form"

    /// Presents the "how the rating works" explanation sheet.
    @State private var showingInfo = false

    /// The compressed score asymptotes near ±0.32; map that span to the
    /// full track and clamp so extremes pin to the ends.
    private static let range: Double = 0.32

    private var style: HeatTierStyle { HeatTierStyle.for(tier) }

    /// 0…1 position of the marker along the track.
    private var fraction: CGFloat {
        let clamped = max(-Self.range, min(Self.range, score))
        return CGFloat((clamped + Self.range) / (2 * Self.range))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            // Tier label + icon on the left, window subtitle on the right.
            HStack(spacing: 6) {
                if let icon = style.icon {
                    Image(systemName: icon)
                        .font(.subheadline)
                        .foregroundStyle(style.color)
                }
                Text(style.label)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(style.color)
                Spacer()
                Text(window)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                Button {
                    showingInfo = true
                } label: {
                    Image(systemName: "info.circle")
                        .font(.system(size: 14))
                        .foregroundStyle(.secondary)
                        // Light glyph, comfortable tap target.
                        .padding(.vertical, 2)
                        .padding(.leading, 2)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("How Hot and Cold works")
            }

            track
        }
        .padding(14)
        .frame(maxWidth: .infinity)
        .background(
            RoundedRectangle(cornerRadius: 16, style: .continuous)
                .fill(.ultraThinMaterial)
        )
        .shadow(color: .black.opacity(0.05), radius: 6, y: 2)
        .sheet(isPresented: $showingInfo) {
            HeatInfoSheet()
        }
    }

    private var track: some View {
        GeometryReader { geo in
            let trackHeight: CGFloat = 7
            let markerSize: CGFloat = 16
            // Keep the marker fully inside the track ends.
            let inset = markerSize / 2
            let x = inset + fraction * (geo.size.width - 2 * inset)

            ZStack(alignment: .leading) {
                Capsule()
                    .fill(Self.spectrum)
                    .frame(height: trackHeight)
                    .frame(maxHeight: .infinity, alignment: .center)

                Circle()
                    .fill(.background)
                    .overlay(Circle().strokeBorder(style.color, lineWidth: 2.5))
                    .frame(width: markerSize, height: markerSize)
                    .shadow(color: .black.opacity(0.15), radius: 1.5, y: 0.5)
                    .position(x: x, y: geo.size.height / 2)
            }
        }
        .frame(height: 16)
    }

    /// Cold → neutral → hot spectrum, left to right.
    private static let spectrum = LinearGradient(
        colors: [
            Color(red: 0.10, green: 0.45, blue: 0.95),   // ice cold
            Color(red: 0.30, green: 0.72, blue: 0.92),   // cold
            Color(.systemGray3),                          // neutral
            Color(red: 0.97, green: 0.58, blue: 0.10),   // hot
            Color(red: 0.95, green: 0.24, blue: 0.13),   // red hot
        ],
        startPoint: .leading,
        endPoint: .trailing
    )
}

/// Per-tier presentation: label, SF Symbol (nil for neutral), and accent
/// color. Centralized so the meter and any callers stay consistent.
struct HeatTierStyle {
    let label: String
    let icon: String?
    let color: Color

    static func `for`(_ tier: String) -> HeatTierStyle {
        switch tier {
        case "red_hot":
            return .init(label: "Red Hot", icon: "flame.fill",
                         color: Color(red: 1.0, green: 0.27, blue: 0.0))
        case "hot":
            return .init(label: "Hot", icon: "flame.fill", color: .orange)
        case "cold":
            return .init(label: "Cold", icon: "snowflake", color: .cyan)
        case "ice_cold":
            return .init(label: "Ice Cold", icon: "snowflake",
                         color: Color(red: 0.0, green: 0.5, blue: 1.0))
        default: // "neutral" or anything unexpected
            return .init(label: "Neutral", icon: nil, color: .secondary)
        }
    }
}

/// Small inline hot/cold tier icon for list rows (Leaders / Search). A pure
/// visual accent: renders the flame/snowflake from `HeatTierStyle` in the tier
/// color. Renders NOTHING (no glyph, no spacing) when `tier` is nil or
/// "neutral" — only red_hot / hot / cold / ice_cold carry an icon. No tap
/// behavior. Reuses `HeatTierStyle` so it matches the meter exactly.
struct HeatTierBadge: View {
    let tier: String?
    var size: CGFloat = 12

    var body: some View {
        if let tier, let icon = HeatTierStyle.for(tier).icon {
            let style = HeatTierStyle.for(tier)
            Image(systemName: icon)
                .font(.system(size: size, weight: .semibold))
                .foregroundStyle(style.color)
                .accessibilityLabel("\(style.label) form")
        }
    }
}

/// Explainer sheet for the Hot & Cold rating. Plain-language summary and a
/// visual tier legend are always visible; the stats-heavy methodology lives
/// in a collapsed-by-default disclosure group.
struct HeatInfoSheet: View {
    @Environment(\.dismiss) private var dismiss
    @State private var showingMath = false

    /// Hottest-to-coldest, reusing the meter's exact tier styling.
    private static let legendTiers = ["red_hot", "hot", "neutral", "cold", "ice_cold"]

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    plainLanguage
                    tierLegend
                    Divider()
                    DisclosureGroup(isExpanded: $showingMath) {
                        howItsCalculated
                            .padding(.top, 8)
                    } label: {
                        Text("How it's calculated")
                            .font(.headline)
                    }
                }
                .padding(20)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .navigationTitle("Hot & Cold")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
        }
        .presentationDetents([.medium, .large])
    }

    // MARK: - Plain language

    private var plainLanguage: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Hot & Cold reflects how a player has been performing recently compared to both their own season and the rest of the league.")
            Text("The rating looks at a player's last 15 games (hitters), last 5 starts (starting pitchers), or last 15 appearances (relievers), then compares that stretch to their season baseline and to league average.")
            Text("A player is hot when they're outperforming both, and cold when they're underperforming. The further from average, the stronger the rating, ranging from Ice Cold to Red Hot.")
        }
        .font(.subheadline)
        .foregroundStyle(.primary)
        .fixedSize(horizontal: false, vertical: true)
    }

    // MARK: - Tier legend

    private var tierLegend: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(Self.legendTiers, id: \.self) { tier in
                let style = HeatTierStyle.for(tier)
                HStack(spacing: 12) {
                    Image(systemName: style.icon ?? "circle.fill")
                        .font(.subheadline)
                        .foregroundStyle(style.color)
                        .frame(width: 22, alignment: .center)
                    Text(style.label)
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(.primary)
                    Spacer()
                }
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(Color(.secondarySystemBackground))
        )
    }

    // MARK: - How it's calculated

    private var howItsCalculated: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Hitters are measured primarily by wOBA (weighted on-base average), which weights each offensive outcome by its real run value, so getting on base and hitting for power count for what they're actually worth. A smaller part of the rating rewards a lower strikeout rate, and active base stealing provides a small bonus.")
            formula("wOBA = (0.69×BB + 0.72×HBP + 0.89×1B + 1.27×2B + 1.62×3B + 2.10×HR) / (AB + BB + SF + HBP)")

            Text("Pitchers blend three stats: ERA (30%), WHIP (30%), and FIP (40%). FIP carries the most weight because it best reflects a pitcher's underlying performance and is less affected by luck or defense.")
            formula("FIP = (13×HR + 3×(BB+HBP) - 2×SO) / IP + 3.10")

            Text("For each stat, two signals are combined: how the recent stretch compares to the player's own season (35%) and to league average (65%).")
            formula("score = (0.35 × trend vs self) + (0.65 × vs league average)")

            Text("Weighting league average more heavily keeps the rating grounded in real quality, similar to how OPS+ and ERA+ work. An elite player performing at their usual level stays hot, and a struggling player is not called hot for a small uptick.")
            Text("Scores are then smoothed to limit the effect of a single outlier game and sorted into five tiers: Red Hot, Hot, Neutral, Cold, and Ice Cold.")
        }
        .font(.subheadline)
        .foregroundStyle(.primary)
        .fixedSize(horizontal: false, vertical: true)
    }

    private func formula(_ text: String) -> some View {
        Text(text)
            .font(.system(.footnote, design: .monospaced))
            .foregroundStyle(.primary)
            .fixedSize(horizontal: false, vertical: true)
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .fill(Color(.secondarySystemBackground))
            )
    }
}

#Preview {
    VStack(spacing: 14) {
        HeatMeterView(score: 0.30,  tier: "red_hot",  window: "Last 15 games")
        HeatMeterView(score: 0.12,  tier: "hot",      window: "Last 15 games")
        HeatMeterView(score: 0.01,  tier: "neutral",  window: "Recent form")
        HeatMeterView(score: -0.12, tier: "cold",     window: "Last 8 starts")
        HeatMeterView(score: -0.30, tier: "ice_cold", window: "Recent form")
    }
    .padding()
    .background(Color(.systemGroupedBackground))
}
