//
//  AppearanceMode.swift
//  BaseballStats
//
//  User-selectable app appearance override (Settings → Appearance). Persisted
//  via @AppStorage("appearanceMode") and applied at the app root with
//  `.preferredColorScheme(_:)` so it cascades over the device's system setting.
//
//  `system` maps to a nil ColorScheme, which means "don't override — follow the
//  device," exactly how the app behaved before this control existed.
//

import SwiftUI

enum AppearanceMode: String, CaseIterable, Identifiable {
    case system
    case light
    case dark

    var id: String { rawValue }

    /// Short label for the Settings picker segments.
    var label: String {
        switch self {
        case .system: return "System"
        case .light:  return "Light"
        case .dark:   return "Dark"
        }
    }

    /// The ColorScheme to force, or nil to follow the device appearance.
    var colorScheme: ColorScheme? {
        switch self {
        case .system: return nil
        case .light:  return .light
        case .dark:   return .dark
        }
    }
}

/// Reads the persisted appearance choice and forces it on the content. Used at
/// the app root AND on modally-presented content (sheets get their own
/// environment, so `.preferredColorScheme` at the root does not reliably reach
/// them) — applying this modifier guarantees a sheet never renders in the
/// "wrong" mode relative to the user's choice.
private struct AppearanceOverride: ViewModifier {
    @AppStorage("appearanceMode") private var appearanceMode: AppearanceMode = .system

    func body(content: Content) -> some View {
        content.preferredColorScheme(appearanceMode.colorScheme)
    }
}

extension View {
    /// Apply the user's System/Light/Dark choice to this view tree.
    func appearanceOverride() -> some View {
        modifier(AppearanceOverride())
    }
}
