//
//  SettingsView.swift
//  BaseballStats
//
//  App settings, presented as a sheet from the Home tab's gear button.
//  Standard grouped Form so it reads as a native iOS settings screen and
//  adapts to light/dark automatically.
//
//  Two settings today:
//    • Favorite Team — taps through to the existing `TeamPickerView`, which
//      writes the shared `FavoriteTeamStore` exactly as before (Home updates
//      identically). This screen only relocates the entry point.
//    • Reading → "Open articles in Reader Mode" — an `@AppStorage` toggle
//      ("autoReaderMode") that `SafariView` reads when opening news articles.
//

import SwiftUI

struct SettingsView: View {
    @Environment(\.dismiss) private var dismiss
    /// Same store the rest of the app uses — the row reflects (and the picker
    /// writes) the one source of truth.
    @ObservedObject private var teamStore = FavoriteTeamStore.shared
    @AppStorage("autoReaderMode") private var autoReaderMode = false
    @AppStorage("appearanceMode") private var appearanceMode: AppearanceMode = .system

    private var currentTeamName: String {
        guard let bdlId = teamStore.bdlTeamId,
              let entry = MLBTeamCatalog.entry(forBDLId: bdlId) else {
            return "None"
        }
        return entry.fullName
    }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    // Reuses the existing picker (pushed); selecting a team
                    // writes FavoriteTeamStore and pops back here.
                    NavigationLink {
                        FavoriteTeamPicker()
                    } label: {
                        LabeledContent("Favorite Team", value: currentTeamName)
                    }
                } header: {
                    Text("Favorite Team")
                } footer: {
                    Text("The team shown on the Home tab.")
                }

                Section {
                    Picker("Appearance", selection: $appearanceMode) {
                        ForEach(AppearanceMode.allCases) { mode in
                            Text(mode.label).tag(mode)
                        }
                    }
                    .pickerStyle(.segmented)
                } header: {
                    Text("Appearance")
                } footer: {
                    Text("System follows your device's Light/Dark setting.")
                }

                Section {
                    Toggle("Open articles in Reader Mode", isOn: $autoReaderMode)
                } header: {
                    Text("Reading")
                } footer: {
                    Text("Show articles in a cleaner, simplified view when available.")
                }

                Section {
                    // Pushes within this NavigationStack; inherits the
                    // appearance override applied to the sheet below.
                    NavigationLink {
                        AboutView()
                    } label: {
                        Text("About")
                    }
                }
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
        }
        // The Settings sheet has its own environment, so force the chosen
        // appearance here too — and it updates live as the user taps a segment.
        .preferredColorScheme(appearanceMode.colorScheme)
    }
}

/// Hosts the existing `TeamPickerView` as a pushed Settings detail. Picking a
/// team writes the shared store (unchanged) and pops back to Settings, so the
/// "Favorite Team" row updates. No team-selection UI is rebuilt here.
private struct FavoriteTeamPicker: View {
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        TeamPickerView { _ in dismiss() }
            .navigationTitle("Favorite Team")
            .navigationBarTitleDisplayMode(.inline)
    }
}

#Preview {
    SettingsView()
}
