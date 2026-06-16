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
