//
//  TeamPickerView.swift
//  BaseballStats
//
//  Modal-style grid of all 30 MLB teams. Tap a tile → favorite is
//  written to `FavoriteTeamStore.shared` and the sheet (or root
//  picker) dismisses. Used both as the empty-state for the Home
//  tab (no favorite yet) and as a settings sheet for changing
//  the active selection.
//

import SwiftUI

struct TeamPickerView: View {
    /// Triggered when the user picks a team. Owner dismisses the
    /// sheet (or, if the view is a root, swaps to the hero view).
    var onPick: (Int) -> Void = { _ in }

    private let columns = [
        GridItem(.adaptive(minimum: 96, maximum: 140), spacing: 12),
    ]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text("Pick your team")
                    .font(.largeTitle.weight(.bold))
                    .padding(.horizontal, 16)
                    .padding(.top, 8)
                Text("Pick the team you follow most. You can change this anytime.")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 16)

                LazyVGrid(columns: columns, spacing: 12) {
                    ForEach(MLBTeamCatalog.all) { entry in
                        Button {
                            FavoriteTeamStore.shared.setFavorite(
                                bdlTeamId: entry.bdlTeamId,
                            )
                            onPick(entry.bdlTeamId)
                        } label: {
                            TeamPickerTile(entry: entry)
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, 16)
            }
            .padding(.bottom, 24)
        }
        .background(backgroundGradient)
    }

    /// Subtle gradient matching the Search and other glass-card
    /// screens. `ignoresSafeArea` so it bleeds under the sheet's
    /// drag indicator.
    private var backgroundGradient: some View {
        LinearGradient(
            colors: [Color(.systemGray6), Color(.systemBackground)],
            startPoint: .top, endPoint: .bottom,
        )
        .ignoresSafeArea()
    }
}

/// Individual tile in the picker grid — logo on top, full name
/// below, glass card border to match the rest of the app.
private struct TeamPickerTile: View {
    let entry: MLBTeamCatalog.Entry

    var body: some View {
        VStack(spacing: 8) {
            TeamLogoView(team: entry.teamInfo, size: 52)
                .padding(.top, 4)
            Text(entry.fullName)
                .font(.caption.weight(.semibold))
                .multilineTextAlignment(.center)
                .lineLimit(2)
                .minimumScaleFactor(0.85)
                .frame(maxWidth: .infinity)
        }
        .padding(.vertical, 10)
        .padding(.horizontal, 6)
        .frame(maxWidth: .infinity, minHeight: 108)
        .background(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .fill(.ultraThinMaterial)
        )
        .shadow(color: .black.opacity(0.06), radius: 8, y: 3)
        .foregroundStyle(.primary)
    }
}

#Preview {
    TeamPickerView()
}
