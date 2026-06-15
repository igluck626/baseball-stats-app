//
//  SearchView.swift
//  BaseballStats
//
//  Root screen: a searchable list of players. The view is intentionally
//  thin — debounce, cancellation, and error handling all live in
//  SearchViewModel.
//

import SwiftUI

struct SearchView: View {
    @StateObject private var viewModel = SearchViewModel()
    /// Drives the (empty) Player Comparison sheet opened from the browse
    /// landing's "Compare Players" card.
    @State private var showingCompare = false

    var body: some View {
        NavigationStack {
            ZStack {
                backgroundGradient
                content
            }
            .navigationTitle("Search")
            .navigationBarTitleDisplayMode(.inline)
            .navigationDestination(for: PlayerSearchResult.self) { player in
                PlayerProfileView(player: player)
            }
            .searchable(
                text: $viewModel.searchText,
                placement: .navigationBarDrawer(displayMode: .always),
                prompt: "Search players"
            )
            .textInputAutocapitalization(.words)
            .autocorrectionDisabled()
            .task { await viewModel.loadActiveStars() }
            .sheet(isPresented: $showingCompare) {
                PlayerCompareView()
            }
        }
    }

    // MARK: - Chrome

    /// Subtle vertical fade — light gray at the top to white at the bottom.
    /// Sits behind the list (which has scrollContentBackground hidden) so
    /// the gradient is visible through the rows.
    private var backgroundGradient: some View {
        LinearGradient(
            colors: [
                Color(.systemGray6),
                Color(.systemBackground),
            ],
            startPoint: .top,
            endPoint: .bottom
        )
        .ignoresSafeArea()
    }

    // MARK: - Content states

    @ViewBuilder
    private var content: some View {
        let trimmed = viewModel.searchText.trimmingCharacters(in: .whitespacesAndNewlines)
        if viewModel.isLoading && viewModel.results.isEmpty {
            loadingState
        } else if let message = viewModel.errorMessage, viewModel.results.isEmpty {
            errorState(message)
        } else if trimmed.count < 2 {
            // Idle landing — no query (or below the 2-char floor that
            // would trigger a fetch). Show the curated browse shelf
            // instead of a blank "type something" placeholder.
            browseLanding
        } else if viewModel.results.isEmpty {
            // Query is at-or-above the fetch floor but matched nothing.
            ContentUnavailableView.search(text: trimmed)
        } else {
            resultsList
        }
    }

    private var loadingState: some View {
        ProgressView()
            .controlSize(.large)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func errorState(_ message: String) -> some View {
        ContentUnavailableView {
            Label("Search failed", systemImage: "exclamationmark.triangle")
        } description: {
            Text(message)
        } actions: {
            Button("Try Again") {
                Task { await viewModel.search() }
            }
            .buttonStyle(.borderedProminent)
        }
    }

    private var resultsList: some View {
        List(viewModel.results) { player in
            // Hidden NavigationLink behind the row so we keep the row's
            // custom chevron/look without the system disclosure indicator
            // a visible NavigationLink would add inside a List.
            ZStack {
                NavigationLink(value: player) { EmptyView() }
                    .opacity(0)
                PlayerSearchResultRow(player: player)
            }
            .listRowSeparatorTint(Color(.systemGray4))
        }
        .listStyle(.insetGrouped)
        .scrollContentBackground(.hidden)
    }

    // MARK: - Browse landing

    @ViewBuilder
    private var browseLanding: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                compareEntryCard
                browseShelf(
                    title: "Active Stars",
                    players: viewModel.activeStars,
                )
            }
            .padding(.top, 16)
            .padding(.bottom, 24)
        }
        .scrollIndicators(.hidden)
    }

    /// Discovery entry point for the Player Comparison feature — opens an
    /// empty comparison the user fills in from scratch. Lives at the top
    /// of the idle browse landing so it's visible before any search.
    private var compareEntryCard: some View {
        Button {
            showingCompare = true
        } label: {
            HStack(spacing: 12) {
                Image(systemName: "person.2.crop.square.stack.fill")
                    .font(.title2)
                    .foregroundStyle(.tint)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Compare Players")
                        .font(.headline)
                        .foregroundStyle(.primary)
                    Text("Stack 2–4 players side by side")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Image(systemName: "chevron.right")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
            }
            .padding(14)
            .background(
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .fill(.ultraThinMaterial)
            )
            .shadow(color: .black.opacity(0.06), radius: 8, y: 3)
        }
        .buttonStyle(.plain)
        .padding(.horizontal, 16)
    }

    private func browseShelf(
        title: String,
        players: [PlayerSearchResult],
    ) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(title)
                .font(.title3.weight(.semibold))
                .padding(.horizontal, 16)

            if players.isEmpty {
                // The fetch hasn't landed yet (or every id failed).
                // Render a row of skeleton cards so the layout doesn't
                // pop on first paint.
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 12) {
                        ForEach(0..<6, id: \.self) { _ in
                            BrowseCard.placeholder
                        }
                    }
                    .padding(.horizontal, 16)
                }
                .disabled(true)
            } else {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 12) {
                        ForEach(players) { player in
                            NavigationLink(value: player) {
                                BrowseCard(player: player)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    .padding(.horizontal, 16)
                }
            }
        }
    }
}

// MARK: - BrowseCard

/// Compact horizontally-scrollable card for the discover shelf.
/// Glass background to match the row's headshot ring and the rest of
/// the app's ultra-thin-material cards.
private struct BrowseCard: View {
    let player: PlayerSearchResult

    var body: some View {
        VStack(spacing: 8) {
            headshot
            Text(player.name)
                .font(.subheadline.weight(.semibold))
                .lineLimit(1)
                .minimumScaleFactor(0.85)
                .multilineTextAlignment(.center)
            Text(detailLine ?? " ")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
        .frame(width: 116)
        .padding(.vertical, 12)
        .padding(.horizontal, 8)
        .background(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .fill(.ultraThinMaterial)
        )
        .shadow(color: .black.opacity(0.06), radius: 8, y: 3)
        .foregroundStyle(.primary)
    }

    /// Skeleton card — neutral material with a shimmering disc in
    /// place of the headshot. Used while the parallel fetch is in
    /// flight; the layout matches a real card so the swap is silent.
    static var placeholder: some View {
        VStack(spacing: 8) {
            Circle()
                .fill(Color(.systemGray5))
                .frame(width: 64, height: 64)
            RoundedRectangle(cornerRadius: 4, style: .continuous)
                .fill(Color(.systemGray5))
                .frame(width: 80, height: 12)
            RoundedRectangle(cornerRadius: 4, style: .continuous)
                .fill(Color(.systemGray5))
                .frame(width: 56, height: 10)
        }
        .frame(width: 116)
        .padding(.vertical, 12)
        .padding(.horizontal, 8)
        .background(
            RoundedRectangle(cornerRadius: 14, style: .continuous)
                .fill(.ultraThinMaterial)
        )
        .shadow(color: .black.opacity(0.06), radius: 8, y: 3)
    }

    private var headshot: some View {
        AsyncImage(url: player.largeHeadshotURL) { phase in
            switch phase {
            case .success(let image):
                image
                    .resizable()
                    .scaledToFill()
            case .empty, .failure:
                Image(systemName: "person.crop.circle.fill")
                    .resizable()
                    .scaledToFit()
                    .foregroundStyle(.tertiary)
            @unknown default:
                Image(systemName: "person.crop.circle.fill")
                    .resizable()
                    .scaledToFit()
                    .foregroundStyle(.tertiary)
            }
        }
        .frame(width: 64, height: 64)
        .background(Circle().fill(.ultraThinMaterial))
        .clipShape(Circle())
        .overlay(Circle().strokeBorder(.quaternary, lineWidth: 0.5))
    }

    /// "OF · NYY" / "RF" / "Team" — same precedence the row uses
    /// (position first, then team code), just compacted for the
    /// 116-pt card width. Lahman codes (`LAN`, `NYA`, `SLN`, …) get
    /// resolved to fan-facing abbreviations via the same lookup the
    /// Leaderboards tab uses, so the card never reads "LAN".
    private var detailLine: String? {
        let pos = player.position?.browseNonEmpty
        let team = player.teamCode?.browseNonEmpty.map { teamAbbreviation(for: $0) }
        switch (pos, team) {
        case let (p?, t?): return "\(p) · \(t)"
        case let (p?, nil): return p
        case let (nil, t?): return t
        default: return nil
        }
    }
}

private extension String {
    /// Returns nil when the string is empty; otherwise self. Prefixed
    /// to avoid colliding with the row's private `nonEmpty` in the
    /// neighboring file (Swift extensions don't conflict across
    /// files but the duplicate name is confusing to readers).
    var browseNonEmpty: String? { isEmpty ? nil : self }
}

#Preview {
    SearchView()
}
